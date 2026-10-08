"""Tests for setup, unload, entry migration and the coordinator."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.watercare import async_migrate_entry
from custom_components.watercare.api import (
    WatercareAuthError,
    WatercareConnectionError,
)
from custom_components.watercare.const import DOMAIN, STAT_CONSUMPTION
from custom_components.watercare.statistics import day_start

from .common import (
    ACCOUNT_NUMBER,
    EMAIL,
    PASSWORD,
    api_period,
    default_periods,
    make_entry,
)


async def _setup(hass: HomeAssistant, entry: Any) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_and_unload(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data.period_count == 3
    assert ha.states.get("sensor.watercare_last_bill_usage").state == "11000"

    assert await ha.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_without_credentials_fails(ha: HomeAssistant) -> None:
    entry = make_entry(data={CONF_USERNAME: EMAIL})
    await _setup(ha, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_backfills_a_missing_unique_id(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry(unique_id=None)
    await _setup(ha, entry)
    assert entry.unique_id == ACCOUNT_NUMBER


async def test_refresh_token_is_persisted(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry()
    await _setup(ha, entry)

    callback = entry.runtime_data.api._token_callback
    assert callback is not None
    callback("rotated-token")

    assert entry.data["refresh_token"] == "rotated-token"
    assert entry.state is ConfigEntryState.LOADED


async def test_entity_registry_migrations(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry()
    entry.add_to_hass(ha)
    registry = er.async_get(ha)
    legacy = registry.async_get_or_create(
        Platform.SENSOR, DOMAIN, DOMAIN, config_entry=entry, suggested_object_id="water"
    )
    disabled_by_us = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{entry.entry_id}_account_balance",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    disabled_by_user = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{entry.entry_id}_amount_due",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done()

    migrated = registry.async_get(legacy.entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{entry.entry_id}_usage"
    balance = registry.async_get(disabled_by_us.entity_id)
    assert balance is not None
    assert balance.disabled_by is None
    due = registry.async_get(disabled_by_user.entity_id)
    assert due is not None
    assert due.disabled_by is er.RegistryEntryDisabler.USER


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (WatercareAuthError("no"), ConfigEntryState.SETUP_ERROR),
        (WatercareConnectionError("down"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_errors(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    error: Exception,
    state: ConfigEntryState,
) -> None:
    mock_api["account"].side_effect = error
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is state
    flows = ha.config_entries.flow.async_progress()
    reauth = [flow for flow in flows if flow["context"]["source"] == SOURCE_REAUTH]
    assert bool(reauth) is isinstance(error, WatercareAuthError)


@pytest.mark.parametrize("payload", [{"usage": []}, [], [{"junk": True}]])
async def test_unusable_billing_data_retries(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock], payload: Any
) -> None:
    mock_api["periods"].return_value = payload
    entry = make_entry()
    await _setup(ha, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_skipped_and_duplicate_periods_are_tolerated(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    periods = default_periods()
    mock_api["periods"].return_value = [*periods, periods[0], {"junk": True}]
    entry = make_entry()
    await _setup(ha, entry)

    data = entry.runtime_data.data
    assert data.skipped_periods == 1
    assert data.duplicate_periods == 1


async def _add_legacy_consumption(hass: HomeAssistant, *days: date) -> None:
    async_add_external_statistics(
        hass,
        {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": "Watercare Water Consumption",
            "source": DOMAIN,
            "statistic_id": STAT_CONSUMPTION,
            "unit_class": "volume",
            "unit_of_measurement": "L",
        },
        [
            {"start": day_start(day), "sum": 1000.0 * (index + 1)}
            for index, day in enumerate(days)
        ],
    )
    await async_wait_recording_done(hass)


async def test_first_run_rebuilds_legacy_statistics(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    await _add_legacy_consumption(ha, date(2026, 7, 16), date(2026, 8, 16))
    entry = make_entry(statistics_version=None)
    await _setup(ha, entry)

    coordinator = entry.runtime_data
    assert coordinator.statistics_status == "rebuilt"
    assert coordinator.data.import_result.rebuilt
    assert coordinator.data.import_result.consumption_rows == 92
    assert entry.data["statistics_version"] == 2

    # Later polls only add new days.
    await coordinator.async_refresh()
    assert coordinator.data.import_result.consumption_rows == 0
    assert not coordinator.data.import_result.rebuilt


async def test_rebuild_is_skipped_when_it_would_lose_history(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    await _add_legacy_consumption(ha, date(2019, 12, 13), date(2026, 8, 16))
    entry = make_entry(statistics_version=None)
    await _setup(ha, entry)

    coordinator = entry.runtime_data
    assert coordinator.statistics_status == "rebuild_skipped"
    assert entry.data["statistics_version"] == 2
    issue = ir.async_get(ha).async_get_issue(
        DOMAIN, f"statistics_rebuild_skipped_{entry.entry_id}"
    )
    assert issue is not None
    # Only the bill after the stored history was added.
    assert coordinator.data.import_result.consumption_rows == 30


async def test_tariff_issue_is_raised_and_cleared(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2027-07-02T12:00:00+12:00")
    mock_api["periods"].return_value = [
        api_period(date(2027, 6, 16), date(2027, 7, 1), 5000),
        *default_periods(),
    ]
    entry = make_entry()
    await _setup(ha, entry)

    coordinator = entry.runtime_data
    issue_id = f"tariff_missing_{entry.entry_id}"
    issue = ir.async_get(ha).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders == {"financial_year": "2027/28"}
    assert issue.data == {"entry_id": entry.entry_id, "financial_year": 2027}
    assert coordinator.data.missing_tariff_year == 2027
    assert coordinator.data.latest_cost is None
    assert coordinator.data.import_result.first_day_without_tariff == date(2027, 7, 1)
    assert ha.states.get("sensor.watercare_last_bill_cost").state == "unknown"

    ha.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "tariff_overrides": {
                "2027": {"water_rate": 2.6, "wastewater_rate": 4.5, "fixed_charge": 380}
            },
        },
    )
    await ha.config_entries.async_reload(entry.entry_id)
    await ha.async_block_till_done()

    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is None
    assert entry.runtime_data.data.latest_cost is not None


async def test_migrate_drops_rates_that_match_a_published_year(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry(
        minor_version=1,
        data={"email": EMAIL, CONF_PASSWORD: PASSWORD, "consumption_rate": 1.0},
        options={
            "endpoint": "mechanicalmonthly",
            "consumption_rate": 2.296,
            "wastewater_rate": 3.994,
            "wastewater_ratio": 0.785,
            "annual_line_charge": 332,
        },
    )
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 2
    assert entry.data[CONF_USERNAME] == EMAIL
    assert "email" not in entry.data
    assert "consumption_rate" not in entry.data
    assert entry.options == {"wastewater_ratio": 0.785}


async def test_migrate_keeps_unpublished_rates_for_the_current_year(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-10-08T12:00:00+13:00")
    entry = make_entry(
        minor_version=1,
        options={
            "consumption_rate": 2.5,
            "wastewater_rate": 4.0,
            "wastewater_ratio": 0.95,
            "annual_line_charge": 350,
        },
    )
    await _setup(ha, entry)

    assert entry.options == {
        "wastewater_ratio": 0.95,
        "tariff_overrides": {
            "2026": {"water_rate": 2.5, "wastewater_rate": 4.0, "fixed_charge": 350.0}
        },
    }


async def test_migrate_without_rates_uses_defaults(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry(minor_version=1, options={"wastewater_ratio": "bad"})
    await _setup(ha, entry)
    assert entry.options == {"wastewater_ratio": 0.785}


async def test_migrate_refuses_a_newer_version(ha: HomeAssistant) -> None:
    entry = make_entry(version=2, minor_version=1)
    await _setup(ha, entry)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_migrate_entry_guards(ha: HomeAssistant) -> None:
    newer = make_entry(version=2, minor_version=1)
    current = make_entry(minor_version=2, options={"wastewater_ratio": 0.95})
    newer.add_to_hass(ha)

    assert not await async_migrate_entry(ha, newer)
    assert await async_migrate_entry(ha, current)
    assert current.options == {"wastewater_ratio": 0.95}
