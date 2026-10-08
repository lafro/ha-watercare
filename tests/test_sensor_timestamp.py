"""Tests for sensor._timestamp.

Real bug history: Watercare's usage endpoints send timestamps with
milliseconds ("...T12:00:00.000Z") but the account endpoint's dueDate does
not ("...T23:59:59Z"). A parser that only handled one form left the other
sensor silently "unknown" in production.
"""

from __future__ import annotations

from datetime import datetime, UTC

from custom_components.watercare.sensor import _timestamp


def test_parses_millisecond_form() -> None:
    result = _timestamp("2026-07-15T12:00:00.000Z")

    assert result == datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def test_parses_no_millisecond_form() -> None:
    result = _timestamp("2026-08-06T23:59:59Z")

    assert result == datetime(2026, 8, 6, 23, 59, 59, tzinfo=UTC)


def test_none_input_returns_none() -> None:
    assert _timestamp(None) is None


def test_garbage_string_returns_none() -> None:
    assert _timestamp("not-a-timestamp") is None


def test_empty_string_returns_none() -> None:
    assert _timestamp("") is None


def test_non_string_input_returns_none() -> None:
    assert _timestamp(12345) is None
    assert _timestamp(["2026-07-15T12:00:00.000Z"]) is None
