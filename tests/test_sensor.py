"""Tests for the Watercare sensors."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.watercare.models import AccountSummary, BillingPeriod
from custom_components.watercare.sensor import SENSORS

from .common import api_period, make_entry


@pytest.fixture
def all_entities_enabled() -> Generator[None]:
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        new_callable=PropertyMock,
        return_value=True,
    ):
        yield


@pytest.fixture
def only_sensors() -> Generator[None]:
    with patch("custom_components.watercare.PLATFORMS", [Platform.SENSOR]):
        yield


@pytest.mark.usefixtures("all_entities_enabled", "only_sensors")
async def test_sensors(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    entry = make_entry()
    entry.add_to_hass(ha)
    await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done()

    await snapshot_platform(ha, entity_registry, snapshot, entry.entry_id)


async def test_usage_sensor_attributes_and_values(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry()
    entry.add_to_hass(ha)
    await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done()

    usage = ha.states.get("sensor.watercare_last_bill_usage")
    assert usage is not None
    assert usage.state == "13000"
    assert usage.attributes["billing_period_usage"] == 13000
    assert usage.attributes["reading_type"] == "E"
    assert usage.attributes["tariff_year"] == "2026/27"
    assert usage.attributes["consumption_rate_per_1000L"] == 2.46
    assert usage.attributes["cost_currency"] == "NZD"
    assert "meter_number" not in usage.attributes
    assert "state_class" not in usage.attributes
    assert ha.states.get("sensor.watercare_reading_type").state == "Estimate"
    # 13 kL over 30 days at 2026/27 prices:
    # 13 x 2.46 + 13 x 0.785 x 4.28 + 355.90 / 365 x 30 = 104.91
    assert ha.states.get("sensor.watercare_last_bill_cost").state == "104.91"
    assert ha.states.get("sensor.watercare_payment_due").state == (
        "2026-09-25T23:59:59+00:00"
    )
    # Disabled by default.
    assert ha.states.get("sensor.watercare_overdue_amount") is None


def test_values_without_an_account_or_cost() -> None:
    period = BillingPeriod.from_json(
        api_period(date(2026, 8, 4), date(2026, 9, 2), 13000, reading="X")
    )
    assert period is not None

    class _Data:
        account: AccountSummary | None = None
        latest_period = period
        latest_cost = None
        latest_tariff = None

    data = _Data()
    values = {
        description.key: description.value_fn(data)  # type: ignore[arg-type]
        for description in SENSORS
    }
    assert values["reading_type"] == "X"
    assert values["current_bill_cost"] is None
    assert values["payment_due_date"] is None
    assert values["account_balance"] is None
    assert values["amount_due"] is None
    assert values["overdue_amount"] is None
    assert values["meter_number"] is None
    usage = next(description for description in SENSORS if description.key == "usage")
    assert usage.attributes_fn is not None
    attributes = usage.attributes_fn(data)  # type: ignore[arg-type]
    assert attributes["current_period_cost"] is None
    assert attributes["consumption_rate_per_1000L"] is None
    assert attributes["account_balance"] is None
