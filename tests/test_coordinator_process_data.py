"""Tests for WatercareCoordinator._process_data.

Real bug history (FIX 3): a malformed "latest" billing period crashed
_process_data with an unhandled exception from max()/.get(), which blanked
out every entity instead of failing cleanly with a retryable UpdateFailed:

  - An explicit `waterUsage: null` on the latest period made
    `_calculate_cost` divide None by 1000.0 (TypeError).
  - A response that parses to something other than a list of periods (e.g. a
    dict or a bare string) let a non-dict/str element reach `.get()` inside
    max()'s key function (AttributeError).

async_add_external_statistics is mocked so these tests don't need a live
recorder/database, matching test_coordinator_statistics.py.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from tests.helpers import REAL_TARIFF, make_coordinator


def _valid_period(water_usage=10000):
    return {
        "billingPeriodFromDate": "2026-07-01T00:00:00.000Z",
        "billingPeriodToDate": "2026-07-31T00:00:00.000Z",
        "waterUsage": water_usage,
        "readingType": "A",
        "statistics": {"numberOfDays": 31, "dailyAverage": 322.6},
    }


async def test_null_water_usage_in_latest_period_yields_zero_not_crash(hass) -> None:
    coordinator = make_coordinator(hass, **REAL_TARIFF)
    payload = json.dumps([_valid_period(water_usage=None)])

    with patch(
        "custom_components.watercare.coordinator.async_add_external_statistics"
    ):
        result = await coordinator._process_data(payload)

    assert result["native_value"] == 0
    assert result["attributes"]["billing_period_usage"] == 0


async def test_valid_payload_still_processes_normally(hass) -> None:
    """Guard against the hardening changing behaviour for a normal payload."""
    coordinator = make_coordinator(hass, **REAL_TARIFF)
    payload = json.dumps([_valid_period(water_usage=10000)])

    with patch(
        "custom_components.watercare.coordinator.async_add_external_statistics"
    ):
        result = await coordinator._process_data(payload)

    assert result["native_value"] == 10000
    assert result["attributes"]["billing_period_usage"] == 10000


async def test_dict_json_body_raises_update_failed(hass) -> None:
    """A response that parses to a dict rather than a list of periods must
    not reach max()/.get() -- it must fail cleanly instead."""
    coordinator = make_coordinator(hass, **REAL_TARIFF)
    payload = json.dumps({"error": "not a list"})

    with pytest.raises(UpdateFailed):
        await coordinator._process_data(payload)


async def test_string_json_body_raises_update_failed(hass) -> None:
    """A bare JSON string is also non-list and must not reach max()."""
    coordinator = make_coordinator(hass, **REAL_TARIFF)
    payload = json.dumps("not-a-list-either")

    with pytest.raises(UpdateFailed):
        await coordinator._process_data(payload)


async def test_empty_list_still_raises_update_failed(hass) -> None:
    """Pre-existing behaviour (an empty list of periods) must be preserved
    by the new isinstance guard."""
    coordinator = make_coordinator(hass, **REAL_TARIFF)
    payload = json.dumps([])

    with pytest.raises(UpdateFailed):
        await coordinator._process_data(payload)
