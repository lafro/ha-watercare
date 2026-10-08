"""Setup never waits for the recorder (the 1.5.0 start-up defect).

Home Assistant's recorder does not work through its queue until Home
Assistant has started (``Recorder._run`` waits for the started event), and
Home Assistant does not finish starting while a config entry set up during
bootstrap is still setting up. 1.5.0 rebuilt the statistics inside setup and
waited for the recorder to confirm the clear, so a restart with the rebuild
pending held bootstrap until it cancelled the setup after 300 seconds, after
the clear and before the import: the statistics were left empty.

These tests hold the recorder's thread, which is what the recorder looks like
from an integration until start-up has finished, and check that setup
finishes anyway and the statistics are rebuilt once the recorder runs. They
fail on 1.5.0.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant

from custom_components.watercare.const import ALL_STATISTIC_IDS, STAT_CONSUMPTION

from .common import (
    HOLD_LIMIT,
    add_legacy_statistics,
    api_period,
    default_periods,
    make_entry,
    recorder_held,
    stored_rows,
)

# Setup takes well under a second here. 1.5.0 waited up to 300 seconds.
PROMPTLY = 5.0
REBUILT_ROWS = 92  # one per day of the three default bills
LEGACY_ROWS = 3  # one per bill, as 1.4.x stored them


async def _set_up_promptly(hass: HomeAssistant, entry: Any) -> None:
    try:
        async with asyncio.timeout(PROMPTLY):
            await hass.config_entries.async_setup(entry.entry_id)
    except TimeoutError:
        pytest.fail("Setup waited for the recorder")
    assert entry.state is ConfigEntryState.LOADED


async def _row_counts(hass: HomeAssistant) -> dict[str, int]:
    return {
        statistic_id: len(await stored_rows(hass, statistic_id))
        for statistic_id in ALL_STATISTIC_IDS
    }


async def test_restart_with_the_rebuild_pending_does_not_hold_start_up(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """The first restart after upgrading from 1.4.x, as 1.5.0 handled it."""
    await add_legacy_statistics(ha)
    entry = make_entry(statistics_version=None)
    entry.add_to_hass(ha)
    # Bootstrap: config entries are set up before Home Assistant has started.
    ha.set_state(CoreState.not_running)

    async with recorder_held(ha) as release:
        await _set_up_promptly(ha, entry)
        sensor = ha.states.get("sensor.watercare_last_bill_usage")
        assert sensor is not None
        assert sensor.state == "13000"
        # Nothing has been cleared, and the rebuild is still to come.
        assert "statistics_version" not in entry.data
        assert entry.runtime_data.statistics_status == "rebuild_pending"

        # Start-up finishes; only then does the recorder work through its
        # queue.
        ha.set_state(CoreState.running)
        ha.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await ha.async_block_till_done()
        assert "statistics_version" not in entry.data
        release.set()

    await ha.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.statistics_status == "rebuilt"
    assert entry.data["statistics_version"] == 2
    assert await _row_counts(ha) == dict.fromkeys(ALL_STATISTIC_IDS, REBUILT_ROWS)


async def test_setup_after_start_up_does_not_wait_for_a_busy_recorder(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """A reload, or adding the integration, while the recorder is busy."""
    await add_legacy_statistics(ha)
    entry = make_entry(statistics_version=None)
    entry.add_to_hass(ha)

    async with recorder_held(ha):
        await _set_up_promptly(ha, entry)
        assert "statistics_version" not in entry.data

    await ha.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.statistics_status == "rebuilt"
    assert entry.data["statistics_version"] == 2
    assert await _row_counts(ha) == dict.fromkeys(ALL_STATISTIC_IDS, REBUILT_ROWS)


async def test_setup_does_not_wait_for_a_slow_database(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """A normal import reads the newest stored rows first; setup never waits."""
    entry = make_entry()
    entry.add_to_hass(ha)
    release = threading.Event()

    def _slow_read(*args: Any, **kwargs: Any) -> Any:
        release.wait(HOLD_LIMIT)
        return get_last_statistics(*args, **kwargs)

    with patch(
        "custom_components.watercare.statistics.get_last_statistics",
        side_effect=_slow_read,
    ):
        try:
            await _set_up_promptly(ha, entry)
        finally:
            release.set()
        await ha.async_block_till_done(wait_background_tasks=True)

    assert len(await stored_rows(ha, STAT_CONSUMPTION)) == REBUILT_ROWS


async def test_a_cancellation_cannot_fall_between_the_clear_and_the_import(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """Cancel whatever queues the clear, the moment it has queued it.

    That is where bootstrap's timeout cancelled 1.5.0. The statistics must
    end up either untouched or rebuilt, never cleared and left empty.
    """
    await add_legacy_statistics(ha)
    entry = make_entry(statistics_version=None)
    entry.add_to_hass(ha)
    clear = Recorder.async_clear_statistics

    def _clear_then_cancel(self: Recorder, *args: Any, **kwargs: Any) -> None:
        clear(self, *args, **kwargs)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()

    with patch.object(Recorder, "async_clear_statistics", _clear_then_cancel):
        setup = ha.async_create_task(ha.config_entries.async_setup(entry.entry_id))
        with contextlib.suppress(asyncio.CancelledError):
            await setup
        await ha.async_block_till_done(wait_background_tasks=True)

    assert await _row_counts(ha) == dict.fromkeys(ALL_STATISTIC_IDS, REBUILT_ROWS)
    assert entry.data["statistics_version"] == 2


async def test_unloading_while_the_rebuild_waits_leaves_the_statistics_alone(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """An unload (or shutdown) before the clear is queued cancels cleanly."""
    await add_legacy_statistics(ha)
    entry = make_entry(statistics_version=None)
    entry.add_to_hass(ha)

    async with recorder_held(ha):
        await _set_up_promptly(ha, entry)
        assert await ha.config_entries.async_unload(entry.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)

    assert "statistics_version" not in entry.data
    assert await _row_counts(ha) == dict.fromkeys(ALL_STATISTIC_IDS, LEGACY_ROWS)

    # The next setup rebuilds.
    assert await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)
    assert entry.data["statistics_version"] == 2
    assert await _row_counts(ha) == dict.fromkeys(ALL_STATISTIC_IDS, REBUILT_ROWS)


async def test_a_poll_during_a_statistics_update_is_taken_in_turn(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """One statistics update at a time; the later poll's bills follow it."""
    await add_legacy_statistics(ha)
    entry = make_entry(statistics_version=None)
    entry.add_to_hass(ha)

    async with recorder_held(ha):
        await _set_up_promptly(ha, entry)
        coordinator = entry.runtime_data
        # A new bill arrives while the rebuild waits for the recorder.
        mock_api["periods"].return_value = [
            api_period(date(2026, 9, 3), date(2026, 10, 2), 9000),
            *default_periods(),
        ]
        await coordinator.async_refresh()
        assert coordinator.data.period_count == 4
        assert "statistics_version" not in entry.data
    await ha.async_block_till_done(wait_background_tasks=True)

    # The rebuild ran first, then the new bill was added after it.
    assert coordinator.statistics_status == "rebuilt"
    assert coordinator.last_import.consumption_rows == 30
    assert not coordinator.last_import.rebuilt
    rows = await stored_rows(ha, STAT_CONSUMPTION)
    assert len(rows) == REBUILT_ROWS + 30
    assert rows[-1]["sum"] == pytest.approx(42000)
