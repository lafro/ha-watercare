"""Tests for the tariff repair flow."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.watercare.const import DOMAIN
from custom_components.watercare.repairs import (
    TariffRepairFlow,
    async_create_fix_flow,
)

from .common import api_period, default_periods, make_entry


async def test_fix_flow_stores_the_new_year_and_reloads(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2027-08-10T12:00:00+12:00")
    mock_api["periods"].return_value = [
        api_period(date(2027, 7, 4), date(2027, 8, 3), 5000),
        *default_periods(),
    ]
    entry = make_entry()
    entry.add_to_hass(ha)
    await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)
    issue_id = f"tariff_missing_{entry.entry_id}"
    issue = ir.async_get(ha).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    flow = await async_create_fix_flow(ha, issue_id, issue.data)
    flow.hass = ha
    flow.handler = DOMAIN
    flow.issue_id = issue_id

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"] == {"financial_year": "2027/28"}
    # Nothing is pre-filled: last year's prices would be stored by a submit
    # without changes.
    assert all(
        "suggested_value" not in (key.description or {})
        for key in result["data_schema"].schema
    )

    result = await flow.async_step_init(
        {"water_rate": 2.6, "wastewater_rate": 4.5, "fixed_charge": 380}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await ha.async_block_till_done(wait_background_tasks=True)

    assert entry.options["tariff_overrides"] == {
        "2027": {"water_rate": 2.6, "wastewater_rate": 4.5, "fixed_charge": 380.0}
    }
    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is None
    assert entry.runtime_data.data.latest_cost is not None


async def test_fix_flow_pre_fills_a_year_that_became_known(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    prices = {"water_rate": 2.6, "wastewater_rate": 4.5, "fixed_charge": 380.0}
    entry = make_entry(
        options={"wastewater_ratio": 0.785, "tariff_overrides": {"2027": prices}}
    )
    entry.add_to_hass(ha)
    flow = TariffRepairFlow(entry.entry_id, 2027)
    flow.hass = ha

    result = await flow.async_step_init()

    suggested = {
        str(key): key.description["suggested_value"]  # type: ignore[index]
        for key in result["data_schema"].schema
    }
    assert suggested == prices


async def test_fix_flow_for_a_removed_entry(ha: HomeAssistant) -> None:
    flow = TariffRepairFlow("missing-entry", 2027)
    flow.hass = ha
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


@pytest.mark.parametrize(
    ("issue_id", "data"),
    [
        ("something_else", {"entry_id": "x", "financial_year": 2027}),
        ("tariff_missing_x", None),
        ("tariff_missing_x", {"entry_id": "x", "financial_year": "2027"}),
    ],
)
async def test_unknown_issues_have_no_fix_flow(
    ha: HomeAssistant, issue_id: str, data: dict[str, object] | None
) -> None:
    with pytest.raises(ValueError, match="Unknown watercare repair issue"):
        await async_create_fix_flow(ha, issue_id, data)  # type: ignore[arg-type]
