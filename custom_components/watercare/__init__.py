"""The Watercare integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .api import WatercareApi
from .const import (
    CONF_FIXED_CHARGE,
    CONF_REFRESH_TOKEN,
    CONF_TARIFF_OVERRIDES,
    CONF_WASTEWATER_RATE,
    CONF_WASTEWATER_RATIO,
    CONF_WATER_RATE,
    DEFAULT_WASTEWATER_RATIO,
    DOMAIN,
    ISSUE_REBUILD_SKIPPED,
    ISSUE_TARIFF_MISSING,
    LEGACY_ANNUAL_LINE_CHARGE,
    LEGACY_CONSUMPTION_RATE,
    LEGACY_EMAIL,
    LEGACY_ENDPOINT,
    LEGACY_WASTEWATER_RATE,
    NZ_TIMEZONE,
    PLATFORMS,
)
from .coordinator import WatercareCoordinator
from .session import sign_in_session_factory
from .tariffs import financial_year, financial_year_label, matching_published_year

_LOGGER = logging.getLogger(__name__)

type WatercareConfigEntry = ConfigEntry[WatercareCoordinator]

_LEGACY_KEYS = (
    LEGACY_CONSUMPTION_RATE,
    LEGACY_WASTEWATER_RATE,
    LEGACY_ANNUAL_LINE_CHARGE,
    LEGACY_ENDPOINT,
    CONF_WASTEWATER_RATIO,
    LEGACY_EMAIL,
)


async def async_setup_entry(hass: HomeAssistant, entry: WatercareConfigEntry) -> bool:
    """Set up Watercare from a config entry."""
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    if not username or not password:
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="missing_credentials"
        )

    @callback
    def _async_store_refresh_token(token: str) -> None:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
        )

    api = WatercareApi(
        username,
        password,
        async_get_clientsession(hass),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        token_callback=_async_store_refresh_token,
        sign_in_session=sign_in_session_factory(hass),
    )
    coordinator = WatercareCoordinator(hass, entry, api)

    _async_migrate_entities(hass, entry)

    await coordinator.async_config_entry_first_refresh()

    # Entries created before 1.2.2 have no unique id; use the account number
    # so the same account cannot be added twice.
    account = coordinator.data.account
    if entry.unique_id is None and account is not None:
        hass.config_entries.async_update_entry(entry, unique_id=account.account_number)

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The statistics (and the one-off rebuild) go through the recorder, which
    # only works through its queue once Home Assistant has started. Start them
    # then, in the background, so setup never waits for the recorder.
    @callback
    def _async_start_statistics(_hass: HomeAssistant) -> None:
        coordinator.async_start_statistics()

    entry.async_on_unload(async_at_started(hass, _async_start_statistics))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WatercareConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the entry's repair issues. Statistics are kept on purpose."""
    for issue in (ISSUE_TARIFF_MISSING, ISSUE_REBUILD_SKIPPED):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate entries from 1.4.x (version 1.1) to version 1.2.

    1.4.x stored one flat tariff in the options and priced all history with
    it. 1.5.0 uses Watercare's published prices per financial year, so the
    flat tariff is dropped when it equals a published year (which is what a
    user copies off a bill), and kept as the current year's figures
    otherwise.
    """
    if entry.version > 1:
        # A newer release created this entry; refuse to guess.
        return False
    if entry.minor_version >= 2:  # noqa: PLR2004
        return True

    legacy: dict[str, Any] = {**entry.data, **entry.options}
    options: dict[str, Any] = {
        CONF_WASTEWATER_RATIO: _number(
            legacy.get(CONF_WASTEWATER_RATIO), DEFAULT_WASTEWATER_RATIO
        )
    }
    rates = (
        legacy.get(LEGACY_CONSUMPTION_RATE),
        legacy.get(LEGACY_WASTEWATER_RATE),
        legacy.get(LEGACY_ANNUAL_LINE_CHARGE),
    )
    if all(_is_number(rate) for rate in rates):
        water, wastewater, fixed = (float(rate) for rate in rates)  # type: ignore[arg-type]
        published = matching_published_year(water, wastewater, fixed)
        if published is not None:
            _LOGGER.info(
                "Watercare: the configured rates match the published %s prices; "
                "using the published price table for every year",
                financial_year_label(published),
            )
        else:
            year = financial_year(dt_util.now(NZ_TIMEZONE).date())
            options[CONF_TARIFF_OVERRIDES] = {
                str(year): {
                    CONF_WATER_RATE: water,
                    CONF_WASTEWATER_RATE: wastewater,
                    CONF_FIXED_CHARGE: fixed,
                }
            }
            _LOGGER.warning(
                "Watercare: the configured rates match no published year; keeping "
                "them as the %s prices. Check them in the integration options",
                financial_year_label(year),
            )

    data = {key: value for key, value in entry.data.items() if key not in _LEGACY_KEYS}
    if not data.get(CONF_USERNAME) and entry.data.get(LEGACY_EMAIL):
        data[CONF_USERNAME] = entry.data[LEGACY_EMAIL]

    hass.config_entries.async_update_entry(
        entry, data=data, options=options, minor_version=2
    )
    _LOGGER.debug("Migrated the Watercare config entry to version 1.2")
    return True


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _number(value: Any, default: float) -> float:
    return float(value) if _is_number(value) else default


@callback
def _async_migrate_entities(hass: HomeAssistant, entry: WatercareConfigEntry) -> None:
    """Carry entity-registry fixes from earlier releases.

    * Before 1.2.2 the usage sensor's unique id was the bare domain. Moving it
      to the entry-scoped id keeps the entity id and its history.
    * 1.2.x shipped two sensors disabled by the integration while their data
      was missing. Re-enable them, but never a sensor the user disabled.
    """
    registry = er.async_get(hass)
    legacy_entity_id = registry.async_get_entity_id(Platform.SENSOR, DOMAIN, DOMAIN)
    new_unique_id = f"{entry.entry_id}_usage"
    if legacy_entity_id and not registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, new_unique_id
    ):
        _LOGGER.info("Moving %s to an entry-scoped unique id", legacy_entity_id)
        registry.async_update_entity(legacy_entity_id, new_unique_id=new_unique_id)

    for key in ("account_balance", "amount_due"):
        entity_id = registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, f"{entry.entry_id}_{key}"
        )
        if not entity_id:
            continue
        existing = registry.async_get(entity_id)
        if existing and existing.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            _LOGGER.info("Re-enabling %s", entity_id)
            registry.async_update_entity(entity_id, disabled_by=None)
