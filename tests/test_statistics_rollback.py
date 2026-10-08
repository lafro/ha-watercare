"""Rehearse the documented rollback through Home Assistant's WebSocket API.

docs/migration.md exports the four statistics before the upgrade and, to roll
back, clears them and imports the export again. These tests take the export
exactly as the WebSocket API returns it, check that Home Assistant rejects it
as it is, convert it with scripts/statistics_rollback.py, and import it back.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.watercare.const import (
    ALL_STATISTIC_IDS,
    STAT_CONSUMPTION,
    STAT_TOTAL_COST,
)
from custom_components.watercare.statistics import day_start
from scripts import statistics_rollback

EXPORT = {
    "type": "recorder/statistics_during_period",
    "start_time": "2000-01-01T00:00:00Z",
    "statistic_ids": list(ALL_STATISTIC_IDS),
    "period": "hour",
    "types": ["state", "sum"],
}
METADATA = {
    "type": "recorder/get_statistics_metadata",
    "statistic_ids": list(ALL_STATISTIC_IDS),
}


async def _add_1_4_rows(hass: HomeAssistant) -> None:
    """Two synthetic bills as 1.4.x stored them: sums only, no state."""
    for statistic_id, name, unit, sums in (
        (STAT_CONSUMPTION, "Watercare Water Consumption", "L", (12000.0, 20000.0)),
        (STAT_TOTAL_COST, "Watercare Total Cost", "NZD", (93.37, 165.02)),
    ):
        async_add_external_statistics(
            hass,
            {
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "name": name,
                "source": "watercare",
                "statistic_id": statistic_id,
                "unit_class": "volume" if unit == "L" else None,
                "unit_of_measurement": unit,
            },
            [
                {"start": day_start(date(2026, 7, 3)), "sum": sums[0]},
                {"start": day_start(date(2026, 8, 3)), "sum": sums[1]},
            ],
        )
    await async_wait_recording_done(hass)


async def _call(client: Any, message: dict[str, Any]) -> dict[str, Any]:
    await client.send_json_auto_id(message)
    return await client.receive_json()


async def test_export_round_trips_through_clear_and_import(
    ha: HomeAssistant, hass_ws_client: WebSocketGenerator, tmp_path: Path
) -> None:
    await _add_1_4_rows(ha)
    client = await hass_ws_client(ha)
    export = await _call(client, EXPORT)
    metadata = await _call(client, METADATA)
    assert export["success"]
    assert metadata["success"]
    rows = export["result"][STAT_CONSUMPTION]
    # The shapes the review found: epoch-ms start, an end, a null state.
    assert isinstance(rows[0]["start"], int | float)
    assert "end" in rows[0]
    assert rows[0].get("state") is None

    # As exported, Home Assistant rejects it.
    meta = next(
        item for item in metadata["result"] if item["statistic_id"] == STAT_CONSUMPTION
    )
    raw = await _call(
        client,
        {"type": "recorder/import_statistics", "metadata": meta, "stats": rows},
    )
    assert not raw["success"]

    # Through the script's command line, as the runbook runs it.
    export_file = tmp_path / "export.json"
    metadata_file = tmp_path / "metadata.json"
    export_file.write_text(json.dumps(export))
    metadata_file.write_text(json.dumps(metadata))
    messages = statistics_rollback.import_messages(
        json.loads(export_file.read_text()), json.loads(metadata_file.read_text())
    )
    assert [message["metadata"]["statistic_id"] for message in messages] == sorted(
        [STAT_TOTAL_COST, STAT_CONSUMPTION]
    )

    cleared = await _call(
        client,
        {"type": "recorder/clear_statistics", "statistic_ids": list(ALL_STATISTIC_IDS)},
    )
    assert cleared["success"]
    await async_wait_recording_done(ha)
    assert (await _call(client, EXPORT))["result"] == {}

    for message in messages:
        response = await _call(client, message)
        assert response["success"], response
    await async_wait_recording_done(ha)

    restored = await _call(client, EXPORT)
    assert restored["result"] == export["result"]
    restored_metadata = await _call(client, METADATA)
    assert restored_metadata["result"] == metadata["result"]


def test_cli_prints_the_messages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = {
        STAT_CONSUMPTION: [
            {"start": 1782993600000, "end": 1782997200000, "state": None, "sum": 5.0}
        ]
    }
    metadata = [
        {
            "statistic_id": STAT_CONSUMPTION,
            "display_unit_of_measurement": "L",
            "has_mean": False,
            "has_sum": True,
            "name": None,
            "source": "watercare",
            "statistics_unit_of_measurement": "L",
            "unit_class": "volume",
        }
    ]
    (tmp_path / "e.json").write_text(json.dumps({"type": "result", "result": export}))
    (tmp_path / "m.json").write_text(json.dumps(metadata))

    statistics_rollback.main([str(tmp_path / "e.json"), str(tmp_path / "m.json")])

    printed = json.loads(capsys.readouterr().out)
    assert printed == [
        {
            "type": "recorder/import_statistics",
            "metadata": {
                "has_sum": True,
                "mean_type": 0,
                "name": None,
                "source": "watercare",
                "statistic_id": STAT_CONSUMPTION,
                "unit_class": "volume",
                "unit_of_measurement": "L",
            },
            "stats": [{"start": "2026-07-02T12:00:00+00:00", "sum": 5.0}],
        }
    ]


def test_cli_rejects_bad_input(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="Usage"):
        statistics_rollback.main([])
    with pytest.raises(SystemExit, match="No metadata exported"):
        statistics_rollback.import_messages({STAT_CONSUMPTION: [{"start": 0}]}, [])
    with pytest.raises(SystemExit, match="Expected"):
        statistics_rollback.import_messages([], [])
    row = statistics_rollback.import_row(
        {"start": "2026-07-02T12:00:00+00:00", "last_reset": 0, "mean": 1.5}
    )
    assert row == {
        "start": "2026-07-02T12:00:00+00:00",
        "mean": 1.5,
        "last_reset": "1970-01-01T00:00:00+00:00",
    }
    del tmp_path
