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

from custom_components.watercare.const import DOMAIN
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
    await ha.async_block_till_done(wait_background_tasks=True)

    await snapshot_platform(ha, entity_registry, snapshot, entry.entry_id)


async def test_usage_sensor_attributes_and_values(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry()
    entry.add_to_hass(ha)
    await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)

    usage = ha.states.get("sensor.watercare_last_bill_usage")
    assert usage is not None
    assert usage.state == "13000"
    assert usage.attributes["tariff_year"] == "2026/27"
    assert usage.attributes["consumption_rate_per_1000L"] == 2.46
    assert usage.attributes["current_period_cost_wastewater"] == 43.66
    assert usage.attributes["meter_type"] == "mechanical"
    # 1.6.0 dropped those that repeated its own state, another sensor's or a
    # constant.
    assert not set(usage.attributes) & {
        "billing_period_usage",
        "daily_average",
        "reading_type",
        "household_efficiency_band",
        "current_period_cost",
        "account_balance",
        "amount_due",
        "overdue_amount",
        "payment_due_date",
        "cost_currency",
        "meter_number",
    }
    assert "state_class" not in usage.attributes
    assert ha.states.get("sensor.watercare_reading_type").state == "Estimate"
    # 13 kL over 30 days at 2026/27 prices, each line rounded to the cent:
    # 13 x 2.46 = 31.98; 10.205 -> 10.20 kL x 4.28 = 43.66; 355.90 / 365 x 30
    # = 29.25. Without the rounding rules it would show 104.91.
    assert ha.states.get("sensor.watercare_last_bill_cost").state == "104.89"
    assert ha.states.get("sensor.watercare_due_date").state == (
        "2026-09-25T23:59:59+00:00"
    )
    assert ha.states.get("sensor.watercare_average_daily_usage").state == "433.0"
    # Disabled by default.
    assert ha.states.get("sensor.watercare_overdue_amount") is None


async def test_last_bill_amount(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """The newest bill's amount, with its date and the period it charges for."""
    entry = make_entry()
    entry.add_to_hass(ha)
    await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)

    amount = ha.states.get("sensor.watercare_last_bill_amount")
    assert amount is not None
    assert amount.state == "98.76"
    assert amount.attributes["unit_of_measurement"] == "NZD"
    assert "state_class" not in amount.attributes
    assert amount.attributes["bill_date"] == "2026-09-04"
    assert amount.attributes["billing_period_start"] == "2026-08-04"
    assert amount.attributes["billing_period_end"] == "2026-09-02"


async def test_renamed_sensors_keep_their_entity_ids(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    entity_registry: er.EntityRegistry,
) -> None:
    """An installation from before 1.6.0 keeps the entity IDs it had."""
    entry = make_entry()
    entry.add_to_hass(ha)
    for key, object_id in (
        ("daily_average", "watercare_daily_average"),
        ("payment_due_date", "watercare_payment_due"),
    ):
        entity_registry.async_get_or_create(
            Platform.SENSOR,
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            suggested_object_id=object_id,
        )
    await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)

    average = ha.states.get("sensor.watercare_daily_average")
    assert average is not None
    assert average.attributes["friendly_name"] == "Watercare Average daily usage"
    due = ha.states.get("sensor.watercare_payment_due")
    assert due is not None
    assert due.attributes["friendly_name"] == "Watercare Due date"
    assert ha.states.get("sensor.watercare_average_daily_usage") is None
    assert ha.states.get("sensor.watercare_due_date") is None


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
        last_bill = None
        last_bill_period = None

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
    assert values["last_bill_amount"] is None
    amount = next(item for item in SENSORS if item.key == "last_bill_amount")
    assert amount.attributes_fn is not None
    assert amount.attributes_fn(data) == {  # type: ignore[arg-type]
        "bill_date": None,
        "billing_period_start": None,
        "billing_period_end": None,
    }
    usage = next(description for description in SENSORS if description.key == "usage")
    assert usage.attributes_fn is not None
    attributes = usage.attributes_fn(data)  # type: ignore[arg-type]
    assert attributes["current_period_cost_consumption"] is None
    assert attributes["consumption_rate_per_1000L"] is None
    assert attributes["meter_type"] is None
