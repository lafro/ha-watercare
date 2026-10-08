"""Tests for spreading bills over days and pricing each bill (no recorder)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from custom_components.watercare.models import BillingPeriod
from custom_components.watercare.statistics import (
    BillShare,
    DailyCost,
    DailyUsage,
    MissingTariffError,
    bill_cost,
    bill_shares,
    daily_cost,
    daily_usage,
    day_cost,
    day_start,
    pricing_date,
    split_evenly,
    spread_period_days,
)
from custom_components.watercare.tariffs import (
    LATEST_PUBLISHED_YEAR,
    PUBLISHED_TARIFFS,
    Tariff,
    TariffSchedule,
)

from .common import api_period

RATIO = Decimal("0.785")
PUBLISHED = TariffSchedule({})


def _period(
    start: date, end: date, usage: int, days: int | None = None
) -> BillingPeriod:
    period = BillingPeriod.from_json(api_period(start, end, usage, days=days))
    assert period is not None
    return period


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _flat(kilolitres: int, days: int, tariff: Tariff) -> Decimal:
    """Price a whole bill at one tariff, the way Watercare's bill does."""
    litres = Decimal(kilolitres)
    return (
        litres * tariff.water_rate
        + litres * RATIO * tariff.wastewater_rate
        + tariff.fixed_charge / 365 * days
    )


def _shares(usage: DailyUsage) -> tuple[Decimal, Decimal]:
    return (
        sum((share.litres for share in usage.shares), Decimal(0)),
        sum((share.fixed_charge_days for share in usage.shares), Decimal(0)),
    )


def test_contiguous_bills_cover_their_own_days() -> None:
    june = _period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    july = _period(date(2026, 7, 4), date(2026, 8, 3), 8000)

    spread = spread_period_days([july, june])

    assert spread == [
        (june, _days(date(2026, 6, 3), date(2026, 7, 3))),
        (july, _days(date(2026, 7, 4), date(2026, 8, 3))),
    ]
    days = daily_usage([june, july])
    assert [usage.day for usage in days] == _days(date(2026, 6, 3), date(2026, 8, 3))
    assert sum(usage.litres for usage in days) == Decimal(20000)
    assert abs(days[0].litres - Decimal(12000) / 31) < Decimal("1e-8")
    assert abs(days[-1].litres - Decimal(8000) / 31) < Decimal("1e-8")
    assert all(_shares(usage)[1] == 1 for usage in days)
    assert all(len(usage.shares) == 1 for usage in days)


def test_bills_that_share_a_boundary_day_never_overlap() -> None:
    june = _period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    # Starts on the previous bill's end date; Watercare still counts 32 days.
    july = _period(date(2026, 7, 3), date(2026, 8, 3), 8000)

    spread = spread_period_days([june, july])

    assert spread[1] == (july, _days(date(2026, 7, 4), date(2026, 8, 3)))
    days = daily_usage([june, july])
    assert len({usage.day for usage in days}) == len(days)
    assert sum(usage.litres for usage in days) == Decimal(20000)
    july_day = next(usage for usage in days if usage.day == date(2026, 8, 1))
    # 32 billing days of fixed charge over 31 spread days.
    assert abs(_shares(july_day)[1] - Decimal(32) / 31) < Decimal("1e-8")
    assert sum(_shares(usage)[1] for usage in days) == 31 + 32


def test_gap_between_bills_gets_no_rows() -> None:
    june = _period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    august = _period(date(2026, 8, 1), date(2026, 8, 31), 6200)

    days = {usage.day for usage in daily_usage([june, august])}

    assert date(2026, 7, 20) not in days
    assert date(2026, 8, 1) in days


def test_after_moves_a_new_bill_past_the_stored_days() -> None:
    july = _period(date(2026, 6, 28), date(2026, 8, 3), 8000)

    days = daily_usage([july], after=date(2026, 7, 3))

    assert days[0].day == date(2026, 7, 4)
    assert days[-1].day == date(2026, 8, 3)
    assert sum(usage.litres for usage in days) == Decimal(8000)
    # Still priced as the bill Watercare issued: from its own start date.
    assert {share.priced_on for usage in days for share in usage.shares} == {
        date(2026, 6, 28)
    }


def test_after_leaves_already_stored_bills_alone() -> None:
    june = _period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    july = _period(date(2026, 7, 4), date(2026, 8, 3), 8000)

    assert daily_usage([june, july], after=date(2026, 7, 3)) == daily_usage(
        [june, july]
    )


def test_overlapping_bills_keep_their_total_and_their_own_shares() -> None:
    june = _period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    # A second bill with the same end date: it lands on that end date.
    odd = _period(date(2026, 7, 1), date(2026, 7, 3), 500)

    spread = spread_period_days([june, odd])
    days = daily_usage([june, odd])

    assert spread[1] == (odd, [date(2026, 7, 3)])
    assert sum(usage.litres for usage in days) == Decimal(12500)
    assert abs(days[-1].litres - (Decimal(12000) / 31 + 500)) < Decimal("1e-8")
    assert [share.bill for share in days[-1].shares] == [
        (date(2026, 6, 3), date(2026, 7, 3)),
        (date(2026, 7, 1), date(2026, 7, 3)),
    ]
    # Each bill's cost counts only its own shares of the shared day.
    june_cost = bill_cost(june, [june, odd], PUBLISHED, RATIO)
    assert june_cost is not None
    assert round(june_cost.total, 9) == round(_flat(12, 31, PUBLISHED_TARIFFS[2025]), 9)


def test_single_day_bill() -> None:
    period = _period(date(2026, 7, 1), date(2026, 7, 1), 1000)
    key = (date(2026, 7, 1), date(2026, 7, 1))
    assert daily_usage([period]) == [
        DailyUsage(
            date(2026, 7, 1),
            (BillShare(date(2026, 7, 1), Decimal(1000), Decimal(1), key, key[0]),),
        )
    ]


def test_bill_shares_add_up_to_each_bill() -> None:
    june = _period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    july = _period(date(2026, 7, 4), date(2026, 8, 3), 8000, days=33)

    shares = bill_shares([june, july])

    for period in (june, july):
        own = [share for share in shares if share.bill == (period.start, period.end)]
        assert sum(share.litres for share in own) == period.usage_litres
        assert sum(share.fixed_charge_days for share in own) == period.number_of_days
        assert {share.priced_on for share in own} == {period.start}


def test_daily_cost_components() -> None:
    tariff = PUBLISHED_TARIFFS[2026]
    key = (date(2026, 7, 1), date(2026, 7, 31))
    share = BillShare(date(2026, 7, 1), Decimal(1000), Decimal(2), key, key[0])

    cost = daily_cost(share, tariff, RATIO)

    assert cost == DailyCost(
        water=Decimal("2.46"),
        wastewater=Decimal("0.785") * Decimal("4.28"),
        fixed=Decimal("355.90") / 365 * 2,
    )
    assert cost.total == cost.water + cost.wastewater + cost.fixed
    assert cost.part("water") == cost.water
    assert cost.part("wastewater") == cost.wastewater
    assert cost.part("total") == cost.total
    assert cost + cost == DailyCost(cost.water * 2, cost.wastewater * 2, cost.fixed * 2)


def test_bill_cost_within_one_year_is_the_flat_bill_formula() -> None:
    # 12 kL over 30 days, all in 2025/26:
    # 12 x 2.296 + 12 x 0.785 x 3.994 + 332 / 365 x 30 = NZD 92.46.
    period = _period(date(2025, 8, 3), date(2025, 9, 1), 12000)
    assert period.number_of_days == 30

    cost = bill_cost(period, [period], PUBLISHED, RATIO)

    assert cost is not None
    assert round(cost.total, 2) == Decimal("92.46")
    assert round(cost.total, 9) == round(_flat(12, 30, PUBLISHED_TARIFFS[2025]), 9)


def test_bill_spanning_1_july_is_priced_at_its_start_year() -> None:
    # 20 June to 19 July 2025: 11 days in 2024/25, 19 days in 2025/26.
    period = _period(date(2025, 6, 20), date(2025, 7, 19), 12000)
    assert pricing_date(period) == date(2025, 6, 20)

    cost = bill_cost(period, [period], PUBLISHED, RATIO)

    assert cost is not None
    old = PUBLISHED_TARIFFS[2024]
    assert round(cost.total, 9) == round(_flat(12, 30, old), 9)
    assert round(cost.water, 9) == round(Decimal(12) * old.water_rate, 9)
    assert round(cost.fixed, 9) == round(old.fixed_charge / 365 * 30, 9)
    # The daily rows of the July days carry the same, earlier prices.
    july_day = next(
        usage for usage in daily_usage([period]) if usage.day == date(2025, 7, 10)
    )
    assert day_cost(july_day, PUBLISHED, RATIO) == daily_cost(
        july_day.shares[0], old, RATIO
    )


def test_bill_cost_is_unknown_without_a_tariff() -> None:
    future = _period(
        date(LATEST_PUBLISHED_YEAR + 1, 7, 3),
        date(LATEST_PUBLISHED_YEAR + 1, 8, 2),
        10000,
    )
    assert bill_cost(future, [future], PUBLISHED, RATIO) is None
    other = _period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    assert bill_cost(other, [future], PUBLISHED, RATIO) is None


def test_day_cost_names_the_earliest_missing_year() -> None:
    first = LATEST_PUBLISHED_YEAR + 1
    late = _period(date(first + 1, 7, 3), date(first + 1, 8, 2), 4000)
    # Ends on the same day, so it shares that day with the later bill.
    early = _period(date(first, 7, 3), date(first + 1, 8, 2), 7000)

    shared = daily_usage([late, early])[-1]

    assert len(shared.shares) == 2
    with pytest.raises(MissingTariffError) as raised:
        day_cost(shared, PUBLISHED, RATIO)
    assert raised.value.year == first


def test_a_cosmetic_field_of_any_shape_never_breaks_pricing() -> None:
    item = api_period(date(2026, 7, 4), date(2026, 8, 3), 8000)
    item["statistics"]["efficiency"] = {
        "currentHouseholdBand": ["B", 2],
        "usageToLowerBand": {"litres": 25},
    }
    period = BillingPeriod.from_json(item)
    assert period is not None

    assert bill_cost(period, [period], PUBLISHED, RATIO) is not None
    assert hash(period) == hash(_period(date(2026, 7, 4), date(2026, 8, 3), 8000))


def test_day_start_is_auckland_midnight_across_daylight_saving() -> None:
    assert day_start(date(2026, 7, 3)) == datetime(2026, 7, 2, 12, tzinfo=UTC)
    # Daylight saving starts 27 Sep 2026 and ends 4 Apr 2027.
    assert day_start(date(2026, 9, 27)) == datetime(2026, 9, 26, 12, tzinfo=UTC)
    assert day_start(date(2026, 9, 28)) == datetime(2026, 9, 27, 11, tzinfo=UTC)
    assert day_start(date(2027, 4, 4)) == datetime(2027, 4, 3, 11, tzinfo=UTC)
    assert day_start(date(2027, 4, 5)) == datetime(2027, 4, 4, 12, tzinfo=UTC)


def test_split_evenly_adds_up_exactly() -> None:
    parts = split_evenly(Decimal(13000), 31)
    assert len(parts) == 31
    assert sum(parts) == Decimal(13000)
    assert max(parts) - min(parts) <= Decimal("0.000000001")
    assert split_evenly(Decimal(5), 1) == [Decimal(5)]
