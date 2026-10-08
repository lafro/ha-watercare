"""Synthetic test data.

Nothing here is a real account, meter, person, bill, balance or usage figure.
The bills fall on the 3rd and 4th of the month and their volumes are made up;
keep new fixtures equally obviously synthetic.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.watercare.const import (
    ALL_STATISTIC_IDS,
    CONF_WASTEWATER_RATIO,
    DATA_STATISTICS_VERSION,
    DOMAIN,
    NZ_TIMEZONE,
    STAT_CONSUMPTION,
    STAT_CONSUMPTION_COST,
    STAT_TOTAL_COST,
    STAT_WASTEWATER_COST,
    STATISTICS_VERSION,
)
from custom_components.watercare.statistics import day_start
from custom_components.watercare.tariffs import PUBLISHED_TARIFFS

ACCOUNT_NUMBER = "1000001-01"
METER_NUMBER = "TEST-METER-0001"
EMAIL = "someone@example.com"
PASSWORD = "synthetic-password"
REFRESH_TOKEN = "synthetic-refresh-token"
ENTRY_ID = "watercare_test_entry"

ACCOUNT_PAYLOAD: list[dict[str, Any]] = [
    {
        "userAccountId": 1,
        "accountNumber": ACCOUNT_NUMBER,
        "accountName": "Test Account",
        "amountDue": 123.45,
        "accountBalance": 123.45,
        "overdueAmount": 0,
        "dueDate": "2026-09-25T23:59:59Z",
        "hasDueDate": True,
        "meterType": "mechanical",
        "meters": [{"id": METER_NUMBER}],
    }
]


def api_date(day: date) -> str:
    """Return a date the way Watercare sends it: Auckland midnight in UTC."""
    instant = datetime.combine(day, time.min, tzinfo=NZ_TIMEZONE).astimezone(UTC)
    return instant.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def api_period(
    start: date,
    end: date,
    usage: int,
    *,
    days: int | None = None,
    reading: str = "A",
) -> dict[str, Any]:
    """Return one mechanicalmonthly item."""
    number_of_days = days if days is not None else (end - start).days + 1
    return {
        "billingPeriodFromDate": api_date(start),
        "billingPeriodToDate": api_date(end),
        "waterUsage": usage,
        "readingType": reading,
        "statistics": {
            "dailyAverage": round(usage / number_of_days),
            "numberOfDays": number_of_days,
            "efficiency": {"currentHouseholdBand": 2, "usageToLowerBand": 25},
        },
    }


def default_periods() -> list[dict[str, Any]]:
    """Three consecutive bills, newest first as the API sends them.

    The oldest spans 1 July 2026, so it is priced at 2025/26 prices.
    """
    return [
        api_period(date(2026, 8, 4), date(2026, 9, 2), 13000, reading="E"),
        api_period(date(2026, 7, 4), date(2026, 8, 3), 8000),
        api_period(date(2026, 6, 3), date(2026, 7, 3), 12000),
    ]


def make_entry(
    *,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    version: int = 1,
    minor_version: int = 2,
    unique_id: str | None = ACCOUNT_NUMBER,
    statistics_version: int | None = STATISTICS_VERSION,
) -> MockConfigEntry:
    """Return a config entry for the synthetic account."""
    entry_data: dict[str, Any] = {CONF_USERNAME: EMAIL, CONF_PASSWORD: PASSWORD}
    if statistics_version is not None:
        entry_data[DATA_STATISTICS_VERSION] = statistics_version
    if data is not None:
        entry_data = data
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        title="Watercare",
        version=version,
        minor_version=minor_version,
        unique_id=unique_id,
        data=entry_data,
        options=options if options is not None else {CONF_WASTEWATER_RATIO: 0.785},
    )


def _legacy_metadata(statistic_id: str, name: str, unit: str) -> Any:
    return {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_class": "volume" if unit == "L" else None,
        "unit_of_measurement": unit,
    }


async def add_legacy_statistics(hass: HomeAssistant) -> None:
    """Store all four statistics the way 1.4.x did for the default bills.

    One row per bill, at the Auckland midnight that starts its end date, every
    bill priced at the one flat tariff from the 1.4.x options (2025/26).
    """
    flat = PUBLISHED_TARIFFS[2025]
    ratio = Decimal("0.785")
    bills = [(date(2026, 7, 3), 12, 31), (date(2026, 8, 3), 8, 31)]
    bills.append((date(2026, 9, 2), 13, 30))
    sums: dict[str, Decimal] = dict.fromkeys(ALL_STATISTIC_IDS, Decimal(0))
    rows: dict[str, list[Any]] = {statistic_id: [] for statistic_id in sums}
    for end, kilolitres, days in bills:
        water = kilolitres * flat.water_rate
        wastewater = kilolitres * ratio * flat.wastewater_rate
        fixed = flat.fixed_charge / 365 * days
        for statistic_id, amount in (
            (STAT_CONSUMPTION, Decimal(kilolitres * 1000)),
            (STAT_TOTAL_COST, water + wastewater + fixed),
            (STAT_CONSUMPTION_COST, water),
            (STAT_WASTEWATER_COST, wastewater),
        ):
            sums[statistic_id] += amount
            rows[statistic_id].append(
                {"start": day_start(end), "sum": float(sums[statistic_id])}
            )
    names = {
        STAT_CONSUMPTION: ("Watercare Water Consumption", "L"),
        STAT_TOTAL_COST: ("Watercare Total Cost", "NZD"),
        STAT_CONSUMPTION_COST: ("Watercare Consumption Cost", "NZD"),
        STAT_WASTEWATER_COST: ("Watercare Wastewater Cost", "NZD"),
    }
    for statistic_id, (name, unit) in names.items():
        async_add_external_statistics(
            hass, _legacy_metadata(statistic_id, name, unit), rows[statistic_id]
        )
    await async_wait_recording_done(hass)


async def stored_rows(hass: HomeAssistant, statistic_id: str) -> list[dict[str, Any]]:
    """Return the stored rows of one statistic, once the recorder has caught up."""
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


# A safety net so a failing test cannot leave the recorder thread blocked.
HOLD_LIMIT = 60.0


@dataclass(slots=True)
class _HoldRecorder(RecorderTask):
    """Keep the recorder thread busy until released."""

    held: asyncio.Event
    release: threading.Event

    def run(self, instance: Recorder) -> None:
        instance.hass.loop.call_soon_threadsafe(self.held.set)
        self.release.wait(HOLD_LIMIT)


def hold_recorder(hass: HomeAssistant) -> tuple[asyncio.Event, threading.Event]:
    """Queue a task that keeps the recorder busy until released.

    Everything queued after it waits. Returns the event set once the recorder
    thread is held, and the one that releases it; always set the second.
    """
    held = asyncio.Event()
    release = threading.Event()
    get_instance(hass).queue_task(_HoldRecorder(held, release))
    return held, release


@contextlib.asynccontextmanager
async def recorder_held(hass: HomeAssistant) -> AsyncIterator[threading.Event]:
    """Stop the recorder working through its queue until released.

    This is how the recorder looks to an integration until Home Assistant has
    started, or while it is busy. Reads through its database executor still
    work, as they do during start-up.
    """
    held, release = hold_recorder(hass)
    await held.wait()
    try:
        yield release
    finally:
        release.set()
