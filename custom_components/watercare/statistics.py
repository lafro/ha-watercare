"""Long-term statistics for the Energy dashboard.

The 1.5.0 model (docs/statistics.md has the reasoning):

* **Daily rows.** A bill covers about a month and HA statistics are hourly
  buckets. Each bill's volume is spread evenly over the days it covers, one
  row per Auckland day, instead of one spike after the period ends.
* **Anchored sums.** A poll only adds rows for days after the last stored
  row, continuing that row's running sum. Stored history is never rewritten
  by a normal poll, and a shorter API response cannot step the sum down.
* **Costs at the tariff in force.** Each day is priced with that day's
  financial-year tariff (tariffs.py). Cost rows stop at the first day that
  has no known tariff and resume once one is available, so a missing price
  never becomes a wrong one.
* **One-off rebuild.** Entries upgraded from 1.4.x hold one row per bill,
  priced at a single flat tariff. ``async_rebuild`` clears the four
  statistics and imports the full history in the new format, once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Final, Literal

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import VolumeConverter

from .const import (
    ALL_STATISTIC_IDS,
    DOMAIN,
    NZ_TIMEZONE,
    STAT_CONSUMPTION,
    STAT_CONSUMPTION_COST,
    STAT_TOTAL_COST,
    STAT_WASTEWATER_COST,
)
from .models import BillingPeriod
from .tariffs import DAYS_PER_YEAR, Tariff, TariffSchedule

_LOGGER = logging.getLogger(__name__)

_LITRES_PER_KILOLITRE: Final = Decimal(1000)
_ONE_DAY: Final = timedelta(days=1)
_PRECISION: Final = Decimal("0.000000001")
_CLEAR_TIMEOUT: Final = 300.0
# Litres. Stored sums are floats; anything above this is a real difference.
_VOLUME_TOLERANCE: Final = Decimal("0.5")

CostKind = Literal["total", "water", "wastewater"]

_COST_STATISTICS: Final[dict[str, tuple[str, CostKind]]] = {
    STAT_TOTAL_COST: ("Watercare Total Cost", "total"),
    STAT_CONSUMPTION_COST: ("Watercare Consumption Cost", "water"),
    STAT_WASTEWATER_COST: ("Watercare Wastewater Cost", "wastewater"),
}


# --- Pure calculations -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DailyUsage:
    """One Auckland day's share of a bill."""

    day: date
    litres: Decimal
    fixed_charge_days: Decimal
    """Billing days this row stands for (1 unless Watercare's day count and
    the spread length differ)."""


@dataclass(frozen=True, slots=True)
class DailyCost:
    """Cost of one day, GST-inclusive (NZD)."""

    water: Decimal
    wastewater: Decimal
    fixed: Decimal

    @property
    def total(self) -> Decimal:
        """Return the total cost of the day."""
        return self.water + self.wastewater + self.fixed

    def part(self, kind: CostKind) -> Decimal:
        """Return one component, as used by a cost statistic."""
        if kind == "water":
            return self.water
        if kind == "wastewater":
            return self.wastewater
        return self.total


def spread_period_days(
    periods: Iterable[BillingPeriod], *, after: date | None = None
) -> list[tuple[BillingPeriod, list[date]]]:
    """Return the days each bill is spread over, oldest bill first.

    A bill covers its start date to its end date inclusive (Watercare counts
    both ends). Bills never share a day: if a bill starts on or before the
    previous bill's end date, it starts the day after instead, so whichever
    way Watercare dates consecutive bills, each day belongs to one bill.

    ``after`` is the last day already stored. A new bill that would reach
    back to or before it is spread over the days after it instead, so its
    whole volume lands in rows that will actually be written.
    """
    ordered = sorted(periods, key=lambda period: (period.end, period.start))
    result: list[tuple[BillingPeriod, list[date]]] = []
    previous_end: date | None = None
    for period in ordered:
        first = period.start
        if previous_end is not None and first <= previous_end:
            first = previous_end + _ONE_DAY
        if after is not None and period.end > after >= first:
            first = after + _ONE_DAY
        first = min(first, period.end)
        count = (period.end - first).days + 1
        result.append((period, [first + _ONE_DAY * offset for offset in range(count)]))
        previous_end = (
            period.end if previous_end is None else max(previous_end, period.end)
        )
    return result


def split_evenly(total: Decimal, parts: int) -> list[Decimal]:
    """Split a total into equal parts that add back up to it exactly.

    Each part is the difference of two cumulative shares rounded to a fixed
    precision, so rounding never accumulates: a bill's rows sum to the bill,
    and sums across bills stay exact.
    """
    shares = [
        (total * index / parts).quantize(_PRECISION) for index in range(parts + 1)
    ]
    return [later - earlier for earlier, later in pairwise(shares)]


def daily_usage(
    periods: Iterable[BillingPeriod], *, after: date | None = None
) -> list[DailyUsage]:
    """Spread every bill evenly over its days; one entry per day, oldest first.

    The sum of all entries equals the sum of all bills.
    """
    litres: dict[date, Decimal] = {}
    fixed_days: dict[date, Decimal] = {}
    for period, days in spread_period_days(periods, after=after):
        usage_shares = split_evenly(Decimal(period.usage_litres), len(days))
        fixed_shares = split_evenly(Decimal(period.number_of_days), len(days))
        for day, usage_share, fixed_share in zip(
            days, usage_shares, fixed_shares, strict=True
        ):
            litres[day] = litres.get(day, Decimal(0)) + usage_share
            fixed_days[day] = fixed_days.get(day, Decimal(0)) + fixed_share
    return [DailyUsage(day, litres[day], fixed_days[day]) for day in sorted(litres)]


def daily_cost(
    usage: DailyUsage, tariff: Tariff, wastewater_ratio: Decimal
) -> DailyCost:
    """Price one day's usage with the tariff in force on that day."""
    kilolitres = usage.litres / _LITRES_PER_KILOLITRE
    return DailyCost(
        water=kilolitres * tariff.water_rate,
        wastewater=kilolitres * wastewater_ratio * tariff.wastewater_rate,
        fixed=tariff.fixed_charge / DAYS_PER_YEAR * usage.fixed_charge_days,
    )


BillCost = DailyCost
"""Cost of one bill: the sum of its days."""


def bill_cost(
    period: BillingPeriod,
    periods: Sequence[BillingPeriod],
    schedule: TariffSchedule,
    wastewater_ratio: Decimal,
) -> BillCost | None:
    """Return the cost of one bill, or None if any of its days has no tariff.

    Uses the same daily rows as the statistics, so the sensor and the Energy
    dashboard always agree.
    """
    spread = dict(spread_period_days(periods))
    if period not in spread:
        return None
    days = set(spread[period])
    total = DailyCost(Decimal(0), Decimal(0), Decimal(0))
    for usage in daily_usage(periods):
        if usage.day not in days:
            continue
        tariff = schedule.for_day(usage.day)
        if tariff is None:
            return None
        cost = daily_cost(usage, tariff, wastewater_ratio)
        total = DailyCost(
            total.water + cost.water,
            total.wastewater + cost.wastewater,
            total.fixed + cost.fixed,
        )
    return total


def day_start(day: date) -> datetime:
    """Return the UTC start of an Auckland day (a whole hour in UTC)."""
    return datetime.combine(day, time.min, tzinfo=NZ_TIMEZONE).astimezone(UTC)


# --- Recorder ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What an import wrote."""

    consumption_rows: int = 0
    cost_rows: int = 0
    first_day_without_tariff: date | None = None
    rebuilt: bool = False


@dataclass(frozen=True, slots=True)
class _Stored:
    start: datetime
    total: Decimal


def _consumption_metadata() -> StatisticMetaData:
    return {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": "Watercare Water Consumption",
        "source": DOMAIN,
        "statistic_id": STAT_CONSUMPTION,
        # The Energy dashboard's water picker filters on the volume class.
        "unit_class": VolumeConverter.UNIT_CLASS,
        "unit_of_measurement": UnitOfVolume.LITERS,
    }


def _cost_metadata(statistic_id: str, name: str) -> StatisticMetaData:
    return {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_class": None,
        "unit_of_measurement": "NZD",
    }


def _last_stored_rows(
    hass: HomeAssistant, statistic_ids: Sequence[str]
) -> dict[str, _Stored | None]:
    """Return the newest stored row of each statistic (recorder executor)."""
    stored: dict[str, _Stored | None] = {}
    for statistic_id in statistic_ids:
        rows = get_last_statistics(hass, 1, statistic_id, False, {"sum"}).get(
            statistic_id
        )
        if not rows or rows[0].get("sum") is None:
            stored[statistic_id] = None
            continue
        start = rows[0]["start"]
        start_dt = (
            start
            if isinstance(start, datetime)
            else datetime.fromtimestamp(float(start), tz=UTC)
        )
        stored[statistic_id] = _Stored(start_dt, Decimal(str(rows[0]["sum"])))
    return stored


async def _async_last_stored(hass: HomeAssistant) -> dict[str, _Stored | None]:
    return await get_instance(hass).async_add_executor_job(
        _last_stored_rows, hass, ALL_STATISTIC_IDS
    )


async def async_import(
    hass: HomeAssistant,
    periods: Sequence[BillingPeriod],
    schedule: TariffSchedule,
    wastewater_ratio: Decimal,
) -> ImportResult:
    """Add rows for days after the stored history (normal poll)."""
    stored = await _async_last_stored(hass)
    consumption = stored[STAT_CONSUMPTION]
    after = (
        consumption.start.astimezone(NZ_TIMEZONE).date()
        if consumption is not None
        else None
    )
    return _write(
        hass,
        periods=periods,
        schedule=schedule,
        wastewater_ratio=wastewater_ratio,
        stored=stored,
        after=after,
    )


def _write(
    hass: HomeAssistant,
    *,
    periods: Sequence[BillingPeriod],
    schedule: TariffSchedule,
    wastewater_ratio: Decimal,
    stored: dict[str, _Stored | None],
    after: date | None,
) -> ImportResult:
    days = daily_usage(periods, after=after)

    consumption_rows = _rows(
        days,
        stored[STAT_CONSUMPTION],
        lambda usage: usage.litres,
    )
    if consumption_rows:
        async_add_external_statistics(hass, _consumption_metadata(), consumption_rows)

    cost_rows = 0
    first_missing: date | None = None
    for statistic_id, (name, kind) in _COST_STATISTICS.items():
        rows, missing = _cost_rows(
            days, stored[statistic_id], schedule, wastewater_ratio, kind
        )
        if missing is not None:
            first_missing = (
                missing if first_missing is None else min(first_missing, missing)
            )
        if rows:
            async_add_external_statistics(
                hass, _cost_metadata(statistic_id, name), rows
            )
            cost_rows += len(rows)

    if first_missing is not None:
        _LOGGER.info(
            "Watercare cost statistics paused at %s: no tariff is known for "
            "that financial year yet",
            first_missing.isoformat(),
        )
    return ImportResult(
        consumption_rows=len(consumption_rows),
        cost_rows=cost_rows,
        first_day_without_tariff=first_missing,
    )


def _rows(
    days: Sequence[DailyUsage],
    stored: _Stored | None,
    value: Callable[[DailyUsage], Decimal],
) -> list[StatisticData]:
    running = stored.total if stored is not None else Decimal(0)
    rows: list[StatisticData] = []
    for usage in days:
        start = day_start(usage.day)
        if stored is not None and start <= stored.start:
            continue
        amount = value(usage)
        running += amount
        rows.append({"start": start, "state": float(amount), "sum": float(running)})
    return rows


def _cost_rows(
    days: Sequence[DailyUsage],
    stored: _Stored | None,
    schedule: TariffSchedule,
    wastewater_ratio: Decimal,
    kind: CostKind,
) -> tuple[list[StatisticData], date | None]:
    running = stored.total if stored is not None else Decimal(0)
    rows: list[StatisticData] = []
    for usage in days:
        start = day_start(usage.day)
        if stored is not None and start <= stored.start:
            continue
        tariff = schedule.for_day(usage.day)
        if tariff is None:
            # Keep the series contiguous: stop here and resume from this day
            # once a tariff exists.
            return rows, usage.day
        amount = daily_cost(usage, tariff, wastewater_ratio).part(kind)
        running += amount
        rows.append({"start": start, "state": float(amount), "sum": float(running)})
    return rows, None


async def async_rebuild_would_lose_history(
    hass: HomeAssistant, periods: Sequence[BillingPeriod]
) -> bool:
    """Return whether replacing the stored history would lose any of it.

    True when stored consumption starts before the oldest bill Watercare now
    returns, or when the stored running total is larger than a rebuild would
    reach by the same day (a bill Watercare no longer returns). Both checks
    hold for the 1.4.x layout (one row per bill end) and for daily rows.
    """
    first_day = min(period.start for period in periods)
    earlier = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(1970, 1, 1, tzinfo=UTC),
        day_start(first_day),
        {STAT_CONSUMPTION},
        "hour",
        None,
        {"sum"},
    )
    if earlier.get(STAT_CONSUMPTION):
        return True
    stored = (await _async_last_stored(hass))[STAT_CONSUMPTION]
    if stored is None:
        return False
    rebuilt = sum(
        (
            usage.litres
            for usage in daily_usage(periods)
            if day_start(usage.day) <= stored.start
        ),
        Decimal(0),
    )
    return stored.total > rebuilt + _VOLUME_TOLERANCE


async def async_clear(hass: HomeAssistant) -> None:
    """Clear the integration's four statistics and wait until it is done.

    Runs through the recorder's own queue (``Recorder.async_clear_statistics``),
    never from an executor thread, and waits for the recorder to confirm.
    """
    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def _on_done() -> None:
        # Called from the recorder thread.
        loop.call_soon_threadsafe(done.set)

    get_instance(hass).async_clear_statistics(list(ALL_STATISTIC_IDS), on_done=_on_done)
    async with asyncio.timeout(_CLEAR_TIMEOUT):
        await done.wait()


async def async_rebuild(
    hass: HomeAssistant,
    periods: Sequence[BillingPeriod],
    schedule: TariffSchedule,
    wastewater_ratio: Decimal,
) -> ImportResult:
    """Replace the four statistics with the full history in the new format.

    The caller has checked that the API's history reaches back at least as
    far as the stored history, so nothing is lost.
    """
    _LOGGER.warning(
        "Rebuilding the Watercare statistics in the 1.5.0 format from %d "
        "billing periods: clearing %s, then importing daily rows",
        len(periods),
        ", ".join(ALL_STATISTIC_IDS),
    )
    await async_clear(hass)
    stored: dict[str, _Stored | None] = dict.fromkeys(ALL_STATISTIC_IDS)
    result = _write(
        hass,
        periods=periods,
        schedule=schedule,
        wastewater_ratio=wastewater_ratio,
        stored=stored,
        after=None,
    )
    _LOGGER.warning(
        "Watercare statistics rebuilt: %d consumption rows and %d cost rows queued",
        result.consumption_rows,
        result.cost_rows,
    )
    return replace(result, rebuilt=True)
