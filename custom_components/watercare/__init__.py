"""The Watercare integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.start import async_at_started

from .api import WatercareApi
from .const import (
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ISSUE_SMART_METER,
    ISSUE_TARIFF_MISSING,
    ISSUE_UPGRADE_REQUIRED,
    PLATFORMS,
    RETIRED_ISSUE_REBUILD_SKIPPED,
)
from .coordinator import WatercareCoordinator
from .session import sign_in_session_factory

_LOGGER = logging.getLogger(__name__)

type WatercareConfigEntry = ConfigEntry[WatercareCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WatercareConfigEntry) -> bool:
    """Set up Watercare from a config entry."""
    # Nothing raises this notice any more, and Home Assistant keeps it.
    ir.async_delete_issue(
        hass, DOMAIN, f"{RETIRED_ISSUE_REBUILD_SKIPPED}_{entry.entry_id}"
    )
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
        # Stay with the property set up, if the login lists several.
        account_number=entry.unique_id,
    )
    coordinator = WatercareCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The statistics go through the recorder, which only works through its
    # queue once Home Assistant has started. Start them then, in the
    # background, so setup never waits for the recorder, and stop them when
    # the entry unloads.
    @callback
    def _async_start_statistics(_hass: HomeAssistant) -> None:
        coordinator.async_start_statistics()

    entry.async_on_unload(coordinator.async_stop_statistics)
    entry.async_on_unload(async_at_started(hass, _async_start_statistics))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WatercareConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the entry's repair issues. Statistics are kept on purpose."""
    for issue in (
        ISSUE_TARIFF_MISSING,
        ISSUE_SMART_METER,
        ISSUE_UPGRADE_REQUIRED,
        RETIRED_ISSUE_REBUILD_SKIPPED,
    ):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Refuse entries from 1.4.x and earlier, which are no longer migrated.

    Home Assistant itself refuses an entry from a newer major version. One
    with a newer minor version, from a later release after going back to
    this one, loads as it is.
    """
    del hass
    if entry.minor_version < 2:  # noqa: PLR2004
        _LOGGER.error(
            "This Watercare entry was created by version 1.4 or earlier, which "
            "this version no longer upgrades. Install Watercare 1.5.1 first, or "
            "remove Watercare, delete its four statistics and add it again"
        )
        return False
    return True
