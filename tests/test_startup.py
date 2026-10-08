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
finishes anyway and the statistics are rebuilt once the recorder runs. The
first five fail on 1.5.0.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant

from custom_components.watercare.const import ALL_STATISTIC_IDS, STAT_CONSUMPTION
from custom_components.watercare.coordinator import WatercareCoordinator
from custom_components.watercare.models import BillingPeriod
from custom_components.watercare.statistics import ImportResult, async_rebuild

from .common import (
    HOLD_LIMIT,
    add_legacy_statistics,
    api_period,
    default_periods,
    hold_recorder,
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


@contextlib.contextmanager
def _statistics_updates() -> Iterator[list[int]]:
    """Record every statistics update (import or rebuild) by its bill count."""
    updates: list[int] = []
    update = WatercareCoordinator._async_update_statistics

    async def _record(
        self: WatercareCoordinator, periods: tuple[BillingPeriod, ...]
    ) -> ImportResult:
        updates.append(len(periods))
        return await update(self, periods)

    with patch.object(WatercareCoordinator, "_async_update_statistics", _record):
        yield updates


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

    # The clear and the import were queued together, so both ran.
    assert await _row_counts(ha) == dict.fromkeys(ALL_STATISTIC_IDS, REBUILT_ROWS)
    # The update was cancelled before the recorder had taken them, so the
    # rebuild is not recorded as done, and the next update repeats it.
    assert "statistics_version" not in entry.data
    await entry.runtime_data.async_refresh()
    await ha.async_block_till_done(wait_background_tasks=True)
    assert entry.data["statistics_version"] == 2
    assert entry.runtime_data.statistics_status == "rebuilt"
    assert await _row_counts(ha) == dict.fromkeys(ALL_STATISTIC_IDS, REBUILT_ROWS)


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


async def test_statistics_wait_for_home_assistant_to_start(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """No statistics update runs before Home Assistant has started.

    The recorder does not work through its queue before then, so an update
    started during setup could only wait for it.
    """
    entry = make_entry()
    entry.add_to_hass(ha)
    ha.set_state(CoreState.not_running)

    with _statistics_updates() as updates:
        await _set_up_promptly(ha, entry)
        # A poll during start-up leaves its bills for later too.
        await entry.runtime_data.async_refresh()
        await ha.async_block_till_done(wait_background_tasks=True)
        assert updates == []

        ha.set_state(CoreState.running)
        ha.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await ha.async_block_till_done(wait_background_tasks=True)
        assert updates == [3]

    assert len(await stored_rows(ha, STAT_CONSUMPTION)) == REBUILT_ROWS


async def test_a_poll_that_finishes_after_unload_starts_no_statistics_update(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """No statistics update starts after the entry unloads.

    A poll can still be fetching bills when the entry unloads (one started by
    ``homeassistant.update_entity``, say). An update it started afterwards
    would belong to an unloaded entry, and nothing would cancel it.
    """
    entry = make_entry()
    entry.add_to_hass(ha)
    await _set_up_promptly(ha, entry)
    await ha.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    fetching = asyncio.Event()
    release = asyncio.Event()

    async def _held_periods(_api: Any) -> list[dict[str, Any]]:
        fetching.set()
        await release.wait()
        return [
            api_period(date(2026, 9, 3), date(2026, 10, 2), 9000),
            *default_periods(),
        ]

    mock_api["periods"].side_effect = _held_periods
    poll = ha.async_create_task(coordinator.async_refresh())
    async with asyncio.timeout(PROMPTLY):
        await fetching.wait()
    assert await ha.config_entries.async_unload(entry.entry_id)

    with _statistics_updates() as updates:
        release.set()
        await poll
        await ha.async_block_till_done(wait_background_tasks=True)

    # The poll finished after the unload, with the new bill, and nothing ran.
    assert coordinator.data.period_count == 4
    assert updates == []
    assert len(await stored_rows(ha, STAT_CONSUMPTION)) == REBUILT_ROWS


async def test_the_rebuild_is_recorded_as_done_only_once_the_recorder_has_it(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """Queued is not written, so the marker waits for the recorder.

    At shutdown the recorder works through its queue at the final-write
    stage. If that stage times out, ``Recorder._async_close`` drops what is
    left, and that can even separate the clear from the import. So
    ``statistics_version`` is recorded only once the recorder has taken the
    rebuild's clear and import off its queue; a restart before then leaves
    the marker unset, and the next start rebuilds (the next test).
    """
    await add_legacy_statistics(ha)
    entry = make_entry(statistics_version=None)
    entry.add_to_hass(ha)
    queued = asyncio.Event()
    releases: list[threading.Event] = []

    def _rebuild_behind_a_busy_recorder(*args: Any) -> ImportResult:
        _, release = hold_recorder(ha)
        releases.append(release)
        result = async_rebuild(*args)
        queued.set()
        return result

    with patch(
        "custom_components.watercare.coordinator.async_rebuild",
        side_effect=_rebuild_behind_a_busy_recorder,
    ):
        try:
            await _set_up_promptly(ha, entry)
            async with asyncio.timeout(PROMPTLY):
                await queued.wait()
            await ha.async_block_till_done()
            # The clear and the import wait behind the busy recorder, and so
            # does the marker.
            assert "statistics_version" not in entry.data
            assert entry.runtime_data.statistics_status == "rebuild_pending"
        finally:
            for release in releases:
                release.set()
        await ha.async_block_till_done(wait_background_tasks=True)

    assert len(releases) == 1
    assert entry.data["statistics_version"] == 2
    assert entry.runtime_data.statistics_status == "rebuilt"
    assert await _row_counts(ha) == dict.fromkeys(ALL_STATISTIC_IDS, REBUILT_ROWS)


@pytest.mark.parametrize(
    ("clear_written", "rows_left"),
    [(False, LEGACY_ROWS), (True, 0)],
    ids=["whole_rebuild_dropped", "imports_dropped_after_the_clear"],
)
async def test_a_rebuild_dropped_at_shutdown_runs_again_on_the_next_start(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    clear_written: bool,
    rows_left: int,
) -> None:
    """A shutdown can drop the queued rebuild, even between clear and import.

    At shutdown the recorder works through its queue at the final-write
    stage. If that stage times out, ``Recorder._async_close`` drops what is
    left. If the recorder is running the clear by then, the imports behind it
    are dropped and the four statistics stay empty across the restart. The
    marker is unset either way, so the next start finds the history it can
    recreate and rebuilds.
    """
    await add_legacy_statistics(ha)
    entry = make_entry(statistics_version=None)
    entry.add_to_hass(ha)
    clear = Recorder.async_clear_statistics
    queued = asyncio.Event()
    holds: list[tuple[asyncio.Event, threading.Event]] = []

    def _hold_at_the_clear(self: Recorder, *args: Any, **kwargs: Any) -> None:
        # Hold the recorder thread in front of the clear, or between the
        # clear and the imports, which are queued straight after it.
        if not clear_written:
            holds.append(hold_recorder(ha))
        clear(self, *args, **kwargs)
        if clear_written:
            holds.append(hold_recorder(ha))
        queued.set()

    with patch.object(Recorder, "async_clear_statistics", _hold_at_the_clear):
        try:
            await _set_up_promptly(ha, entry)
            async with asyncio.timeout(PROMPTLY):
                await queued.wait()
                await holds[0][0].wait()
            # Stopping Home Assistant cancels the update before the final
            # write, as unloading the entry does.
            assert await ha.config_entries.async_unload(entry.entry_id)
            assert "statistics_version" not in entry.data
            # The final-write stage timed out: Recorder._async_close drops
            # whatever is still queued, here the imports and perhaps the clear.
            recorder = get_instance(ha)
            with contextlib.suppress(queue.Empty):
                while True:
                    recorder._queue.get_nowait()
        finally:
            for _, release in holds:
                release.set()
        await ha.async_block_till_done(wait_background_tasks=True)

    assert "statistics_version" not in entry.data
    assert await _row_counts(ha) == dict.fromkeys(ALL_STATISTIC_IDS, rows_left)

    # The next start rebuilds.
    assert await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)
    assert entry.data["statistics_version"] == 2
    assert entry.runtime_data.statistics_status == "rebuilt"
    assert await _row_counts(ha) == dict.fromkeys(ALL_STATISTIC_IDS, REBUILT_ROWS)
