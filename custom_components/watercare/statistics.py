"""Long-term statistics for the Energy dashboard.

The model (docs/statistics.md has the reasoning):

* **Daily rows.** A bill covers about a month and HA statistics are hourly
  buckets. Each bill's volume is spread evenly over the days it covers, one
  row per Auckland day, instead of one spike after the period ends.
* **Anchored sums.** A poll only adds rows for days after the last stored
  row, continuing that row's running sum. Stored history is never rewritten
  or deleted, and a shorter API response cannot step the sum down. A
  statistic with no stored rows is imported in full.
* **Costs at the tariff in force.** Watercare prices a whole bill at the
  prices in force when its billing period starts, so every day of a bill is
  priced with the financial-year tariff (tariffs.py) of the bill's start
  date, and a bill that spans 1 July keeps the earlier year's prices. Each
  bill is priced as a whole, with Watercare's rounding, and its three charge
  lines are then spread evenly over its days. Cost rows stop at the first
  day whose bill has no known tariff and resume once one is available, so a
  missing price never becomes a wrong one.
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
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
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
from .models import BillingPeriod, RefusedPeriod
from .tariffs import (
    Tariff,
    TariffSchedule,
    days_in_financial_year,
    financial_year,
    financial_year_label,
)

_LOGGER = logging.getLogger(__name__)

_LITRES_PER_KILOLITRE: Final = Decimal(1000)
_ONE_DAY: Final = timedelta(days=1)
_PRECISION: Final = Decimal("0.000000001")
_CENT: Final = Decimal("0.01")
# Watercare charges wastewater on a volume rounded to this, in kilolitres.
_WASTEWATER_VOLUME_STEP: Final = Decimal("0.01")

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
    earlier financial year's prices (docs/tariffs.md). Change the rule here,
    and only here, if a bill shows otherwise.
    """
    return period.start


@dataclass(frozen=True, slots=True)
class BillShare:
    """One bill's share of one Auckland day."""

    day: date
    litres: Decimal
    period: BillingPeriod
    part: int
    """This share's place among the days the bill is spread over, from 0."""
    parts: int
    """How many days the bill is spread over."""

    @property
    def bill(self) -> BillKey:
        """Return the key of the bill this share belongs to."""
        return bill_key(self.period)

    @property
    def priced_on(self) -> date:
        """Return the date whose tariff prices this share (``pricing_date``)."""
        return pricing_date(self.period)


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
    return [_even_part(total, part, parts) for part in range(parts)]


def _even_part(total: Decimal, part: int, parts: int) -> Decimal:
    """Return one part of ``split_evenly(total, parts)``, counting from 0."""
    return (total * (part + 1) / parts).quantize(_PRECISION) - (
        total * part / parts
    ).quantize(_PRECISION)


def bill_shares(
    periods: Iterable[BillingPeriod], *, after: date | None = None
) -> list[BillShare]:
    """Spread every bill evenly over its days, oldest bill first.

    The litres of a bill's shares add up to the bill.
    """
    shares: list[BillShare] = []
    for period, days in spread_period_days(periods, after=after):
        usage_shares = split_evenly(Decimal(period.usage_litres), len(days))
        shares.extend(
            BillShare(day, usage_share, period, part, len(days))
            for part, (day, usage_share) in enumerate(
                zip(days, usage_shares, strict=True)
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


BillCost = DailyCost
"""Cost of one bill: its three charge lines, each rounded to the cent."""


def _to_cent(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, ROUND_HALF_EVEN)


def price_bill(
    period: BillingPeriod, tariff: Tariff, wastewater_ratio: Decimal
) -> BillCost:
    """Price a whole bill as Watercare's invoice does (docs/tariffs.md).

    Each charge line is rounded to the cent, and the bill is their sum. The
    wastewater line charges the volume rounded to 0.01 kL, half to even.
    The fixed charge is the yearly charge over the days of the financial
    year whose prices apply (366 when it contains 29 February), times
    Watercare's day count for the bill, which includes both of its dates.
    """
    kilolitres = Decimal(period.usage_litres) / _LITRES_PER_KILOLITRE
    wastewater_kilolitres = (kilolitres * wastewater_ratio).quantize(
        _WASTEWATER_VOLUME_STEP, ROUND_HALF_EVEN
    )
    year_days = days_in_financial_year(financial_year(pricing_date(period)))
    return BillCost(
        water=_to_cent(kilolitres * tariff.water_rate),
        wastewater=_to_cent(wastewater_kilolitres * tariff.wastewater_rate),
        fixed=_to_cent(tariff.fixed_charge / year_days * period.number_of_days),
    )


def daily_cost(
    share: BillShare, tariff: Tariff, wastewater_ratio: Decimal
) -> DailyCost:
    """Return one share of its bill's cost, priced with one tariff.

    Each charge line of the bill is split evenly over the bill's days, so a
    bill's shares add up to the bill exactly.
    """
    bill = price_bill(share.period, tariff, wastewater_ratio)
    return DailyCost(
        water=_even_part(bill.water, share.part, share.parts),
        wastewater=_even_part(bill.wastewater, share.part, share.parts),
        fixed=_even_part(bill.fixed, share.part, share.parts),
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


def bill_cost(
    period: BillingPeriod, schedule: TariffSchedule, wastewater_ratio: Decimal
) -> BillCost | None:
    """Return the cost of one bill, or None if its tariff is unknown.

    The statistics spread this same cost over the bill's days when they record
    them, so the sensor and the Energy dashboard agree until the ratio or the
    prices change: the sensor then reprices the bill at once, while days
    already recorded keep the cost they were recorded with.
    """
    tariff = schedule.for_day(pricing_date(period))
    if tariff is None:
        return None
    return price_bill(period, tariff, wastewater_ratio)


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
    refused: tuple[RefusedPeriod, ...] = ()
    """Bills the smart-meter guard left out because they overlap stored days."""


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
    the wait a read could miss rows queued by an earlier update and plan the
    same days again from older sums.

    The read can still miss one write despite the wait
    (``async_wait_for_queue``): the last one queued, while the recorder is
    committing it. If that write was its statistic's first, the read finds
    the statistic empty. That is harmless because a statistic with no stored
    rows is always imported in full, from zero (``_plan``): from the same
    bills, the import queues the same rows again, and the recorder keeps one
    row per statistic and hour, so they replace the identical rows the missed
    write stored. Otherwise the missed write continues its statistic from
    that statistic's newest stored row, which the read still sees, so the
    next import plans the same days again from the same row and its rows
    replace the missed ones. ``tests/test_statistics_recorder.py`` pins the
    rule.
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
    *,
    smart_meter: bool = False,
) -> ImportResult:
    """Add rows for days after the stored history.

    With nothing stored, every bill is imported. Nothing is awaited between
    reading the stored history and queuing the new rows, and nothing waits
    for the recorder to write them.

    A bill that starts before the last stored day and ends after it is
    normally spread over the days after it, with its whole volume
    (``spread_period_days``). With ``smart_meter``, such a bill is refused
    instead: a smart meter's periods have never been seen, and one that grew
    or was revised would count the stored days' water twice.
    """
    stored = await _async_last_stored(hass)
    consumption = stored[STAT_CONSUMPTION]
    after = (
        consumption.start.astimezone(NZ_TIMEZONE).date()
        if consumption is not None
        else None
    )
    refused: tuple[RefusedPeriod, ...] = ()
    if smart_meter and after is not None:
        refused = tuple(
            RefusedPeriod(period, "overlaps_stored")
            for period in periods
            if period.start < after < period.end
        )
        periods = [
            period for period in periods if not period.start < after < period.end
        ]
    plan = _plan(
        periods=periods,
        schedule=schedule,
        wastewater_ratio=wastewater_ratio,
        stored=stored,
        after=after,
    )
    if consumption is None and plan.result.consumption_rows:
        _LOGGER.debug(
            "Importing the Watercare history: %d days from %d billing periods",
            plan.result.consumption_rows,
            len(periods),
        )
    result = _async_queue(hass, plan)
    return ImportResult(
        consumption_rows=result.consumption_rows,
        cost_rows=result.cost_rows,
        first_day_without_tariff=result.first_day_without_tariff,
        missing_tariff_year=result.missing_tariff_year,
        refused=refused,
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
        # The tariff_missing repair notice tells the user; this repeats on
        # every update while the prices are missing.
        _LOGGER.debug(
            "Cost statistics paused from %s: no prices for the %s financial year",
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
