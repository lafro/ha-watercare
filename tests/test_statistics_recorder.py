"""Statistics import, anchoring and the one-off rebuild, on a real recorder."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from typing import Any

import pytest
from homeassistant.components.recorder import get_instance
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
    STAT_CONSUMPTION,
    STAT_CONSUMPTION_COST,
    STAT_TOTAL_COST,
    STAT_WASTEWATER_COST,
)
from custom_components.watercare.models import BillingPeriod
from custom_components.watercare.statistics import (
    async_clear,
    async_history_before,
    async_import,
    async_rebuild,
    bill_cost,
    day_start,
)
from custom_components.watercare.tariffs import (
    LATEST_PUBLISHED_YEAR,
    Tariff,
    TariffSchedule,
)

from .common import api_period

RATIO = Decimal("0.785")
PUBLISHED = TariffSchedule({})


def _period(start: date, end: date, usage: int) -> BillingPeriod:
    period = BillingPeriod.from_json(api_period(start, end, usage))
    assert period is not None
    return period


JUNE = _period(date(2026, 6, 16), date(2026, 7, 16), 10000)
JULY = _period(date(2026, 7, 17), date(2026, 8, 16), 9000)
AUGUST = _period(date(2026, 8, 17), date(2026, 9, 15), 11000)


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
    rows = await _rows(ha, STAT_CONSUMPTION)
    assert len(rows) == 62
    assert _start(rows[0]) == day_start(date(2026, 6, 16))
    assert _start(rows[-1]) == day_start(date(2026, 8, 16))
    assert rows[-1]["sum"] == pytest.approx(19000)
    assert rows[0]["state"] == pytest.approx(10000 / 31)
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
    assert rows[-1]["sum"] == pytest.approx(19000)


async def test_new_bill_continues_from_the_stored_sum(ha: HomeAssistant) -> None:
    await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)
    await async_wait_recording_done(ha)
    before = await _rows(ha, STAT_CONSUMPTION)

    result = await async_import(ha, [JUNE, JULY, AUGUST], PUBLISHED, RATIO)

    assert result.consumption_rows == 30
    after = await _rows(ha, STAT_CONSUMPTION)
    # Older rows are untouched; the new ones continue the sum.
    assert after[: len(before)] == before
    assert after[-1]["sum"] == pytest.approx(30000)
    assert _start(after[len(before)]) == day_start(date(2026, 8, 17))


async def test_shorter_api_history_never_steps_the_sum_down(ha: HomeAssistant) -> None:
    await async_import(ha, [JUNE, JULY], PUBLISHED, RATIO)
    await async_wait_recording_done(ha)

    # Watercare returns only the newest bill.
    await async_import(ha, [AUGUST], PUBLISHED, RATIO)

    rows = await _rows(ha, STAT_CONSUMPTION)
    sums = [row["sum"] for row in rows]
    assert sums == sorted(sums)
    assert sums[-1] == pytest.approx(30000)


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
    known = _period(date(year, 5, 17), date(year, 6, 16), 9300)
    unknown = _period(date(year, 6, 17), date(year, 7, 16), 9000)

    result = await async_import(ha, [known, unknown], PUBLISHED, RATIO)

    assert result.consumption_rows == 61
    assert result.first_day_without_tariff == date(year, 7, 1)
    consumption = await _rows(ha, STAT_CONSUMPTION)
    total = await _rows(ha, STAT_TOTAL_COST)
    assert len(consumption) == 61
    assert _start(total[-1]) == day_start(date(year, 6, 30))

    schedule = TariffSchedule({year: Tariff.of("3", "5", "400")})
    resumed = await async_import(ha, [known, unknown], schedule, RATIO)

    assert resumed.consumption_rows == 0
    assert resumed.cost_rows == 3 * 16
    assert resumed.first_day_without_tariff is None
    total = await _rows(ha, STAT_TOTAL_COST)
    assert len(total) == 61
    expected = bill_cost(known, [known, unknown], schedule, RATIO)
    expected_unknown = bill_cost(unknown, [known, unknown], schedule, RATIO)
    assert expected is not None
    assert expected_unknown is not None
    assert total[-1]["sum"] == pytest.approx(
        float(expected.total + expected_unknown.total)
    )


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
            {"start": day_start(date(2026, 7, 16)), "sum": 10000.0},
            {"start": day_start(date(2026, 8, 16)), "sum": 19000.0},
        ],
    )
    cost_meta = {**meta, "statistic_id": STAT_TOTAL_COST, "unit_class": None}
    cost_meta["unit_of_measurement"] = "NZD"
    async_add_external_statistics(
        hass,
        cost_meta,  # type: ignore[arg-type]
        [
            {"start": day_start(date(2026, 7, 16)), "sum": 82.51},
            {"start": day_start(date(2026, 8, 16)), "sum": 160.0},
        ],
    )
    await async_wait_recording_done(hass)


async def test_rebuild_replaces_legacy_rows(ha: HomeAssistant) -> None:
    await _add_legacy_rows(ha)
    assert len(await _rows(ha, STAT_CONSUMPTION)) == 2

    result = await async_rebuild(ha, [JUNE, JULY], PUBLISHED, RATIO)

    assert result.rebuilt
    assert result.consumption_rows == 62
    rows = await _rows(ha, STAT_CONSUMPTION)
    assert len(rows) == 62
    assert rows[-1]["sum"] == pytest.approx(19000)
    total = await _rows(ha, STAT_TOTAL_COST)
    assert len(total) == 62
    assert total[-1]["sum"] != pytest.approx(160.0)


async def test_clear_removes_all_four_statistics(ha: HomeAssistant) -> None:
    await async_import(ha, [JUNE], PUBLISHED, RATIO)
    await async_wait_recording_done(ha)

    await async_clear(ha)

    for statistic_id in (
        STAT_CONSUMPTION,
        STAT_TOTAL_COST,
        STAT_CONSUMPTION_COST,
        STAT_WASTEWATER_COST,
    ):
        assert await _rows(ha, statistic_id) == []


async def test_history_before(ha: HomeAssistant) -> None:
    assert not await async_history_before(ha, date(2026, 6, 16))
    await _add_legacy_rows(ha)

    assert not await async_history_before(ha, date(2026, 6, 16))
    assert await async_history_before(ha, date(2026, 8, 1))
