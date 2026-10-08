"""Tests for the dated tariff table."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import pairwise

import pytest

from custom_components.watercare.tariffs import (
    FIRST_PUBLISHED_YEAR,
    LATEST_PUBLISHED_YEAR,
    PUBLISHED_TARIFFS,
    Tariff,
    TariffSchedule,
    financial_year,
    financial_year_label,
    matching_published_year,
    schedule_from_options,
    tariff_from_user_input,
)


@pytest.mark.parametrize(
    ("day", "year"),
    [
        (date(2026, 6, 30), 2025),
        (date(2026, 7, 1), 2026),
        (date(2026, 12, 31), 2026),
        (date(2027, 1, 1), 2026),
    ],
)
def test_financial_year(day: date, year: int) -> None:
    assert financial_year(day) == year


def test_financial_year_label() -> None:
    assert financial_year_label(2026) == "2026/27"
    assert financial_year_label(2099) == "2099/00"


def test_published_table_is_contiguous_and_rising() -> None:
    years = sorted(PUBLISHED_TARIFFS)
    assert years == list(range(FIRST_PUBLISHED_YEAR, LATEST_PUBLISHED_YEAR + 1))
    for earlier, later in pairwise(years):
        assert (
            PUBLISHED_TARIFFS[later].water_rate > PUBLISHED_TARIFFS[earlier].water_rate
        )
        assert (
            PUBLISHED_TARIFFS[later].wastewater_rate
            > PUBLISHED_TARIFFS[earlier].wastewater_rate
        )
        assert (
            PUBLISHED_TARIFFS[later].fixed_charge
            > PUBLISHED_TARIFFS[earlier].fixed_charge
        )


def test_published_values_match_the_cited_schedules() -> None:
    # Spot checks against docs/tariffs.md.
    assert PUBLISHED_TARIFFS[2019] == Tariff.of("1.555", "2.704", "225")
    assert PUBLISHED_TARIFFS[2025] == Tariff.of("2.296", "3.994", "332")
    assert PUBLISHED_TARIFFS[2026] == Tariff.of("2.46", "4.28", "355.90")


def test_schedule_lookup_order() -> None:
    custom = Tariff.of("9", "9", "999")
    schedule = TariffSchedule({2026: custom, LATEST_PUBLISHED_YEAR + 1: custom})
    assert schedule.for_year(2026) is custom
    assert schedule.for_year(2024) == PUBLISHED_TARIFFS[2024]
    # Before the table: the earliest published year.
    assert schedule.for_year(2010) == PUBLISHED_TARIFFS[FIRST_PUBLISHED_YEAR]
    assert schedule.for_year(LATEST_PUBLISHED_YEAR + 1) is custom
    # After the table, without figures from the user: unknown.
    assert schedule.for_year(LATEST_PUBLISHED_YEAR + 2) is None
    assert schedule.has_year(LATEST_PUBLISHED_YEAR + 1)
    assert not schedule.has_year(LATEST_PUBLISHED_YEAR + 2)
    assert schedule.for_day(date(2026, 7, 1)) is custom


def test_best_known_walks_back_to_the_latest_known_year() -> None:
    schedule = TariffSchedule({})
    assert (
        schedule.best_known(LATEST_PUBLISHED_YEAR + 3)
        == PUBLISHED_TARIFFS[LATEST_PUBLISHED_YEAR]
    )
    assert schedule.best_known(2020) == PUBLISHED_TARIFFS[2020]
    assert schedule.best_known(1990) == PUBLISHED_TARIFFS[FIRST_PUBLISHED_YEAR]


def test_schedule_from_options_ignores_malformed_entries() -> None:
    good = {"water_rate": 3.0, "wastewater_rate": 5.0, "fixed_charge": 400.0}
    schedule = schedule_from_options(
        {
            "tariff_overrides": {
                "2027": good,
                "not-a-year": good,
                "2028": {"water_rate": 1.0},
                "2029": {"water_rate": -1, "wastewater_rate": 1, "fixed_charge": 1},
                "2030": {"water_rate": "NaN", "wastewater_rate": 1, "fixed_charge": 1},
                "2031": {"water_rate": "x", "wastewater_rate": 1, "fixed_charge": 1},
                "2032": "not a mapping",
            }
        }
    )
    assert schedule.overrides == {2027: Tariff.of("3.0", "5.0", "400.0")}
    assert schedule_from_options({}).overrides == {}
    assert schedule_from_options({"tariff_overrides": ["x"]}).overrides == {}


def test_tariff_conversions() -> None:
    tariff = tariff_from_user_input(
        {"water_rate": 2.46, "wastewater_rate": 4.28, "fixed_charge": 355.9}
    )
    assert tariff == PUBLISHED_TARIFFS[2026]
    assert tariff.as_options() == {
        "water_rate": 2.46,
        "wastewater_rate": 4.28,
        "fixed_charge": 355.9,
    }


def test_matching_published_year() -> None:
    assert matching_published_year(2.296, 3.994, 332) == 2025
    assert matching_published_year(2.296, 3.994, 332.0) == 2025
    assert matching_published_year(2.3, 3.994, 332) is None
    assert Decimal("355.90") == PUBLISHED_TARIFFS[2026].fixed_charge


def test_tariff_from_user_input_rejects_invalid_figures() -> None:
    with pytest.raises(ValueError, match="Invalid tariff"):
        tariff_from_user_input(
            {"water_rate": -1, "wastewater_rate": 4.28, "fixed_charge": 355.9}
        )
