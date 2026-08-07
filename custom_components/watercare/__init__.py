"""Watercare custom integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WatercareApi
from .const import (
    DOMAIN,
    CONF_CONSUMPTION_RATE,
    CONF_WASTEWATER_RATE,
    CONF_WASTEWATER_RATIO,
    CONF_ANNUAL_LINE_CHARGE,
    CONF_ENDPOINT,
    DEFAULT_CONSUMPTION_RATE,
    DEFAULT_WASTEWATER_RATE,
    DEFAULT_WASTEWATER_RATIO,
    DEFAULT_ANNUAL_LINE_CHARGE,
    DEFAULT_ENDPOINT,
)
from .coordinator import WatercareCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

type WatercareConfigEntry = ConfigEntry[WatercareCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WatercareConfigEntry) -> bool:
    """Set up Watercare from a config entry."""

    # Entries created before v1.2.2 stored the login under "email".
    email = entry.data.get(CONF_USERNAME) or entry.data.get("email")
    password = entry.data.get(CONF_PASSWORD)

    if not email or not password:
        raise ConfigEntryError("Config entry is missing the username or password")

    api = WatercareApi(email, password, session=async_get_clientsession(hass))

    # Options (saved from the options flow) override initial setup data.
    config = {**entry.data, **entry.options}
    coordinator = WatercareCoordinator(
        hass,
        entry,
        api,
        config.get(CONF_CONSUMPTION_RATE, DEFAULT_CONSUMPTION_RATE),
        config.get(CONF_WASTEWATER_RATE, DEFAULT_WASTEWATER_RATE),
        config.get(CONF_WASTEWATER_RATIO, DEFAULT_WASTEWATER_RATIO),
        config.get(CONF_ANNUAL_LINE_CHARGE, DEFAULT_ANNUAL_LINE_CHARGE),
        config.get(CONF_ENDPOINT, DEFAULT_ENDPOINT),
    )

    _async_migrate_unique_id(hass, entry)

    await coordinator.async_config_entry_first_refresh()

    # Entries created before v1.2.2 have no unique id; backfill the account
    # number so duplicate accounts are refused from now on.
    if entry.unique_id is None and api.account_number:
        hass.config_entries.async_update_entry(
            entry, unique_id=str(api.account_number)
        )

    entry.runtime_data = coordinator

    # Reload the entry whenever options change so new rates apply immediately.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


@callback
def _async_migrate_unique_id(hass: HomeAssistant, entry: WatercareConfigEntry) -> None:
    """Move a pre-1.2.2 entity to the entry-scoped unique id.

    The original sensor used the bare domain string as its unique id. Updating
    the registry entry in place keeps the entity id and all recorded history.
    """
    registry = er.async_get(hass)
    legacy_entity_id = registry.async_get_entity_id(Platform.SENSOR, DOMAIN, DOMAIN)
    new_unique_id = f"{entry.entry_id}_usage"

    if legacy_entity_id and not registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, new_unique_id
    ):
        _LOGGER.info(
            "Migrating %s unique id from %r to %r",
            legacy_entity_id,
            DOMAIN,
            new_unique_id,
        )
        registry.async_update_entity(legacy_entity_id, new_unique_id=new_unique_id)


async def _async_options_updated(
    hass: HomeAssistant, entry: WatercareConfigEntry
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: WatercareConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
