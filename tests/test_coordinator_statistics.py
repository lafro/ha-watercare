"""Tests for WatercareCoordinator._generate_statistics.

Real bug history: the consumption statistic was missing unit_class, which
made it invisible in the Energy Dashboard's water picker (that picker
filters on unit_class). This is asserted explicitly below.

async_add_external_statistics is mocked so these tests don't need a live
recorder/database -- we only need to see what would have been written.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import UnitOfVolume

from custom_components.watercare.const import DOMAIN
from tests.helpers import REAL_TARIFF, make_coordinator

NZ = ZoneInfo("Pacific/Auckland")

# Three billing periods, deliberately given out of chronological order, to
# prove _generate_statistics sorts them itself rather than trusting input
# order. All three fall in NZ Standard Time (no daylight saving in
# May/June/July), so the expected UTC offset is a plain +12:00.
JUNE = {
    "billingPeriodToDate": "2026-06-30T00:00:00.000Z",
    "billingPeriodFromDate": "2026-06-01T00:00:00.000Z",
    "waterUsage": 10000,
    "statistics": {"numberOfDays": 30},
}
MAY = {
    "billingPeriodToDate": "2026-05-31T00:00:00.000Z",
    "billingPeriodFromDate": "2026-05-01T00:00:00.000Z",
    "waterUsage": 9000,
    "statistics": {"numberOfDays": 31},
}
JULY = {
    "billingPeriodToDate": "2026-07-31T00:00:00.000Z",
    "billingPeriodFromDate": "2026-07-01T00:00:00.000Z",
    "waterUsage": 11000,
    "statistics": {"numberOfDays": 31},
}
UNORDERED_PERIODS = [JUNE, MAY, JULY]


def _calls_by_statistic_id(mock_add_stats) -> dict[str, tuple]:
    """Map statistic_id -> (metadata, statistics) from the mocked calls."""
    result = {}
    for call in mock_add_stats.call_args_list:
        _hass, metadata, statistics = call.args
        result[metadata["statistic_id"]] = (metadata, list(statistics))
    return result


async def test_consumption_is_a_correct_running_cumulative_sum(hass) -> None:
    coordinator = make_coordinator(hass, **REAL_TARIFF)

    with patch(
        "custom_components.watercare.coordinator.async_add_external_statistics"
    ) as mock_add_stats:
        await coordinator._generate_statistics(UNORDERED_PERIODS)

    calls = _calls_by_statistic_id(mock_add_stats)
    _metadata, stats = calls[f"{DOMAIN}:water_consumption"]

    assert len(stats) == 3
    # Chronological order (May, June, July) regardless of input order, and
    # each entry's "sum" is the running total, not the period's own usage.
    assert [s["sum"] for s in stats] == [9000, 19000, 30000]


async def test_consumption_start_timestamps_are_nz_localised(hass) -> None:
    coordinator = make_coordinator(hass, **REAL_TARIFF)

    with patch(
        "custom_components.watercare.coordinator.async_add_external_statistics"
    ) as mock_add_stats:
        await coordinator._generate_statistics(UNORDERED_PERIODS)

    calls = _calls_by_statistic_id(mock_add_stats)
    _metadata, stats = calls[f"{DOMAIN}:water_consumption"]

    expected_starts = [
        datetime_utc.astimezone(NZ)
        for datetime_utc in (
            datetime(2026, 5, 31, tzinfo=timezone.utc),
            datetime(2026, 6, 30, tzinfo=timezone.utc),
            datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
    ]

    actual_starts = [s["start"] for s in stats]
    assert actual_starts == expected_starts
    # Belt and braces: NZ Standard Time really is UTC+12, not left naive or
    # still in UTC.
    for start in actual_starts:
        assert start.utcoffset() == timedelta(hours=12)
        assert start.tzinfo is not None


async def test_consumption_metadata_has_unit_class_volume(hass) -> None:
    """Without unit_class="volume" this statistic never appears in the
    Energy Dashboard's water picker -- a real bug that shipped once."""
    coordinator = make_coordinator(hass, **REAL_TARIFF)

    with patch(
        "custom_components.watercare.coordinator.async_add_external_statistics"
    ) as mock_add_stats:
        await coordinator._generate_statistics(UNORDERED_PERIODS)

    calls = _calls_by_statistic_id(mock_add_stats)
    metadata, _stats = calls[f"{DOMAIN}:water_consumption"]

    assert metadata["unit_class"] == "volume"
    assert metadata["unit_of_measurement"] == UnitOfVolume.LITERS
    assert metadata["has_sum"] is True


async def test_cost_metadata_has_no_unit_class(hass) -> None:
    """Cost is money, not a physical quantity -- it must not claim a
    unit_class (that would make the Energy Dashboard try to convert it)."""
    coordinator = make_coordinator(hass, **REAL_TARIFF)

    with patch(
        "custom_components.watercare.coordinator.async_add_external_statistics"
    ) as mock_add_stats:
        await coordinator._generate_statistics(UNORDERED_PERIODS)

    calls = _calls_by_statistic_id(mock_add_stats)
    metadata, stats = calls[f"{DOMAIN}:water_cost"]

    assert metadata["unit_class"] is None
    assert metadata["unit_of_measurement"] == "NZD"

    # The cost running sum must accumulate the *same* per-period totals that
    # _calculate_cost produces (tested independently in
    # test_coordinator_cost.py) -- i.e. _generate_statistics's accumulation
    # loop, not its cost formula, is what's under test here.
    expected_total = 0.0
    expected_running_sums = []
    for period, days in ((MAY, 31), (JUNE, 30), (JULY, 31)):
        expected_total += coordinator._calculate_cost(period["waterUsage"], days)[
            "total"
        ]
        expected_running_sums.append(expected_total)

    assert [s["sum"] for s in stats] == pytest.approx(expected_running_sums)


async def test_no_billing_periods_writes_nothing(hass) -> None:
    coordinator = make_coordinator(hass, **REAL_TARIFF)

    with patch(
        "custom_components.watercare.coordinator.async_add_external_statistics"
    ) as mock_add_stats:
        await coordinator._generate_statistics([])

    mock_add_stats.assert_not_called()


async def test_zero_rate_suppresses_its_own_cost_statistic_only(hass) -> None:
    """wastewater_rate=0 must skip the wastewater_cost statistic but still
    emit consumption and water_cost."""
    coordinator = make_coordinator(
        hass,
        consumption_rate=2.296,
        wastewater_rate=0,
        wastewater_ratio=0.785,
        annual_line_charge=332,
    )

    with patch(
        "custom_components.watercare.coordinator.async_add_external_statistics"
    ) as mock_add_stats:
        await coordinator._generate_statistics(UNORDERED_PERIODS)

    calls = _calls_by_statistic_id(mock_add_stats)
    assert f"{DOMAIN}:water_consumption" in calls
    assert f"{DOMAIN}:water_cost" in calls
    assert f"{DOMAIN}:consumption_cost" in calls
    assert f"{DOMAIN}:wastewater_cost" not in calls
