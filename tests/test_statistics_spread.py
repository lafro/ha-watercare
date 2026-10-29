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
    price_bill,
    pricing_date,
    split_evenly,
    spread_period_days,
)
from custom_components.watercare.tariffs import (
    LATEST_PUBLISHED_YEAR,
    PUBLISHED_TARIFFS,
    Tariff,
    TariffSchedule,
    days_in_financial_year,
)

from .common import api_period

RATIO = Decimal("0.785")
PUBLISHED = TariffSchedule({})
# Made-up prices, for 2022/23 to 2024/25 (2023/24 contains 29 February).
SYNTHETIC = Tariff.of("2.345", "3.456", "300.00")
SYNTHETIC_PRICES = TariffSchedule(dict.fromkeys((2022, 2023, 2024), SYNTHETIC))
NO_COST = DailyCost(Decimal(0), Decimal(0), Decimal(0))


def _period(
    start: date, end: date, usage: int, days: int | None = None
) -> BillingPeriod:
    period = BillingPeriod.from_json(api_period(start, end, usage, days=days))
    assert period is not None
    return period


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _days_cost(days: list[DailyUsage], schedule: TariffSchedule) -> DailyCost:
    return sum((day_cost(usage, schedule, RATIO) for usage in days), NO_COST)


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
    # The fixed charge is for Watercare's 32 days, spread over 31 days.
    july_cost = bill_cost(july, PUBLISHED, RATIO)
    june_cost = bill_cost(june, PUBLISHED, RATIO)
    assert july_cost is not None
    assert june_cost is not None
    assert july_cost.fixed == Decimal("31.20")  # 355.90 / 365 x 32
    july_day = next(usage for usage in days if usage.day == date(2026, 8, 1))
    assert abs(
        day_cost(july_day, PUBLISHED, RATIO).fixed - july_cost.fixed / 31
    ) < Decimal("1e-8")
    assert _days_cost(days, PUBLISHED) == june_cost + july_cost


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
    # The shared day carries each bill's own share of its cost.
    june_cost = bill_cost(june, PUBLISHED, RATIO)
    odd_cost = bill_cost(odd, PUBLISHED, RATIO)
    assert june_cost is not None
    assert odd_cost is not None
    assert _days_cost(days, PUBLISHED) == june_cost + odd_cost


def test_single_day_bill() -> None:
    period = _period(date(2026, 7, 1), date(2026, 7, 1), 1000)
    assert daily_usage([period]) == [
        DailyUsage(
            date(2026, 7, 1),
            (BillShare(date(2026, 7, 1), Decimal(1000), period, 0, 1),),
        )
    ]


def test_bill_shares_add_up_to_each_bill() -> None:
    june = _period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    july = _period(date(2026, 7, 4), date(2026, 8, 3), 8000, days=33)

    shares = bill_shares([june, july])

    for period in (june, july):
        own = [share for share in shares if share.bill == (period.start, period.end)]
        assert sum(share.litres for share in own) == period.usage_litres
        assert {share.priced_on for share in own} == {period.start}
        tariff = PUBLISHED.for_day(period.start)
        assert tariff is not None
        own_cost = sum((daily_cost(share, tariff, RATIO) for share in own), NO_COST)
        assert own_cost == bill_cost(period, PUBLISHED, RATIO)
    # The fixed charge counts Watercare's 33 days, not the 31 between the dates.
    july_cost = bill_cost(july, PUBLISHED, RATIO)
    assert july_cost is not None
    assert july_cost.fixed == Decimal("32.18")  # 355.90 / 365 x 33


def test_daily_cost_is_an_even_share_of_each_rounded_line() -> None:
    period = _period(date(2024, 8, 1), date(2024, 8, 30), 12000)
    bill = bill_cost(period, SYNTHETIC_PRICES, RATIO)
    assert bill is not None

    costs = [daily_cost(share, SYNTHETIC, RATIO) for share in bill_shares([period])]

    assert len(costs) == 30
    assert sum(costs, NO_COST) == bill
    for line in ("water", "wastewater", "fixed"):
        parts = [getattr(cost, line) for cost in costs]
        assert max(parts) - min(parts) <= Decimal("0.000000001")
    cost = costs[0]
    assert cost.total == cost.water + cost.wastewater + cost.fixed
    assert cost.part("water") == cost.water
    assert cost.part("wastewater") == cost.wastewater
    assert cost.part("total") == cost.total
    assert cost + cost == DailyCost(cost.water * 2, cost.wastewater * 2, cost.fixed * 2)


def test_bill_cost_is_the_sum_of_its_lines_rounded_to_the_cent() -> None:
    # 12 kL over 30 days, all in 2025/26.
    period = _period(date(2025, 8, 3), date(2025, 9, 1), 12000)
    assert period.number_of_days == 30

    cost = bill_cost(period, PUBLISHED, RATIO)

    assert cost == DailyCost(
        water=Decimal("27.55"),  # 12 x 2.296 = 27.552
        wastewater=Decimal("37.62"),  # 9.42 kL x 3.994 = 37.62348
        fixed=Decimal("27.29"),  # 332 / 365 x 30 = 27.2877
    )
    assert cost.total == Decimal("92.46")


def test_bill_total_adds_the_rounded_lines() -> None:
    # 12 kL over 30 days in 2024/25 at made-up prices: 28.14 + 32.56 + 24.66.
    # Rounding the unrounded sum, 28.14 + 32.55552 + 24.6575, gives 85.35.
    period = _period(date(2024, 8, 1), date(2024, 8, 30), 12000)

    cost = bill_cost(period, SYNTHETIC_PRICES, RATIO)

    assert cost == DailyCost(Decimal("28.14"), Decimal("32.56"), Decimal("24.66"))
    assert cost.total == Decimal("85.36")


@pytest.mark.parametrize(
    ("kilolitres", "wastewater"),
    [
        # 9 x 0.785 = 7.065 kL -> 7.06 kL x 3.456 = 24.40 (half up: 24.43).
        pytest.param(9, Decimal("24.40"), id="7.065 kL rounds down"),
        # 11 x 0.785 = 8.635 kL -> 8.64 kL x 3.456 = 29.86 (rounding down: 29.83).
        pytest.param(11, Decimal("29.86"), id="8.635 kL rounds up"),
    ],
)
def test_wastewater_volume_rounds_half_to_even(
    kilolitres: int, wastewater: Decimal
) -> None:
    period = _period(date(2024, 8, 1), date(2024, 8, 30), kilolitres * 1000)

    cost = bill_cost(period, SYNTHETIC_PRICES, RATIO)

    assert cost is not None
    assert cost.wastewater == wastewater


def test_fixed_charge_divides_by_the_days_of_its_financial_year() -> None:
    assert days_in_financial_year(2023) == 366  # contains 29 February 2024
    assert days_in_financial_year(2024) == 365
    assert days_in_financial_year(2027) == 366  # contains 29 February 2028
    assert days_in_financial_year(2099) == 365  # 2100 is not a leap year
    leap = _period(date(2024, 2, 15), date(2024, 3, 15), 0)
    plain = _period(date(2024, 8, 1), date(2024, 8, 30), 0)
    # Priced in 2022/23, so 365 days, though the bill runs into 2023/24.
    spanning = _period(date(2023, 6, 16), date(2023, 7, 15), 0)

    costs = [
        bill_cost(period, SYNTHETIC_PRICES, RATIO) for period in (leap, plain, spanning)
    ]

    assert [period.number_of_days for period in (leap, plain, spanning)] == [30] * 3
    assert [cost.fixed if cost else None for cost in costs] == [
        Decimal("24.59"),  # 300 / 366 x 30 = 24.590
        Decimal("24.66"),  # 300 / 365 x 30 = 24.658
        Decimal("24.66"),
    ]


def test_bill_spanning_1_july_is_priced_at_its_start_year() -> None:
    # 20 June to 19 July 2025: 11 days in 2024/25, 19 days in 2025/26.
    period = _period(date(2025, 6, 20), date(2025, 7, 19), 12000)
    assert pricing_date(period) == date(2025, 6, 20)

    cost = bill_cost(period, PUBLISHED, RATIO)

    old = PUBLISHED_TARIFFS[2024]
    assert cost == price_bill(period, old, RATIO)
    assert cost.water == Decimal("25.70")  # 12 x 2.142
    assert cost.fixed == Decimal("25.48")  # 310 / 365 x 30
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
    assert bill_cost(future, PUBLISHED, RATIO) is None


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

    assert bill_cost(period, PUBLISHED, RATIO) is not None
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
