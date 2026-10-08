"""Statistics import, anchoring and the one-off rebuild, on a real recorder."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

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
    async_rebuild,
    async_rebuild_would_lose_history,
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
    june_cost = bill_cost(JUNE, [JUNE, JULY], PUBLISHED, RATIO)
    july_cost = bill_cost(JULY, [JUNE, JULY], PUBLISHED, RATIO)
    assert june_cost is not None
    assert july_cost is not None
    assert total[-1]["sum"] == pytest.approx(float(june_cost.total + july_cost.total))
    water = await _rows(ha, STAT_CONSUMPTION_COST)
    wastewater = await _rows(ha, STAT_WASTEWATER_COST)
    assert water[-1]["sum"] == pytest.approx(float(june_cost.water + july_cost.water))
    assert wastewater[-1]["sum"] == pytest.approx(
        float(june_cost.wastewater + july_cost.wastewater)
    )


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
    expected = bill_cost(spanning, [spanning, unknown], schedule, RATIO)
    expected_unknown = bill_cost(unknown, [spanning, unknown], schedule, RATIO)
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


async def _add_legacy_rows(hass: HomeAssistant) -> None:
    """Store statistics the way 1.4.x did: one spike per bill end."""
    meta: dict[str, Any] = {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": "Watercare Water Consumption",
        "source": "watercare",
        "statistic_id": STAT_CONSUMPTION,
        "unit_class": "volume",
        "unit_of_measurement": "L",
    }
    async_add_external_statistics(
        hass,
        meta,
        [
            {"start": day_start(date(2026, 7, 3)), "sum": 12000.0},
            {"start": day_start(date(2026, 8, 3)), "sum": 20000.0},
        ],
    )
    cost_meta = {**meta, "statistic_id": STAT_TOTAL_COST, "unit_class": None}
    cost_meta["unit_of_measurement"] = "NZD"
    async_add_external_statistics(
        hass,
        cost_meta,  # type: ignore[arg-type]
        [
            {"start": day_start(date(2026, 7, 3)), "sum": 111.11},
            {"start": day_start(date(2026, 8, 3)), "sum": 222.22},
        ],
    )
    await async_wait_recording_done(hass)


async def test_rebuild_replaces_legacy_rows(ha: HomeAssistant) -> None:
    await _add_legacy_rows(ha)
    assert len(await _rows(ha, STAT_CONSUMPTION)) == 2

    result = async_rebuild(ha, [JUNE, JULY], PUBLISHED, RATIO)

    assert result.rebuilt
    assert result.consumption_rows == 62
    rows = await _rows(ha, STAT_CONSUMPTION)
    assert len(rows) == 62
    assert rows[-1]["sum"] == pytest.approx(20000)
    total = await _rows(ha, STAT_TOTAL_COST)
    assert len(total) == 62
    june = bill_cost(JUNE, [JUNE, JULY], PUBLISHED, RATIO)
    july = bill_cost(JULY, [JUNE, JULY], PUBLISHED, RATIO)
    assert june is not None
    assert july is not None
    assert total[-1]["sum"] == pytest.approx(float(june.total + july.total))


async def test_rebuild_clears_all_four_statistics(ha: HomeAssistant) -> None:
    await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)
    await async_wait_recording_done(ha)

    async_rebuild(ha, [JULY], PUBLISHED, RATIO)

    for statistic_id in ALL_STATISTIC_IDS:
        rows = await _rows(ha, statistic_id)
        assert len(rows) == 31, statistic_id
        assert _start(rows[0]) == day_start(date(2026, 7, 4)), statistic_id


async def test_rebuild_queues_the_clear_and_the_import_back_to_back(
    ha: HomeAssistant,
) -> None:
    """One call, no await: nothing can come between the clear and the import."""
    queued: list[Any] = []
    queue_task = Recorder.queue_task

    def _record(self: Recorder, task: Any) -> None:
        queued.append(task)
        queue_task(self, task)

    with patch.object(Recorder, "queue_task", _record):
        result = async_rebuild(ha, [JUNE, JULY], PUBLISHED, RATIO)

    assert result.rebuilt
    assert [type(task).__name__ for task in queued] == [
        "ClearStatisticsTask",
        *["ImportStatisticsTask"] * 4,
    ]
    clear = queued[0]
    assert sorted(clear.statistic_ids) == sorted(ALL_STATISTIC_IDS)
    # Nothing waits for the recorder to confirm the clear.
    assert clear.on_done is None
    assert [task.metadata["statistic_id"] for task in queued[1:]] == [
        STAT_CONSUMPTION,
        STAT_TOTAL_COST,
        STAT_CONSUMPTION_COST,
        STAT_WASTEWATER_COST,
    ]


async def test_an_import_reads_only_after_queued_writes(ha: HomeAssistant) -> None:
    """A poll right after a rebuild continues the rebuilt rows, not the old ones.

    The recorder is busy, so the rebuild is still queued when the next import
    starts. Reading the stored history then would find the 1.4.x rows and
    continue their sums.
    """
    await _add_legacy_rows(ha)

    async with recorder_held(ha):
        async_rebuild(ha, [JUNE, JULY], PUBLISHED, RATIO)
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
    costs = [
        bill_cost(period, [JUNE, JULY, AUGUST], PUBLISHED, RATIO)
        for period in (JUNE, JULY, AUGUST)
    ]
    assert total[-1]["sum"] == pytest.approx(
        float(sum(cost.total for cost in costs if cost is not None))
    )


async def test_rebuild_guard(ha: HomeAssistant) -> None:
    # Nothing stored: nothing to lose.
    assert not await async_rebuild_would_lose_history(ha, [JUNE, JULY])

    # 1.4.x rows at bill ends with matching totals: safe to rebuild.
    await _add_legacy_rows(ha)
    assert not await async_rebuild_would_lose_history(ha, [JUNE, JULY, AUGUST])

    # Watercare no longer returns the June bill: stored rows reach back
    # before the oldest bill.
    assert await async_rebuild_would_lose_history(ha, [JULY, AUGUST])


async def test_rebuild_guard_catches_a_dropped_bill_on_a_shared_boundary(
    ha: HomeAssistant,
) -> None:
    await _add_legacy_rows(ha)
    # Bills share boundary days and the API dropped the June bill: the next
    # bill starts on 3 July, the day of the stored June row, so only the
    # running total shows the loss.
    july = _period(date(2026, 7, 3), date(2026, 8, 3), 8000)

    assert await async_rebuild_would_lose_history(ha, [july, AUGUST])


async def test_daily_rows_from_an_earlier_install_are_safe_to_rebuild(
    ha: HomeAssistant,
) -> None:
    await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)
    await async_wait_recording_done(ha)

    assert not await async_rebuild_would_lose_history(ha, [JUNE, JULY, AUGUST])
