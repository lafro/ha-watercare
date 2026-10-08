"""Turn a statistics export into ``recorder/import_statistics`` messages.

The rollback in docs/migration.md re-imports the four ``watercare:*``
statistics from the export taken before upgrading. The export's shapes are
not what ``recorder/import_statistics`` accepts (Home Assistant 2026.10):

- metadata from ``recorder/get_statistics_metadata`` names the unit
  ``statistics_unit_of_measurement`` and adds ``display_unit_of_measurement``
  and ``has_mean``;
- rows from ``recorder/statistics_during_period`` give ``start`` (and
  ``end``) as epoch milliseconds, and rows written by 1.4.x have
  ``state: null``.

This script converts them: ISO-8601 UTC ``start``, no ``end`` or null values,
``unit_of_measurement`` renamed, ``display_unit_of_measurement`` and
``has_mean`` dropped; ``mean_type``, ``unit_class``, ``has_sum``, ``name``,
``source`` and ``statistic_id`` kept.

Usage::

    python3 statistics_rollback.py EXPORT.json METADATA.json > import.json

EXPORT.json and METADATA.json are the two WebSocket results, saved either as
the bare ``result`` or as the whole response message. The output is a JSON
list with one message per statistic; give each an ``id`` and send it after
``recorder/clear_statistics``. Standard library only, so it runs wherever the
export was taken (Python 3.11 or newer).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROW_VALUES = ("state", "sum", "mean", "min", "max")


def _result(payload: Any) -> Any:
    """Return the ``result`` of a saved WebSocket response, or the payload."""
    if isinstance(payload, dict) and payload.get("type") == "result":
        return payload["result"]
    return payload


def _iso(value: Any) -> str:
    """Return a timestamp as ISO-8601 UTC; numbers are epoch milliseconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    return str(value)


def import_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Return ``recorder/import_statistics`` metadata for an exported entry."""
    if "mean_type" in meta:
        mean_type = meta["mean_type"]
    else:
        mean_type = 1 if meta.get("has_mean") else 0
    return {
        "has_sum": bool(meta["has_sum"]),
        "mean_type": mean_type,
        "name": meta.get("name"),
        "source": meta["source"],
        "statistic_id": meta["statistic_id"],
        "unit_class": meta.get("unit_class"),
        "unit_of_measurement": meta.get(
            "statistics_unit_of_measurement", meta.get("unit_of_measurement")
        ),
    }


def import_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return an import row for an exported row."""
    converted: dict[str, Any] = {"start": _iso(row["start"])}
    for key in _ROW_VALUES:
        if row.get(key) is not None:
            converted[key] = row[key]
    if row.get("last_reset") is not None:
        converted["last_reset"] = _iso(row["last_reset"])
    return converted


def import_messages(export: Any, metadata: Any) -> list[dict[str, Any]]:
    """Return one ``recorder/import_statistics`` message per exported series."""
    rows_by_id = _result(export)
    metas = _result(metadata)
    if not isinstance(rows_by_id, dict) or not isinstance(metas, list):
        msg = "Expected the statistics_during_period and metadata results"
        raise SystemExit(msg)
    by_id = {meta["statistic_id"]: meta for meta in metas}
    missing = sorted(set(rows_by_id) - set(by_id))
    if missing:
        msg = f"No metadata exported for: {', '.join(missing)}"
        raise SystemExit(msg)
    return [
        {
            "type": "recorder/import_statistics",
            "metadata": import_metadata(by_id[statistic_id]),
            "stats": [import_row(row) for row in rows],
        }
        for statistic_id, rows in sorted(rows_by_id.items())
        if rows
    ]


def main(argv: list[str] | None = None) -> None:
    """Print the import messages for an export and its metadata."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:  # noqa: PLR2004 - two files
        msg = "Usage: statistics_rollback.py EXPORT.json METADATA.json"
        raise SystemExit(msg)
    export, metadata = (json.loads(Path(name).read_text()) for name in args)
    json.dump(import_messages(export, metadata), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
