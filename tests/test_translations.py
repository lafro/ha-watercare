"""Tests for user-facing strings."""

from __future__ import annotations

import json
import re
from pathlib import Path

from custom_components.watercare import const
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


def _source() -> str:
    return "\n".join(path.read_text() for path in INTEGRATION.glob("*.py"))


def _translation_keys(source: str) -> set[str]:
    """Return the translation keys the code passes, literal or constant."""
    keys: set[str] = set()
    pattern = r'translation_key=(\([^)]*\)|"[a-z_]+"|[A-Z_]+)'
    for value in re.findall(pattern, source):
        if value.isupper():
            keys.add(getattr(const, value))
        else:
            keys.update(re.findall(r'"([a-z_]+)"', value))
    return keys


def test_every_translation_key_used_in_code_exists_and_is_used() -> None:
    strings = _strings()
    source = _source()
    used = _translation_keys(source)
    sensors = set(strings["entity"]["sensor"])
    assert used - sensors == set(strings["exceptions"]) | set(strings["issues"])
    assert sensors <= used


def test_every_issue_has_strings() -> None:
    issues = {
        value
        for name, value in vars(const).items()
        if name.startswith("ISSUE_") and isinstance(value, str)
    }
    assert issues == set(_strings()["issues"])
    # Retired issues are only deleted, so they need no strings.
    assert const.RETIRED_ISSUE_REBUILD_SKIPPED not in _strings()["issues"]


def test_every_abort_and_error_has_strings_and_is_used() -> None:
    strings = _strings()
    source = _source()
    reasons = set(re.findall(r'reason="([a-z_]+)"', source))
    # Home Assistant raises this one for _abort_if_unique_id_configured. The
    # other implicit aborts (single_instance_allowed, already_in_progress,
    # reauth_successful, reconfigure_successful) use its own strings.
    assert "_abort_if_unique_id_configured()" in source
    reasons.add("already_configured")
    fix_flow = strings["issues"]["tariff_missing"]["fix_flow"]["abort"]
    assert reasons == set(strings["config"]["abort"]) | set(fix_flow)
    errors = set(re.findall(r'errors\["base"\] = "([a-z_]+)"', source))
    assert errors == set(strings["config"]["error"]) | set(strings["options"]["error"])


def test_every_flow_step_has_strings() -> None:
    strings = _strings()
    source = (INTEGRATION / "config_flow.py").read_text()
    steps = set(strings["config"]["step"]) | set(strings["options"]["step"])
    for step in re.findall(r'step_id="([a-z_]+)"', source):
        assert step in steps, step
    new_year = strings["options"]["step"]["new_year"]
    assert new_year["data"] == strings["options"]["step"]["init"]["data"]
