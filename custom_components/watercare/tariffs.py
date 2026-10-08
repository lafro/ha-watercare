"""Watercare residential tariffs by financial year.

Watercare reprices on 1 July. Costs are worked out per day with the tariff in
force on that day, so a bill that spans 1 July is apportioned between the two
years by day, and past bills keep the prices that applied at the time.

Every published figure below is GST-inclusive, as printed on the bill and in
Watercare's annual price schedules. docs/tariffs.md lists the source for each
year. Watercare has no API for prices, so a new financial year needs either a
new release of this table or the user's own figures in the integration
options.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from .const import (
    CONF_FIXED_CHARGE,
    CONF_TARIFF_OVERRIDES,
    CONF_WASTEWATER_RATE,
    CONF_WATER_RATE,
)

DAYS_PER_YEAR: Final = Decimal(365)


@dataclass(frozen=True, slots=True)
class Tariff:
    """Residential prices for one financial year, GST-inclusive (NZD)."""

    water_rate: Decimal
    """Water supply, per kilolitre of metered water."""
    wastewater_rate: Decimal
    """Wastewater, per kilolitre of wastewater (a share of metered water)."""
    fixed_charge: Decimal
    """Wastewater fixed charge per meter, per year."""

    @classmethod
    def of(cls, water: str, wastewater: str, fixed: str) -> Tariff:
        """Build a tariff from decimal strings."""
        return cls(Decimal(water), Decimal(wastewater), Decimal(fixed))

    def as_options(self) -> dict[str, float]:
        """Return the tariff as config-entry option values."""
        return {
            CONF_WATER_RATE: float(self.water_rate),
            CONF_WASTEWATER_RATE: float(self.wastewater_rate),
            CONF_FIXED_CHARGE: float(self.fixed_charge),
        }


# Keyed by the calendar year in which the financial year starts (1 July).
PUBLISHED_TARIFFS: Final[Mapping[int, Tariff]] = {
    2018: Tariff.of("1.517", "2.618", "218"),
    2019: Tariff.of("1.555", "2.704", "225"),
    2020: Tariff.of("1.594", "2.772", "231"),
    2021: Tariff.of("1.706", "2.966", "247"),
    2022: Tariff.of("1.825", "3.174", "264"),
    2023: Tariff.of("1.998", "3.476", "289.00"),
    2024: Tariff.of("2.142", "3.726", "310.00"),
    2025: Tariff.of("2.296", "3.994", "332.00"),
    2026: Tariff.of("2.46", "4.28", "355.90"),
}
FIRST_PUBLISHED_YEAR: Final = min(PUBLISHED_TARIFFS)
LATEST_PUBLISHED_YEAR: Final = max(PUBLISHED_TARIFFS)


def financial_year(day: date) -> int:
    """Return the financial year (by its starting calendar year) of a date."""
    return day.year if day.month >= 7 else day.year - 1  # noqa: PLR2004 - July


def financial_year_label(year: int) -> str:
    """Return the label Watercare uses for a financial year, e.g. 2026/27."""
    return f"{year}/{(year + 1) % 100:02d}"


@dataclass(frozen=True, slots=True)
class TariffSchedule:
    """Published tariffs plus any per-year figures the user entered."""

    overrides: Mapping[int, Tariff]

    def for_year(self, year: int) -> Tariff | None:
        """Return the tariff for a financial year, or None when unknown.

        Years before the earliest published year use that year's prices: the
        table starts in 2018/19, which covers every account seen so far.
        Years after the latest published year have no price until the user
        enters one or a release adds it.
        """
        if (override := self.overrides.get(year)) is not None:
            return override
        if (published := PUBLISHED_TARIFFS.get(year)) is not None:
            return published
        if year < FIRST_PUBLISHED_YEAR:
            return PUBLISHED_TARIFFS[FIRST_PUBLISHED_YEAR]
        return None

    def for_day(self, day: date) -> Tariff | None:
        """Return the tariff in force on a day."""
        return self.for_year(financial_year(day))

    def best_known(self, year: int) -> Tariff:
        """Return the tariff for a year, or the latest known one before it."""
        candidate = year
        while candidate >= FIRST_PUBLISHED_YEAR:
            if (tariff := self.for_year(candidate)) is not None:
                return tariff
            candidate -= 1
        return PUBLISHED_TARIFFS[FIRST_PUBLISHED_YEAR]

    def has_year(self, year: int) -> bool:
        """Return whether a year has a published or user-entered tariff."""
        return self.for_year(year) is not None


def schedule_from_options(options: Mapping[str, Any]) -> TariffSchedule:
    """Build a schedule from the stored per-year overrides.

    Malformed entries are ignored rather than failing setup.
    """
    raw = options.get(CONF_TARIFF_OVERRIDES)
    overrides: dict[int, Tariff] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            tariff = _tariff_from_mapping(value)
            try:
                year = int(key)
            except TypeError, ValueError:
                continue
            if tariff is not None:
                overrides[year] = tariff
    return TariffSchedule(overrides)


def _tariff_from_mapping(value: Any) -> Tariff | None:
    if not isinstance(value, Mapping):
        return None
    try:
        tariff = Tariff(
            Decimal(str(value[CONF_WATER_RATE])),
            Decimal(str(value[CONF_WASTEWATER_RATE])),
            Decimal(str(value[CONF_FIXED_CHARGE])),
        )
    except KeyError, InvalidOperation:
        return None
    if any(
        not part.is_finite() or part < 0
        for part in (tariff.water_rate, tariff.wastewater_rate, tariff.fixed_charge)
    ):
        return None
    return tariff


def tariff_from_user_input(value: Mapping[str, Any]) -> Tariff:
    """Build a tariff from validated form input."""
    tariff = _tariff_from_mapping(value)
    if tariff is None:
        raise ValueError("Invalid tariff input")
    return tariff


def matching_published_year(
    water_rate: float, wastewater_rate: float, fixed_charge: float
) -> int | None:
    """Return the published year whose prices equal the given figures."""
    candidate = Tariff(
        Decimal(str(water_rate)),
        Decimal(str(wastewater_rate)),
        Decimal(str(fixed_charge)),
    )
    for year, tariff in PUBLISHED_TARIFFS.items():
        if tariff == candidate:
            return year
    return None
