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
from datetime import UTC, date, datetime
from typing import Any

from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.watercare.const import (
    CONF_WASTEWATER_RATIO,
    DATA_STATISTICS_VERSION,
    DOMAIN,
    STATISTICS_VERSION,
)

# The synthetic account, login and payload builders live with the fake
# Watercare, which the journeys' stand-in also loads outside the harness.
from .watercare_http import (
    ACCOUNT_NUMBER,
    EMAIL,
    METER_NUMBER,
    PASSWORD,
    account_payload,
    api_period,
    api_transaction,
)

REFRESH_TOKEN = "synthetic-refresh-token"
ENTRY_ID = "watercare_test_entry"

ACCOUNT_PAYLOAD: list[dict[str, Any]] = account_payload(ACCOUNT_NUMBER, METER_NUMBER)


def default_periods() -> list[dict[str, Any]]:
    """Three consecutive bills, newest first as the API sends them.

    The oldest spans 1 July 2026, so it is priced at 2025/26 prices.
    """
    return [
        api_period(date(2026, 8, 4), date(2026, 9, 2), 13000, reading="E"),
        api_period(date(2026, 7, 4), date(2026, 8, 3), 8000),
        api_period(date(2026, 6, 3), date(2026, 7, 3), 12000),
    ]


def default_transactions() -> dict[str, Any]:
    """The default bills' amounts and payments, newest first.

    Each bill is issued two days after its period ends; the newest is not
    paid yet.
    """
    return {
        "items": [
            api_transaction(date(2026, 9, 4), 98.76, balance=98.76),
            api_transaction(date(2026, 8, 19), 76.54, kind="payment"),
            api_transaction(date(2026, 8, 5), 76.54, balance=76.54),
            api_transaction(date(2026, 7, 19), 102.34, kind="payment"),
            api_transaction(date(2026, 7, 5), 102.34, balance=102.34),
        ]
    }


def make_entry(
    *,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    version: int = 1,
    minor_version: int = 2,
    unique_id: str | None = ACCOUNT_NUMBER,
) -> MockConfigEntry:
    """Return a config entry for the synthetic account, as setup creates it.

    Setup also stores a refresh token when Watercare gives one; without it,
    the entry's first update signs in.
    """
    entry_data: dict[str, Any] = {
        CONF_USERNAME: EMAIL,
        CONF_PASSWORD: PASSWORD,
        DATA_STATISTICS_VERSION: STATISTICS_VERSION,
    }
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


async def stored_rows(
    hass: HomeAssistant, statistic_id: str, *, wait: bool = True
) -> list[dict[str, Any]]:
    """Return the stored rows of one statistic, once the recorder has caught up.

    With ``wait=False``, return what is stored now, even while the recorder
    is held.
    """
    if wait:
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
