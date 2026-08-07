"""Tests for __init__._async_migrate_unique_id.

Covers the pre-1.2.2 -> current migration: the legacy "watercare" unique id
must move to the entry-scoped id *in place* (same entity_id, same history),
and account_balance/amount_due entities that were disabled by the
integration itself (because the account payload never carried them) must be
re-enabled now that it does -- without touching entities a user disabled on
purpose.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.watercare import _async_migrate_unique_id
from custom_components.watercare.const import DOMAIN


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    return entry


async def test_legacy_unique_id_migrates_in_place(hass) -> None:
    entry = _entry(hass)
    registry = er.async_get(hass)

    legacy = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        DOMAIN,  # the pre-1.2.2 unique id was the bare domain string
        config_entry=entry,
        suggested_object_id="watercare",
    )
    original_entity_id = legacy.entity_id

    _async_migrate_unique_id(hass, entry)

    migrated = registry.async_get(original_entity_id)
    assert migrated is not None, "the entity itself must be preserved, not recreated"
    assert migrated.unique_id == f"{entry.entry_id}_usage"


async def test_migration_is_a_no_op_once_already_migrated(hass) -> None:
    """Running migration twice (e.g. across restarts) must not error or
    duplicate anything."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        DOMAIN,
        config_entry=entry,
        suggested_object_id="watercare",
    )

    _async_migrate_unique_id(hass, entry)
    _async_migrate_unique_id(hass, entry)  # must not raise

    migrated_entity_id = registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{entry.entry_id}_usage"
    )
    assert migrated_entity_id is not None


async def test_no_legacy_entity_is_a_no_op(hass) -> None:
    """A fresh install (no pre-1.2.2 entity at all) must not error."""
    entry = _entry(hass)

    _async_migrate_unique_id(hass, entry)  # must not raise


async def test_integration_disabled_balance_entities_are_reenabled(hass) -> None:
    entry = _entry(hass)
    registry = er.async_get(hass)

    balance = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{entry.entry_id}_account_balance",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    amount_due = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{entry.entry_id}_amount_due",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )

    _async_migrate_unique_id(hass, entry)

    assert registry.async_get(balance.entity_id).disabled_by is None
    assert registry.async_get(amount_due.entity_id).disabled_by is None


async def test_user_disabled_balance_entity_is_left_alone(hass) -> None:
    """A user who deliberately disabled account_balance must not have it
    silently re-enabled underneath them."""
    entry = _entry(hass)
    registry = er.async_get(hass)

    user_disabled = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{entry.entry_id}_account_balance",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    _async_migrate_unique_id(hass, entry)

    assert registry.async_get(user_disabled.entity_id).disabled_by is (
        er.RegistryEntryDisabler.USER
    )


async def test_already_enabled_balance_entity_is_left_alone(hass) -> None:
    entry = _entry(hass)
    registry = er.async_get(hass)

    enabled = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{entry.entry_id}_account_balance",
        config_entry=entry,
    )
    assert enabled.disabled_by is None

    _async_migrate_unique_id(hass, entry)  # must not raise

    assert registry.async_get(enabled.entity_id).disabled_by is None
