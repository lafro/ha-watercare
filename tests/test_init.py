"""Tests for setup, unload, entry migration and the coordinator."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from sqlalchemy.exc import OperationalError

from custom_components.watercare import async_migrate_entry
from custom_components.watercare.api import (
    WatercareAuthError,
    WatercareConnectionError,
    _default_sign_in_session,
)
from custom_components.watercare.const import (
    ALL_STATISTIC_IDS,
    DOMAIN,
    STAT_CONSUMPTION,
    STAT_CONSUMPTION_COST,
    STAT_TOTAL_COST,
    STAT_WASTEWATER_COST,
)
from custom_components.watercare.models import parse_billing_periods
from custom_components.watercare.statistics import bill_cost, day_start
from custom_components.watercare.tariffs import PUBLISHED_TARIFFS, TariffSchedule

from .common import (
    ACCOUNT_NUMBER,
    EMAIL,
    PASSWORD,
    add_legacy_statistics,
    api_period,
    default_periods,
    make_entry,
    stored_rows,
)


async def _setup(hass: HomeAssistant, entry: Any) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    # The statistics update runs in the background after setup.
    await hass.async_block_till_done(wait_background_tasks=True)


async def _refresh(hass: HomeAssistant, coordinator: Any) -> None:
    """Poll, then let the statistics update that follows finish."""
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def _reload(hass: HomeAssistant, entry: Any) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_and_unload(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data.period_count == 3
    # Sign-in sessions come from Home Assistant's helper (session.py).
    assert entry.runtime_data.api._sign_in_session is not _default_sign_in_session
    assert ha.states.get("sensor.watercare_last_bill_usage").state == "13000"

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
    await ha.async_block_till_done(wait_background_tasks=True)

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
    await _add_legacy_consumption(ha, date(2026, 7, 3), date(2026, 8, 3))
    entry = make_entry(statistics_version=None)
    await _setup(ha, entry)

    coordinator = entry.runtime_data
    assert coordinator.statistics_status == "rebuilt"
    assert coordinator.last_import.rebuilt
    assert coordinator.last_import.consumption_rows == 92
    assert entry.data["statistics_version"] == 2

    # Later polls only add new days.
    await _refresh(ha, coordinator)
    assert coordinator.last_import.consumption_rows == 0
    assert not coordinator.last_import.rebuilt


async def test_rebuild_is_skipped_when_it_would_lose_history(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    await _add_legacy_consumption(ha, date(2019, 12, 13), date(2026, 8, 3))
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
    assert coordinator.last_import.consumption_rows == 30


async def test_tariff_issue_is_raised_and_cleared(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2027-08-10T12:00:00+12:00")
    mock_api["periods"].return_value = [
        api_period(date(2027, 7, 4), date(2027, 8, 3), 5000),
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
    assert coordinator.missing_tariff_year == 2027
    assert coordinator.data.latest_cost is None
    assert coordinator.last_import.first_day_without_tariff == date(2027, 7, 4)
    assert coordinator.last_import.missing_tariff_year == 2027
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
    await _reload(ha, entry)

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


async def test_tariff_issue_names_the_earliest_unpriced_year(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    # A year was skipped: 2028/29 prices are known, 2027/28 are not.
    freezer.move_to("2028-08-02T12:00:00+12:00")
    mock_api["periods"].return_value = [
        api_period(date(2027, 7, 4), date(2027, 8, 3), 5000),
        *default_periods(),
    ]
    entry = make_entry(
        options={
            "wastewater_ratio": 0.785,
            "tariff_overrides": {
                "2028": {"water_rate": 2.8, "wastewater_rate": 4.9, "fixed_charge": 400}
            },
        }
    )
    await _setup(ha, entry)

    issue = ir.async_get(ha).async_get_issue(DOMAIN, f"tariff_missing_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders == {"financial_year": "2027/28"}
    assert entry.runtime_data.missing_tariff_year == 2027


async def test_removing_the_entry_removes_its_issues(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2027-07-02T12:00:00+12:00")
    entry = make_entry()
    await _setup(ha, entry)
    issues = ir.async_get(ha)
    assert issues.async_get_issue(DOMAIN, f"tariff_missing_{entry.entry_id}")

    await ha.config_entries.async_remove(entry.entry_id)

    assert issues.async_get_issue(DOMAIN, f"tariff_missing_{entry.entry_id}") is None


async def test_bill_spanning_1_july_keeps_its_cost_while_the_new_year_is_unknown(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    # The latest bill started in June 2027, so 2026/27 prices apply to all of
    # it; only the current year's prices are missing.
    freezer.move_to("2027-07-10T12:00:00+12:00")
    mock_api["periods"].return_value = [
        api_period(date(2027, 6, 4), date(2027, 7, 3), 5000),
        *default_periods(),
    ]
    entry = make_entry()
    await _setup(ha, entry)

    data = entry.runtime_data.data
    assert entry.runtime_data.last_import.first_day_without_tariff is None
    assert data.latest_cost is not None
    assert data.latest_tariff == PUBLISHED_TARIFFS[2026]
    assert entry.runtime_data.missing_tariff_year == 2027
    usage = ha.states.get("sensor.watercare_last_bill_usage")
    assert usage is not None
    assert usage.attributes["tariff_year"] == "2026/27"


def _legacy_entry() -> Any:
    """A config entry exactly as 1.4.x left it (version 1.1, flat prices)."""
    return make_entry(
        minor_version=1,
        statistics_version=None,
        unique_id=None,
        data={"email": EMAIL, CONF_PASSWORD: PASSWORD},
        options={
            "endpoint": "mechanicalmonthly",
            "consumption_rate": 2.296,
            "wastewater_rate": 3.994,
            "wastewater_ratio": 0.785,
            "annual_line_charge": 332,
        },
    )


async def test_upgrade_from_1_4_x_end_to_end(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    await add_legacy_statistics(ha)
    assert len(await stored_rows(ha, STAT_TOTAL_COST)) == 3
    entry = _legacy_entry()

    await _setup(ha, entry)

    # Config entry migration.
    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 2
    assert entry.unique_id == ACCOUNT_NUMBER
    assert entry.options == {"wastewater_ratio": 0.785}
    assert entry.data[CONF_USERNAME] == EMAIL
    assert entry.data["statistics_version"] == 2
    coordinator = entry.runtime_data
    assert coordinator.statistics_status == "rebuilt"

    # The legacy rows are gone and every statistic holds one row per day.
    periods = parse_billing_periods(default_periods()).periods
    costs = [
        bill_cost(p, periods, TariffSchedule({}), Decimal("0.785")) for p in periods
    ]
    assert all(cost is not None for cost in costs)
    expected = {
        STAT_CONSUMPTION: 33000.0,
        STAT_TOTAL_COST: float(sum(cost.total for cost in costs if cost)),
        STAT_CONSUMPTION_COST: float(sum(cost.water for cost in costs if cost)),
        STAT_WASTEWATER_COST: float(sum(cost.wastewater for cost in costs if cost)),
    }
    for statistic_id, final_sum in expected.items():
        rows = await stored_rows(ha, statistic_id)
        assert len(rows) == 92, statistic_id
        assert datetime.fromtimestamp(rows[0]["start"], tz=UTC) == day_start(
            date(2026, 6, 3)
        )
        assert rows[-1]["sum"] == pytest.approx(final_sum), statistic_id
    # The June bill spans 1 July and keeps its 2025/26 price; the later bills
    # now cost more than the flat 1.4.x price gave them.
    assert costs[0] is not None
    assert round(costs[0].total, 2) == Decimal("93.37")
    assert expected[STAT_TOTAL_COST] == pytest.approx(93.3727 + 76.7855 + 104.9095)

    # The next poll adds nothing and keeps the rebuilt rows.
    await _refresh(ha, coordinator)
    assert coordinator.last_import.consumption_rows == 0
    assert len(await stored_rows(ha, STAT_TOTAL_COST)) == 92


async def test_rebuild_retries_after_a_failed_read(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    await add_legacy_statistics(ha)
    entry = make_entry(statistics_version=None)

    # The database cannot be read when the rebuild checks the stored history.
    with patch(
        "custom_components.watercare.statistics.statistics_during_period",
        side_effect=OperationalError("SELECT", {}, Exception("database is locked")),
    ):
        await _setup(ha, entry)

    # The sensors work; the statistics wait for the next poll.
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.statistics_status == "rebuild_pending"
    assert "statistics_version" not in entry.data
    # Nothing was cleared: the legacy rows are still there.
    assert len(await stored_rows(ha, STAT_CONSUMPTION)) == 3

    await _refresh(ha, coordinator)

    assert coordinator.statistics_status == "rebuilt"
    assert entry.data["statistics_version"] == 2
    assert len(await stored_rows(ha, STAT_CONSUMPTION)) == 92


async def test_rebuild_retries_after_the_recorder_fails_the_clear(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A clear the recorder fails leaves the marker unset.

    The recorder runs the clear only once. On a database error it logs the
    error and moves on, and the imports queued behind the clear still run, on
    top of the 1.4.x rows. The rebuild is recorded as done only if the clear
    succeeded, so the next update rebuilds.
    """
    await add_legacy_statistics(ha)
    entry = make_entry(statistics_version=None)
    clears: list[list[str]] = []
    clear_statistics = recorder_statistics.clear_statistics

    def _locked_once(instance: Recorder, statistic_ids: list[str]) -> None:
        clears.append(statistic_ids)
        if len(clears) == 1:
            raise OperationalError("DELETE", {}, Exception("database is locked"))
        clear_statistics(instance, statistic_ids)

    with patch.object(recorder_statistics, "clear_statistics", _locked_once):
        await _setup(ha, entry)

        assert len(clears) == 1
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data
        assert "statistics_version" not in entry.data
        assert coordinator.statistics_status == "rebuild_pending"
        assert not coordinator.last_import.rebuilt
        assert "The recorder did not clear the Watercare statistics" in caplog.text

        await _refresh(ha, coordinator)

    # The check before the rebuild let it run again, and this clear succeeded.
    assert len(clears) == 2
    assert coordinator.statistics_status == "rebuilt"
    assert coordinator.last_import.rebuilt
    assert entry.data["statistics_version"] == 2
    for statistic_id in ALL_STATISTIC_IDS:
        rows = await stored_rows(ha, statistic_id)
        assert len(rows) == 92, statistic_id
        assert datetime.fromtimestamp(rows[0]["start"], tz=UTC) == day_start(
            date(2026, 6, 3)
        )
    consumption = await stored_rows(ha, STAT_CONSUMPTION)
    assert consumption[-1]["sum"] == pytest.approx(33000)


async def test_rebuild_retries_after_the_import_fails(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    await add_legacy_statistics(ha)
    entry = make_entry(statistics_version=None)
    calls: list[str] = []

    def _fail_on_the_cost_rows(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        calls.append(metadata["statistic_id"])
        if len(calls) > 1:
            raise HomeAssistantError("import failed")
        async_add_external_statistics(hass, metadata, rows)

    # The clear completes, the consumption rows are queued, then the import
    # of the first cost statistic fails.
    with patch(
        "custom_components.watercare.statistics.async_add_external_statistics",
        side_effect=_fail_on_the_cost_rows,
    ):
        await _setup(ha, entry)

    assert calls == [STAT_CONSUMPTION, STAT_TOTAL_COST]
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.statistics_status == "rebuild_pending"
    assert "statistics_version" not in entry.data
    assert len(await stored_rows(ha, STAT_CONSUMPTION)) == 92
    assert await stored_rows(ha, STAT_TOTAL_COST) == []

    await _refresh(ha, coordinator)

    # The partial import loses nothing, so the next attempt rebuilds in full.
    assert coordinator.statistics_status == "rebuilt"
    assert entry.data["statistics_version"] == 2
    for statistic_id in ALL_STATISTIC_IDS:
        assert len(await stored_rows(ha, statistic_id)) == 92, statistic_id
