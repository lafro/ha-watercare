"""Regression guards for sensor.py's descriptions and entity configuration.

Real bug history: the usage entity and the "current_bill_cost" description
both carried a state_class, which made Home Assistant compile a bogus
auto-generated statistic for each -- for the usage entity, this additionally
shadowed the correct external `watercare:water_consumption` statistic in the
Energy dashboard's picker (see coordinator._generate_statistics). Neither
value is a monotonically increasing counter -- each is a per-billing-period
total that resets every period -- so neither may carry a state_class.

Also covers the "meter_number" diagnostic sensor and the device-info change
that accompanied it: the device represents a cloud account (a service), not
one physical meter, so it must not claim a meter id as its serial number.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.watercare.const import DOMAIN
from custom_components.watercare.sensor import (
    SENSOR_DESCRIPTIONS,
    WatercareUsageSensor,
    _device_info,
)
from tests.helpers import make_coordinator


def _description(key: str):
    return next(d for d in SENSOR_DESCRIPTIONS if d.key == key)


def _entry_and_coordinator(hass, *, account=None):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    api = MagicMock()
    api.account = account
    api.account_number = None
    coordinator = make_coordinator(hass, entry=entry, api=api)
    return entry, coordinator


async def test_usage_sensor_has_no_state_class(hass) -> None:
    """Regression guard for FIX 1: the usage entity must not carry a
    state_class (TOTAL_INCREASING or otherwise), since it is a
    per-billing-period total that resets each period, not a monotonic
    counter. Before the fix this asserted SensorStateClass.TOTAL_INCREASING."""
    entry, coordinator = _entry_and_coordinator(hass)

    sensor = WatercareUsageSensor(entry, coordinator)

    assert sensor.state_class is None


def test_current_bill_cost_description_has_no_state_class() -> None:
    """Regression guard for FIX 2. Before the fix this asserted
    SensorStateClass.TOTAL."""
    assert _description("current_bill_cost").state_class is None


def test_account_balance_and_amount_due_still_have_no_state_class() -> None:
    """These never had a state_class; current_bill_cost should now match
    them, per the fix's rationale."""
    assert _description("account_balance").state_class is None
    assert _description("amount_due").state_class is None


def test_meter_number_description_exists_as_enabled_diagnostic() -> None:
    description = _description("meter_number")

    assert description.name == "Meter number"
    assert description.icon == "mdi:identifier"
    assert description.entity_category == EntityCategory.DIAGNOSTIC
    assert description.device_class is None
    assert description.state_class is None
    assert description.entity_registry_enabled_default is True


def test_meter_number_value_fn_reads_from_attributes() -> None:
    description = _description("meter_number")

    data = {"attributes": {"meter_number": "M12345"}}
    assert description.value_fn(data) == "M12345"


def test_meter_number_value_fn_is_none_safe_when_absent() -> None:
    """meters missing/empty on the account record must not raise -- it must
    read as None, matching _account_attributes's own None-safety."""
    description = _description("meter_number")

    assert description.value_fn({"attributes": {}}) is None
    assert description.value_fn({"attributes": {"meter_number": None}}) is None


async def test_device_is_marked_as_a_service_with_no_serial_number(hass) -> None:
    """Regression guard for FIX 6: this device represents a cloud account,
    not one physical meter, so it must be entry_type SERVICE and must not
    claim a meter id as its serial_number."""
    entry, coordinator = _entry_and_coordinator(
        hass, account={"meters": [{"id": "M999"}]}
    )

    info = _device_info(entry, coordinator)

    assert info["entry_type"] is DeviceEntryType.SERVICE
    assert "serial_number" not in info
