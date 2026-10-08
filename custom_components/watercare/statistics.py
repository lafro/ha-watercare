"""Long-term statistics for the Energy dashboard.

The 1.5.0 model (docs/statistics.md has the reasoning):

* **Daily rows.** A bill covers about a month and HA statistics are hourly
  buckets. Each bill's volume is spread evenly over the days it covers, one
  row per Auckland day, instead of one spike after the period ends.
* **Anchored sums.** A poll only adds rows for days after the last stored
  row, continuing that row's running sum. Stored history is never rewritten
  by a normal poll, and a shorter API response cannot step the sum down.
* **Costs at the tariff in force.** Watercare prices a whole bill at the
  prices in force when its billing period starts, so every day of a bill is
  priced with the financial-year tariff (tariffs.py) of the bill's start
  date, and a bill that spans 1 July keeps the earlier year's prices. Cost
  rows stop at the first day whose bill has no known tariff and resume once
  one is available, so a missing price never becomes a wrong one.
* **One-off rebuild.** Entries upgraded from 1.4.x hold one row per bill,
  priced at a single flat tariff. ``async_rebuild`` clears the four
  statistics and imports the full history in the new format, once.
* **Never wait for the recorder in setup.** Writes are queued on the
  recorder. Reads first wait for the queue (``async_wait_for_queue``), which
  covers every write still waiting in it but not one the recorder is already
  running; ``_async_read`` explains why that gap is harmless. The recorder
  only works through its queue once Home Assistant has started, so the
  coordinator runs all of this in the background after start-up, never in
  setup (docs/statistics.md has the 1.5.0 defect this avoids).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Final, Literal, Protocol

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
from homeassistant.core import HomeAssistant, callback
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
from .tariffs import (
    DAYS_PER_YEAR,
    Tariff,
    TariffSchedule,
    financial_year,
    financial_year_label,
)

_LOGGER = logging.getLogger(__name__)

_LITRES_PER_KILOLITRE: Final = Decimal(1000)
_ONE_DAY: Final = timedelta(days=1)
_PRECISION: Final = Decimal("0.000000001")
# Litres. Stored sums are floats; anything above this is a real difference.
_VOLUME_TOLERANCE: Final = Decimal("0.5")

CostKind = Literal["total", "water", "wastewater"]

_COST_STATISTICS: Final[dict[str, tuple[str, CostKind]]] = {
    STAT_TOTAL_COST: ("Watercare Total Cost", "total"),
    STAT_CONSUMPTION_COST: ("Watercare Consumption Cost", "water"),
    STAT_WASTEWATER_COST: ("Watercare Wastewater Cost", "wastewater"),
}


# --- Pure calculations -------------------------------------------------------


type BillKey = tuple[date, date]
"""A bill's start and end date, which identify it (parse_billing_periods
drops duplicates)."""


def bill_key(period: BillingPeriod) -> BillKey:
    """Return the key of a bill."""
    return (period.start, period.end)


def pricing_date(period: BillingPeriod) -> date:
    """Return the date whose tariff prices the whole bill.

    Watercare prices a bill at the prices in force when its billing period
    starts: a bill that runs from June into July is charged entirely at the
    earlier financial year's prices. A bill spanning 1 July 2026 matched this
    to the cent, where a split by day was several dollars out; docs/tariffs.md
    has the details. Change the rule here, and only here, if more bills show
    otherwise.
    """
    return period.start


@dataclass(frozen=True, slots=True)
class BillShare:
    """One bill's share of one Auckland day."""

    day: date
    litres: Decimal
    fixed_charge_days: Decimal
    """Billing days this share stands for (1 unless Watercare's day count
    and the spread length differ)."""
    bill: BillKey
    priced_on: date
    """The date whose tariff prices this share (see ``pricing_date``)."""


@dataclass(frozen=True, slots=True)
class DailyUsage:
    """One Auckland day: the shares of every bill spread over it."""

    day: date
    shares: tuple[BillShare, ...]

    @property
    def litres(self) -> Decimal:
        """Return the litres recorded for the day."""
        return sum((share.litres for share in self.shares), Decimal(0))


@dataclass(frozen=True, slots=True)
class DailyCost:
    """Cost of one day or one bill, GST-inclusive (NZD)."""

    water: Decimal
    wastewater: Decimal
    fixed: Decimal

    def __add__(self, other: DailyCost) -> DailyCost:
        """Return the sum of two costs."""
        return DailyCost(
            self.water + other.water,
            self.wastewater + other.wastewater,
            self.fixed + other.fixed,
        )

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


_NO_COST: Final = DailyCost(Decimal(0), Decimal(0), Decimal(0))


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


def bill_shares(
    periods: Iterable[BillingPeriod], *, after: date | None = None
) -> list[BillShare]:
    """Spread every bill evenly over its days, oldest bill first.

    The litres of a bill's shares add up to the bill, and its fixed-charge
    days to Watercare's day count for it.
    """
    shares: list[BillShare] = []
    for period, days in spread_period_days(periods, after=after):
        usage_shares = split_evenly(Decimal(period.usage_litres), len(days))
        fixed_shares = split_evenly(Decimal(period.number_of_days), len(days))
        key = bill_key(period)
        priced_on = pricing_date(period)
        shares.extend(
            BillShare(day, usage_share, fixed_share, key, priced_on)
            for day, usage_share, fixed_share in zip(
                days, usage_shares, fixed_shares, strict=True
            )
        )
    return shares


def daily_usage(
    periods: Iterable[BillingPeriod], *, after: date | None = None
) -> list[DailyUsage]:
    """Return one entry per day, oldest first, with each bill's share of it.

    The litres of all entries add up to the sum of all bills.
    """
    by_day: dict[date, list[BillShare]] = {}
    for share in bill_shares(periods, after=after):
        by_day.setdefault(share.day, []).append(share)
    return [DailyUsage(day, tuple(by_day[day])) for day in sorted(by_day)]


class _Priceable(Protocol):
    @property
    def litres(self) -> Decimal: ...

    @property
    def fixed_charge_days(self) -> Decimal: ...


def daily_cost(
    usage: _Priceable, tariff: Tariff, wastewater_ratio: Decimal
) -> DailyCost:
    """Price litres and fixed-charge days with one tariff."""
    kilolitres = usage.litres / _LITRES_PER_KILOLITRE
    return DailyCost(
        water=kilolitres * tariff.water_rate,
        wastewater=kilolitres * wastewater_ratio * tariff.wastewater_rate,
        fixed=tariff.fixed_charge / DAYS_PER_YEAR * usage.fixed_charge_days,
    )


class MissingTariffError(LookupError):
    """A bill needs the prices of a financial year the schedule lacks."""

    def __init__(self, year: int) -> None:
        """Initialise the error with the financial year that is missing."""
        super().__init__(year)
        self.year = year


def day_cost(
    usage: DailyUsage, schedule: TariffSchedule, wastewater_ratio: Decimal
) -> DailyCost:
    """Price one day, each bill's share at that bill's tariff.

    Raises MissingTariffError, naming the earliest such year, when a bill on
    that day has no known tariff.
    """
    cost = _NO_COST
    for share in sorted(usage.shares, key=lambda share: share.priced_on):
        tariff = schedule.for_day(share.priced_on)
        if tariff is None:
            raise MissingTariffError(financial_year(share.priced_on))
        cost += daily_cost(share, tariff, wastewater_ratio)
    return cost


BillCost = DailyCost
"""Cost of one bill: the sum of its days."""


def bill_cost(
    period: BillingPeriod,
    periods: Sequence[BillingPeriod],
    schedule: TariffSchedule,
    wastewater_ratio: Decimal,
) -> BillCost | None:
    """Return the cost of one bill, or None if its tariff is unknown.

    Uses the same shares as the statistics, so the sensor and the Energy
    dashboard always agree.
    """
    key = bill_key(period)
    tariff = schedule.for_day(pricing_date(period))
    shares = [share for share in bill_shares(periods) if share.bill == key]
    if tariff is None or not shares:
        return None
    return sum(
        (daily_cost(share, tariff, wastewater_ratio) for share in shares), _NO_COST
    )


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
    """The day the cost statistics stop at, when a tariff is missing."""
    missing_tariff_year: int | None = None
    """The financial year whose prices that day's bill needs."""
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


async def async_wait_for_queue(hass: HomeAssistant) -> None:
    """Wait until the recorder has taken every queued task off its queue.

    This is ``Recorder.async_block_till_done``, which Home Assistant's history
    and logbook await before they read. If anything is queued, it queues a
    task of its own behind it and returns when the recorder thread reaches
    that task: every task that was waiting has then run. If the queue is
    empty, it returns at once, even while the recorder thread is still
    running the last task it took off the queue, so that task may not be
    committed yet.
    """
    await get_instance(hass).async_block_till_done()


async def _async_read[T](
    hass: HomeAssistant, target: Callable[..., T], *args: object
) -> T:
    """Read from the database after waiting for the recorder's queue.

    Reads go straight to the database, past the recorder's queue, so without
    the wait a read could miss rows, or a clear, queued by an earlier update
    and anchor new rows to stale ones (the 1.4.x rows, right after a rebuild).

    The read can still miss one write despite the wait
    (``async_wait_for_queue``): the last one queued, while the recorder is
    committing it. After a rebuild, that is the import of a statistic the
    rebuild has just cleared, so the read finds the statistic empty. That is
    harmless because a statistic with no stored rows is always imported in
    full, from zero (``_plan``): from the same bills, the import queues the
    same rows again, and the recorder keeps one row per statistic and hour,
    so they replace the identical rows the missed write stored. After a
    normal import, the missed write continues its statistic from that
    statistic's newest stored row, which the read still sees, so the next
    import plans the same days again from the same row and its rows replace
    the missed ones. ``tests/test_statistics_recorder.py`` pins the rule.
    """
    await async_wait_for_queue(hass)
    return await get_instance(hass).async_add_executor_job(target, *args)


async def _async_last_stored(hass: HomeAssistant) -> dict[str, _Stored | None]:
    return await _async_read(hass, _last_stored_rows, hass, ALL_STATISTIC_IDS)


async def async_import(
    hass: HomeAssistant,
    periods: Sequence[BillingPeriod],
    schedule: TariffSchedule,
    wastewater_ratio: Decimal,
) -> ImportResult:
    """Add rows for days after the stored history (normal poll).

    Nothing is awaited between reading the stored history and queuing the new
    rows, and nothing waits for the recorder to write them.
    """
    stored = await _async_last_stored(hass)
    consumption = stored[STAT_CONSUMPTION]
    after = (
        consumption.start.astimezone(NZ_TIMEZONE).date()
        if consumption is not None
        else None
    )
    return _async_queue(
        hass,
        _plan(
            periods=periods,
            schedule=schedule,
            wastewater_ratio=wastewater_ratio,
            stored=stored,
            after=after,
        ),
    )


@dataclass(frozen=True, slots=True)
class _Plan:
    """The rows to add to each statistic, and what they amount to."""

    writes: tuple[tuple[StatisticMetaData, list[StatisticData]], ...]
    result: ImportResult


def _plan(
    *,
    periods: Sequence[BillingPeriod],
    schedule: TariffSchedule,
    wastewater_ratio: Decimal,
    stored: dict[str, _Stored | None],
    after: date | None,
) -> _Plan:
    """Work out the rows to add after the stored ones (no I/O)."""
    days = daily_usage(periods, after=after)
    writes: list[tuple[StatisticMetaData, list[StatisticData]]] = []

    consumption_rows = _rows(
        days,
        stored[STAT_CONSUMPTION],
        lambda usage: usage.litres,
    )
    if consumption_rows:
        writes.append((_consumption_metadata(), consumption_rows))

    cost_rows = 0
    first_missing: tuple[date, int] | None = None
    for statistic_id, (name, kind) in _COST_STATISTICS.items():
        rows, missing = _cost_rows(
            days, stored[statistic_id], schedule, wastewater_ratio, kind
        )
        if missing is not None and (first_missing is None or missing < first_missing):
            first_missing = missing
        if rows:
            writes.append((_cost_metadata(statistic_id, name), rows))
            cost_rows += len(rows)

    if first_missing is not None:
        _LOGGER.info(
            "Watercare cost statistics paused at %s: no tariff is known for the "
            "%s prices that bill needs",
            first_missing[0].isoformat(),
            financial_year_label(first_missing[1]),
        )
    return _Plan(
        writes=tuple(writes),
        result=ImportResult(
            consumption_rows=len(consumption_rows),
            cost_rows=cost_rows,
            first_day_without_tariff=first_missing[0] if first_missing else None,
            missing_tariff_year=first_missing[1] if first_missing else None,
        ),
    )


@callback
def _async_queue(hass: HomeAssistant, plan: _Plan) -> ImportResult:
    """Queue the planned rows on the recorder, in order, without waiting."""
    for metadata, rows in plan.writes:
        async_add_external_statistics(hass, metadata, rows)
    return plan.result


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
) -> tuple[list[StatisticData], tuple[date, int] | None]:
    """Return cost rows after the stored ones, and where they stop, if they do.

    The stop is the first day a bill on it has no tariff, with the financial
    year that bill needs.
    """
    running = stored.total if stored is not None else Decimal(0)
    rows: list[StatisticData] = []
    for usage in days:
        start = day_start(usage.day)
        if stored is not None and start <= stored.start:
            continue
        try:
            amount = day_cost(usage, schedule, wastewater_ratio).part(kind)
        except MissingTariffError as err:
            # Keep the series contiguous: stop here and resume from this day
            # once a tariff exists.
            return rows, (usage.day, err.year)
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
    earlier = await _async_read(
        hass,
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


@callback
def async_rebuild(
    hass: HomeAssistant,
    periods: Sequence[BillingPeriod],
    schedule: TariffSchedule,
    wastewater_ratio: Decimal,
) -> ImportResult:
    """Replace the four statistics with the full history in the new format.

    The caller has checked that the API's history reaches back at least as
    far as the stored history, so nothing is lost.

    The rows are worked out first. Then the clear
    (``Recorder.async_clear_statistics``) and the imports are queued on the
    recorder back to back, in this one call, which never yields to the event
    loop: the imports are queued straight behind the clear, and no
    cancellation can fall between them. A shutdown can: if its final-write
    stage times out while the recorder runs the clear,
    ``Recorder._async_close`` drops the imports still queued. The marker is
    unset then, so the next start rebuilds.

    Nothing here waits for the recorder; 1.5.0 waited for the clear inside
    setup, and the recorder does not work through its queue until Home
    Assistant has started. The coordinator waits for the queue afterwards, in
    the background, before it records the rebuild as done.
    """
    plan = _plan(
        periods=periods,
        schedule=schedule,
        wastewater_ratio=wastewater_ratio,
        stored=dict.fromkeys(ALL_STATISTIC_IDS),
        after=None,
    )
    _LOGGER.warning(
        "Rebuilding the Watercare statistics in the 1.5.0 format from %d "
        "billing periods: clearing %s, then importing daily rows",
        len(periods),
        ", ".join(ALL_STATISTIC_IDS),
    )
    get_instance(hass).async_clear_statistics(list(ALL_STATISTIC_IDS))
    result = _async_queue(hass, plan)
    _LOGGER.warning(
        "Watercare statistics rebuilt: %d consumption rows and %d cost rows queued",
        result.consumption_rows,
        result.cost_rows,
    )
    return replace(result, rebuilt=True)
