"""Tests for user-facing strings."""

from __future__ import annotations

import json
import re
from pathlib import Path

from custom_components.watercare.sensor import SENSORS

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "watercare"


def _strings() -> dict:
    return json.loads((INTEGRATION / "strings.json").read_text())


def test_english_translation_matches_source_strings() -> None:
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text())
    assert _strings() == english


def test_every_sensor_has_a_name_and_an_icon() -> None:
    names = _strings()["entity"]["sensor"]
    icons = json.loads((INTEGRATION / "icons.json").read_text())["entity"]["sensor"]
    keys = {description.translation_key for description in SENSORS}
    assert keys == set(names) == set(icons)


def test_every_translation_key_used_in_code_exists() -> None:
    strings = _strings()
    source = "\n".join(path.read_text() for path in INTEGRATION.glob("*.py"))
    for key in re.findall(r'translation_key="([a-z_]+)"', source):
        known = (
            set(strings["exceptions"])
            | set(strings["issues"])
            | set(strings["entity"]["sensor"])
        )
        assert key in known, key
    for reason in re.findall(r'reason="([a-z_]+)"', source):
        aborts = set(strings["config"]["abort"]) | set(
            strings["issues"]["tariff_missing"]["fix_flow"]["abort"]
        )
        assert reason in aborts, reason
    for error in re.findall(r'errors\["base"\] = "([a-z_]+)"', source):
        assert error in strings["config"]["error"], error
