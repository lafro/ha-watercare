"""Tests for spreading bills over days and pricing each day (no recorder)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from custom_components.watercare.models import BillingPeriod
from custom_components.watercare.statistics import (
    DailyCost,
    DailyUsage,
    bill_cost,
    daily_cost,
    daily_usage,
    day_start,
    split_evenly,
    spread_period_days,
)
from custom_components.watercare.tariffs import PUBLISHED_TARIFFS, TariffSchedule

from .common import api_period

RATIO = Decimal("0.785")


def _period(
    start: date, end: date, usage: int, days: int | None = None
) -> BillingPeriod:
    period = BillingPeriod.from_json(api_period(start, end, usage, days=days))
    assert period is not None
    return period


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def test_contiguous_bills_cover_their_own_days() -> None:
    june = _period(date(2026, 6, 16), date(2026, 7, 16), 10000)
    july = _period(date(2026, 7, 17), date(2026, 8, 16), 9000)

    spread = spread_period_days([july, june])

    assert spread == [
        (june, _days(date(2026, 6, 16), date(2026, 7, 16))),
        (july, _days(date(2026, 7, 17), date(2026, 8, 16))),
    ]
    days = daily_usage([june, july])
    assert [usage.day for usage in days] == _days(date(2026, 6, 16), date(2026, 8, 16))
    assert sum(usage.litres for usage in days) == Decimal(19000)
    assert abs(days[0].litres - Decimal(10000) / 31) < Decimal("1e-8")
    assert abs(days[-1].litres - Decimal(9000) / 31) < Decimal("1e-8")
    assert all(usage.fixed_charge_days == 1 for usage in days)


def test_bills_that_share_a_boundary_day_never_overlap() -> None:
    june = _period(date(2026, 6, 16), date(2026, 7, 16), 10000)
    # Starts on the previous bill's end date; Watercare still counts 32 days.
    july = _period(date(2026, 7, 16), date(2026, 8, 16), 9000)

    spread = dict(spread_period_days([june, july]))

    assert spread[july] == _days(date(2026, 7, 17), date(2026, 8, 16))
    days = daily_usage([june, july])
    assert len({usage.day for usage in days}) == len(days)
    assert sum(usage.litres for usage in days) == Decimal(19000)
    july_day = next(usage for usage in days if usage.day == date(2026, 8, 1))
    # 32 billing days of fixed charge over 31 spread days.
    assert abs(july_day.fixed_charge_days - Decimal(32) / 31) < Decimal("1e-8")
    assert sum(usage.fixed_charge_days for usage in days) == 31 + 32


def test_gap_between_bills_gets_no_rows() -> None:
    june = _period(date(2026, 6, 16), date(2026, 7, 16), 10000)
    august = _period(date(2026, 8, 1), date(2026, 8, 31), 6200)

    days = {usage.day for usage in daily_usage([june, august])}

    assert date(2026, 7, 20) not in days
    assert date(2026, 8, 1) in days


def test_after_moves_a_new_bill_past_the_stored_days() -> None:
    july = _period(date(2026, 7, 10), date(2026, 8, 16), 9000)

    days = daily_usage([july], after=date(2026, 7, 16))

    assert days[0].day == date(2026, 7, 17)
    assert days[-1].day == date(2026, 8, 16)
    assert sum(usage.litres for usage in days) == Decimal(9000)


def test_after_leaves_already_stored_bills_alone() -> None:
    june = _period(date(2026, 6, 16), date(2026, 7, 16), 10000)
    july = _period(date(2026, 7, 17), date(2026, 8, 16), 9000)

    assert daily_usage([june, july], after=date(2026, 7, 16)) == daily_usage(
        [june, july]
    )


def test_overlapping_bills_keep_their_total() -> None:
    june = _period(date(2026, 6, 16), date(2026, 7, 16), 10000)
    # A second bill with the same end date: it lands on that end date.
    odd = _period(date(2026, 7, 1), date(2026, 7, 16), 500)

    spread = dict(spread_period_days([june, odd]))
    days = daily_usage([june, odd])

    assert spread[odd] == [date(2026, 7, 16)]
    assert sum(usage.litres for usage in days) == Decimal(10500)
    assert abs(days[-1].litres - (Decimal(10000) / 31 + 500)) < Decimal("1e-8")


def test_single_day_bill() -> None:
    period = _period(date(2026, 7, 1), date(2026, 7, 1), 1000)
    assert daily_usage([period]) == [
        DailyUsage(date(2026, 7, 1), Decimal(1000), Decimal(1))
    ]


def test_daily_cost_components() -> None:
    tariff = PUBLISHED_TARIFFS[2026]
    usage = DailyUsage(date(2026, 7, 1), Decimal(1000), Decimal(2))

    cost = daily_cost(usage, tariff, RATIO)

    assert cost == DailyCost(
        water=Decimal("2.46"),
        wastewater=Decimal("0.785") * Decimal("4.28"),
        fixed=Decimal("355.90") / 365 * 2,
    )
    assert cost.total == cost.water + cost.wastewater + cost.fixed
    assert cost.part("water") == cost.water
    assert cost.part("wastewater") == cost.wastewater
    assert cost.part("total") == cost.total


def test_bill_cost_matches_the_flat_rate_formula_within_one_year() -> None:
    # 14 kL over 32 days, all in 2025/26: the 1.4.x formula gave NZD 105.14.
    period = _period(date(2025, 8, 1), date(2025, 9, 1), 14000)
    assert period.number_of_days == 32

    cost = bill_cost(period, [period], TariffSchedule({}), RATIO)

    assert cost is not None
    assert round(cost.total, 2) == Decimal("105.14")


def test_bill_cost_spanning_1_july_uses_both_years() -> None:
    period = _period(date(2026, 6, 16), date(2026, 7, 16), 10000)

    cost = bill_cost(period, [period], TariffSchedule({}), RATIO)

    old = PUBLISHED_TARIFFS[2025]
    new = PUBLISHED_TARIFFS[2026]
    per_day_kl = Decimal(10) / 31
    expected_water = per_day_kl * (15 * old.water_rate + 16 * new.water_rate)
    expected_fixed = (15 * old.fixed_charge + 16 * new.fixed_charge) / 365
    assert cost is not None
    assert round(cost.water, 10) == round(expected_water, 10)
    assert round(cost.fixed, 10) == round(expected_fixed, 10)
    assert round(cost.total, 2) == Decimal("85.56")
    # The same bill at 2025/26 prices only (what 1.4.x showed) was NZD 82.51.


def test_bill_cost_is_unknown_without_a_tariff() -> None:
    future = _period(date(2099, 6, 16), date(2099, 7, 16), 10000)
    assert bill_cost(future, [future], TariffSchedule({}), RATIO) is None
    other = _period(date(2026, 6, 16), date(2026, 7, 16), 10000)
    assert bill_cost(other, [future], TariffSchedule({}), RATIO) is None


def test_day_start_is_auckland_midnight_across_daylight_saving() -> None:
    assert day_start(date(2026, 7, 16)) == datetime(2026, 7, 15, 12, tzinfo=UTC)
    # Daylight saving starts 27 Sep 2026 and ends 4 Apr 2027.
    assert day_start(date(2026, 9, 27)) == datetime(2026, 9, 26, 12, tzinfo=UTC)
    assert day_start(date(2026, 9, 28)) == datetime(2026, 9, 27, 11, tzinfo=UTC)
    assert day_start(date(2027, 4, 4)) == datetime(2027, 4, 3, 11, tzinfo=UTC)
    assert day_start(date(2027, 4, 5)) == datetime(2027, 4, 4, 12, tzinfo=UTC)


def test_split_evenly_adds_up_exactly() -> None:
    parts = split_evenly(Decimal(11000), 31)
    assert len(parts) == 31
    assert sum(parts) == Decimal(11000)
    assert max(parts) - min(parts) <= Decimal("0.000000001")
    assert split_evenly(Decimal(5), 1) == [Decimal(5)]
