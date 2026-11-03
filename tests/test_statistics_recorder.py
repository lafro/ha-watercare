"""Statistics import and anchoring, on a real recorder."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.watercare import statistics
from custom_components.watercare.const import (
    ALL_STATISTIC_IDS,
    STAT_CONSUMPTION,
    STAT_CONSUMPTION_COST,
    STAT_TOTAL_COST,
    STAT_WASTEWATER_COST,
)
from custom_components.watercare.models import BillingPeriod
from custom_components.watercare.statistics import (
    async_import,
    bill_cost,
    day_start,
)
from custom_components.watercare.tariffs import (
    LATEST_PUBLISHED_YEAR,
    Tariff,
    TariffSchedule,
)

from .common import api_period, recorder_held

RATIO = Decimal("0.785")
PUBLISHED = TariffSchedule({})


def _period(start: date, end: date, usage: int) -> BillingPeriod:
    period = BillingPeriod.from_json(api_period(start, end, usage))
    assert period is not None
    return period


# Synthetic bills; JUNE spans 1 July and is priced at 2025/26 prices.
JUNE = _period(date(2026, 6, 3), date(2026, 7, 3), 12000)
JULY = _period(date(2026, 7, 4), date(2026, 8, 3), 8000)
AUGUST = _period(date(2026, 8, 4), date(2026, 9, 2), 13000)


async def _rows(hass: HomeAssistant, statistic_id: str) -> list[dict[str, Any]]:
    await async_wait_recording_done(hass)
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2000, 1, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return list(result.get(statistic_id, []))


def _start(row: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(row["start"], tz=UTC)


async def test_first_import_writes_daily_rows(ha: HomeAssistant) -> None:
    result = await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)

    assert result.consumption_rows == 62
    assert result.cost_rows == 3 * 62
    assert result.first_day_without_tariff is None
    assert result.missing_tariff_year is None
    rows = await _rows(ha, STAT_CONSUMPTION)
    assert len(rows) == 62
    assert _start(rows[0]) == day_start(date(2026, 6, 3))
    assert _start(rows[-1]) == day_start(date(2026, 8, 3))
    assert rows[-1]["sum"] == pytest.approx(20000)
    assert rows[0]["state"] == pytest.approx(12000 / 31)
    sums = [row["sum"] for row in rows]
    assert sums == sorted(sums)

    metadata = await get_instance(ha).async_add_executor_job(
        partial(get_metadata, ha, statistic_ids={STAT_CONSUMPTION, STAT_TOTAL_COST})
    )
    consumption_meta = metadata[STAT_CONSUMPTION][1]
    assert consumption_meta["unit_class"] == "volume"
    assert consumption_meta["unit_of_measurement"] == "L"
    assert consumption_meta["mean_type"] is StatisticMeanType.NONE
    assert metadata[STAT_TOTAL_COST][1]["unit_of_measurement"] == "NZD"

    total = await _rows(ha, STAT_TOTAL_COST)
    june_cost = bill_cost(JUNE, PUBLISHED, RATIO)
    july_cost = bill_cost(JULY, PUBLISHED, RATIO)
    assert june_cost is not None
    assert july_cost is not None
    assert total[-1]["sum"] == pytest.approx(float(june_cost.total + july_cost.total))
    water = await _rows(ha, STAT_CONSUMPTION_COST)
    wastewater = await _rows(ha, STAT_WASTEWATER_COST)
    assert water[-1]["sum"] == pytest.approx(float(june_cost.water + july_cost.water))
    assert wastewater[-1]["sum"] == pytest.approx(
        float(june_cost.wastewater + july_cost.wastewater)
    )


def _watercare_log(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name.startswith("custom_components.watercare")
    ]


async def test_only_the_first_import_is_logged(
    ha: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.watercare")
    await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)
    await async_wait_recording_done(ha)

    assert _watercare_log(caplog) == [
        (
            logging.DEBUG,
            "Importing the Watercare history: 62 days from 2 billing periods",
        )
    ]
    caplog.clear()
    await async_import(ha, [JUNE, JULY, AUGUST], PUBLISHED, RATIO)
    assert _watercare_log(caplog) == []


async def test_repeat_poll_writes_nothing(ha: HomeAssistant) -> None:
    await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)
    await async_wait_recording_done(ha)

    result = await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)

    assert result.consumption_rows == 0
    assert result.cost_rows == 0
    rows = await _rows(ha, STAT_CONSUMPTION)
    assert rows[-1]["sum"] == pytest.approx(20000)


async def test_new_bill_continues_from_the_stored_sum(ha: HomeAssistant) -> None:
    await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)
    await async_wait_recording_done(ha)
    before = await _rows(ha, STAT_CONSUMPTION)

    result = await async_import(ha, [JUNE, JULY, AUGUST], PUBLISHED, RATIO)

    assert result.consumption_rows == 30
    after = await _rows(ha, STAT_CONSUMPTION)
    # Older rows are untouched; the new ones continue the sum.
    assert after[: len(before)] == before
    assert after[-1]["sum"] == pytest.approx(33000)
    assert _start(after[len(before)]) == day_start(date(2026, 8, 4))


async def test_shorter_api_history_never_steps_the_sum_down(ha: HomeAssistant) -> None:
    await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)
    await async_wait_recording_done(ha)

    # Watercare returns only the newest bill.
    await async_import(ha, [AUGUST], PUBLISHED, RATIO)

    rows = await _rows(ha, STAT_CONSUMPTION)
    sums = [row["sum"] for row in rows]
    assert sums == sorted(sums)
    assert sums[-1] == pytest.approx(33000)


async def test_stored_rows_are_never_repriced(ha: HomeAssistant) -> None:
    await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)
    await async_wait_recording_done(ha)
    before = await _rows(ha, STAT_TOTAL_COST)

    # A changed price for a year that is already stored has no effect.
    expensive = TariffSchedule({2026: Tariff.of("9", "9", "999")})
    await async_import(ha, [JUNE, JULY], expensive, RATIO)

    assert await _rows(ha, STAT_TOTAL_COST) == before


async def test_cost_pauses_without_a_tariff_and_resumes(ha: HomeAssistant) -> None:
    year = LATEST_PUBLISHED_YEAR + 1
    # Spans 1 July but starts in a published year, so it is fully priced.
    spanning = _period(date(year, 6, 3), date(year, 7, 3), 9300)
    unknown = _period(date(year, 7, 4), date(year, 8, 3), 7000)

    result = await async_import(ha, [spanning, unknown], PUBLISHED, RATIO)

    assert result.consumption_rows == 62
    assert result.first_day_without_tariff == date(year, 7, 4)
    assert result.missing_tariff_year == year
    consumption = await _rows(ha, STAT_CONSUMPTION)
    total = await _rows(ha, STAT_TOTAL_COST)
    assert len(consumption) == 62
    assert _start(total[-1]) == day_start(date(year, 7, 3))

    schedule = TariffSchedule({year: Tariff.of("3", "5", "400")})
    resumed = await async_import(ha, [spanning, unknown], schedule, RATIO)

    assert resumed.consumption_rows == 0
    assert resumed.cost_rows == 3 * 31
    assert resumed.first_day_without_tariff is None
    total = await _rows(ha, STAT_TOTAL_COST)
    assert len(total) == 62
    expected = bill_cost(spanning, schedule, RATIO)
    expected_unknown = bill_cost(unknown, schedule, RATIO)
    assert expected is not None
    assert expected_unknown is not None
    assert total[-1]["sum"] == pytest.approx(
        float(expected.total + expected_unknown.total)
    )


async def test_daily_rows_across_daylight_saving_changes(ha: HomeAssistant) -> None:
    # Daylight saving starts 27 Sep 2026 and ends 4 Apr 2027 in Auckland.
    spring = _period(date(2026, 9, 20), date(2026, 10, 4), 1500)
    autumn = _period(date(2027, 3, 28), date(2027, 4, 11), 1500)

    result = await async_import(ha, [spring, autumn], PUBLISHED, RATIO)

    assert result.consumption_rows == 30
    rows = await _rows(ha, STAT_CONSUMPTION)
    days = [
        *(date(2026, 9, 20) + timedelta(days=offset) for offset in range(15)),
        *(date(2027, 3, 28) + timedelta(days=offset) for offset in range(15)),
    ]
    assert [_start(row) for row in rows] == [day_start(day) for day in days]
    # One row per Auckland midnight: 12:00 UTC in standard time, 11:00 UTC in
    # daylight time, and never two rows for one day.
    assert {_start(row).hour for row in rows} == {11, 12}
    assert _start(rows[7]) == datetime(2026, 9, 26, 12, tzinfo=UTC)
    assert _start(rows[8]) == datetime(2026, 9, 27, 11, tzinfo=UTC)
    assert _start(rows[22]) == datetime(2027, 4, 3, 11, tzinfo=UTC)
    assert _start(rows[23]) == datetime(2027, 4, 4, 12, tzinfo=UTC)
    assert all(row["state"] == pytest.approx(100) for row in rows)
    assert rows[-1]["sum"] == pytest.approx(3000)
    total = await _rows(ha, STAT_TOTAL_COST)
    assert [_start(row) for row in total] == [_start(row) for row in rows]


async def test_an_import_reads_only_after_queued_writes(ha: HomeAssistant) -> None:
    """A poll right after an import continues the rows it queued.

    The recorder is busy, so the first import's rows are still queued when the
    next import starts. Reading the stored history then would find nothing
    and import every bill again from zero.
    """
    async with recorder_held(ha):
        first = statistics._plan(
            periods=[JUNE, JULY],
            schedule=PUBLISHED,
            wastewater_ratio=RATIO,
            stored=dict.fromkeys(ALL_STATISTIC_IDS),
            after=None,
        )
        statistics._async_queue(ha, first)
        poll = asyncio.ensure_future(
            async_import(ha, [JUNE, JULY, AUGUST], PUBLISHED, RATIO)
        )
        await asyncio.sleep(0.1)
        assert not poll.done()
    result = await poll

    assert result.consumption_rows == 30
    rows = await _rows(ha, STAT_CONSUMPTION)
    assert len(rows) == 92
    assert rows[-1]["sum"] == pytest.approx(33000)
    total = await _rows(ha, STAT_TOTAL_COST)
    assert len(total) == 92
    costs = [bill_cost(period, PUBLISHED, RATIO) for period in (JUNE, JULY, AUGUST)]
    assert total[-1]["sum"] == pytest.approx(
        float(sum(cost.total for cost in costs if cost is not None))
    )


@pytest.mark.parametrize("missed", ALL_STATISTIC_IDS)
async def test_a_statistic_with_no_stored_rows_is_reimported_with_the_same_rows(
    ha: HomeAssistant, missed: str
) -> None:
    """The rule that makes the gap in the read barrier harmless.

    ``_async_read`` can go ahead while the recorder is still committing the
    last write queued. Right after the first import, that write is the first
    of its statistic, so the next import reads it as empty (the parameter
    says which). A statistic with no stored rows is always imported in full,
    from zero, so the next import queues the same rows for it again, and
    they replace themselves.
    """
    await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)
    imported = {sid: await _rows(ha, sid) for sid in ALL_STATISTIC_IDS}
    read = statistics._last_stored_rows

    def _read_before_the_last_write(
        hass: HomeAssistant, statistic_ids: list[str]
    ) -> dict[str, Any]:
        stored = read(hass, statistic_ids)
        stored[missed] = None
        return stored

    with patch.object(
        statistics, "_last_stored_rows", side_effect=_read_before_the_last_write
    ):
        result = await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)

    # Every day again for the statistic read as empty, nothing for the others.
    if missed == STAT_CONSUMPTION:
        assert (result.consumption_rows, result.cost_rows) == (62, 0)
    else:
        assert (result.consumption_rows, result.cost_rows) == (0, 62)
    # The same rows: all four statistics are exactly as the import left them.
    assert {sid: await _rows(ha, sid) for sid in ALL_STATISTIC_IDS} == imported


# A smart meter's periods have never been seen. Whatever Watercare lists for
# one is read, but a period that would move after the stored days with its
# whole volume is refused instead: it may be a grown or revised period.
GROWN_JUNE = _period(date(2026, 6, 3), date(2026, 7, 20), 15000)
JULY_FROM_JUNE_END = _period(date(2026, 7, 3), date(2026, 8, 3), 8000)


@pytest.mark.parametrize("smart_meter", [False, True], ids=["manual", "smart"])
async def test_a_smart_meter_period_overlapping_stored_days_is_refused(
    ha: HomeAssistant, smart_meter: bool
) -> None:
    await async_import(ha, [JUNE], PUBLISHED, RATIO, smart_meter=smart_meter)
    await async_wait_recording_done(ha)

    # Watercare lists the June period again, with the same start and a later
    # end.
    result = await async_import(
        ha, [GROWN_JUNE], PUBLISHED, RATIO, smart_meter=smart_meter
    )

    rows = await _rows(ha, STAT_CONSUMPTION)
    if smart_meter:
        assert [(item.period, item.reason) for item in result.refused] == [
            (GROWN_JUNE, "overlaps_stored")
        ]
        assert result.consumption_rows == 0
        assert rows[-1]["sum"] == pytest.approx(12000)
    else:
        # A manually read meter keeps the rule from 1.5.0: the period moves
        # after the stored days, with its whole volume.
        assert result.refused == ()
        assert result.consumption_rows == 17
        assert rows[-1]["sum"] == pytest.approx(27000)


async def test_a_smart_meter_period_starting_on_the_last_stored_day_is_read(
    ha: HomeAssistant,
) -> None:
    await async_import(ha, [JUNE], PUBLISHED, RATIO, smart_meter=True)
    await async_wait_recording_done(ha)

    result = await async_import(
        ha, [JUNE, JULY_FROM_JUNE_END], PUBLISHED, RATIO, smart_meter=True
    )

    assert result.refused == ()
    assert result.consumption_rows == 31
    rows = await _rows(ha, STAT_CONSUMPTION)
    assert _start(rows[-1]) == day_start(date(2026, 8, 3))
    assert rows[-1]["sum"] == pytest.approx(20000)


async def test_a_smart_meter_first_import_takes_every_period(
    ha: HomeAssistant,
) -> None:
    result = await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO, smart_meter=True)

    assert result.refused == ()
    assert result.consumption_rows == 62
