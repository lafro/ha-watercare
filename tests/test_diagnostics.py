"""Tests for the privacy-safe diagnostics."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from syrupy.assertion import SnapshotAssertion

from custom_components.watercare.api import WatercareConnectionError
from custom_components.watercare.diagnostics import async_get_config_entry_diagnostics

from .common import ACCOUNT_NUMBER, EMAIL, METER_NUMBER, PASSWORD, make_entry


async def test_diagnostics_hold_no_identifiers(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock], snapshot: SnapshotAssertion
) -> None:
    entry = make_entry(
        data={
            "username": EMAIL,
            "password": PASSWORD,
            "refresh_token": "synthetic-refresh-token",
            "statistics_version": 2,
        },
        options={
            "wastewater_ratio": 0.785,
            "tariff_overrides": {
                "2027": {"water_rate": 2.6, "wastewater_rate": 4.5, "fixed_charge": 380}
            },
        },
    )
    entry.add_to_hass(ha)
    await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)

    result = await async_get_config_entry_diagnostics(ha, entry)

    text = json.dumps(result)
    for secret in (EMAIL, PASSWORD, ACCOUNT_NUMBER, METER_NUMBER, "synthetic-refresh"):
        assert secret not in text
    assert "13000" not in text
    assert "123.45" not in text
    assert result == snapshot


async def test_diagnostics_after_a_failed_poll(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry()
    entry.add_to_hass(ha)
    await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data

    mock_api["account"].side_effect = WatercareConnectionError("down")
    await coordinator.async_refresh()

    result = await async_get_config_entry_diagnostics(ha, entry)
    assert result["coordinator"]["last_update_success"] is False
    assert result["coordinator"]["last_exception_type"] == "UpdateFailed"

    coordinator.data = None  # type: ignore[assignment]
    result = await async_get_config_entry_diagnostics(ha, entry)
    assert "billing_periods" not in result["coordinator"]
