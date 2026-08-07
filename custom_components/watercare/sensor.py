"""Watercare sensors."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WatercareConfigEntry
from .const import DOMAIN
from .coordinator import WatercareCoordinator

_LOGGER = logging.getLogger(__name__)

READING_TYPES = {"E": "Estimate", "A": "Actual"}

# The billing-period endpoints only ever return *completed* periods, so the
# newest one is the last issued bill -- not a period still accruing. Name the
# entities for what they hold. Only "mechanicalmonthly" is reachable today
# (see coordinator.py's endpoint dispatch comment), so "default" is always
# used; a re-added smart-meter endpoint (e.g. the daily one, which reports
# yesterday rather than a billing period) would add its own dict entry here,
# keyed by endpoint name, the same way "dailywithstats" used to be.
PERIOD_LABELS = {
    "default": {"usage": "Last bill usage", "cost": "Last bill cost"},
}


def _labels(endpoint: str) -> dict[str, str]:
    return PERIOD_LABELS.get(endpoint, PERIOD_LABELS["default"])


def _attr(data: dict[str, Any], key: str) -> Any:
    return data.get("attributes", {}).get(key)


def _timestamp(value: Any) -> datetime | None:
    """Parse a Watercare timestamp.

    The usage endpoints send milliseconds ("...T12:00:00.000Z") but the
    account endpoint's dueDate does not ("...T23:59:59Z"), so accept both.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, kw_only=True)
class WatercareSensorDescription(SensorEntityDescription):
    """Describes a Watercare sensor fed from coordinator data."""

    value_fn: Callable[[dict[str, Any]], Any]


# The billing-period sensors mirror the mechanical-meter billing-period
# payload. A re-added smart-meter endpoint may not carry every source key
# these rely on, in which case the affected sensors read "unknown" --
# acceptable, but worth testing for real before that path ships.
SENSOR_DESCRIPTIONS: tuple[WatercareSensorDescription, ...] = (
    WatercareSensorDescription(
        key="current_bill_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        # Deliberately no state_class, same reasoning as the usage entity
        # above: this is a per-billing-period total that resets each period,
        # not a monotonically increasing value, so a state_class would make
        # HA compile a bogus auto-statistic. Matches account_balance /
        # amount_due below, which correctly have no state_class either.
        suggested_display_precision=2,
        value_fn=lambda data: _attr(data, "current_period_cost"),
    ),
    WatercareSensorDescription(
        key="daily_average",
        name="Daily average",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-percent",
        suggested_display_precision=0,
        value_fn=lambda data: _attr(data, "daily_average"),
    ),
    WatercareSensorDescription(
        key="billing_period_end",
        name="Last billing period end",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _timestamp(_attr(data, "billing_period_to")),
    ),
    WatercareSensorDescription(
        key="payment_due_date",
        name="Payment due",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _timestamp(_attr(data, "payment_due_date")),
    ),
    WatercareSensorDescription(
        key="reading_type",
        name="Reading type",
        icon="mdi:counter",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: READING_TYPES.get(
            _attr(data, "reading_type"), _attr(data, "reading_type")
        ),
    ),
    WatercareSensorDescription(
        key="efficiency_band",
        name="Household efficiency band",
        icon="mdi:gauge",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _attr(data, "household_efficiency_band"),
    ),
    WatercareSensorDescription(
        key="account_balance",
        name="Account balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _attr(data, "account_balance"),
    ),
    WatercareSensorDescription(
        key="amount_due",
        name="Amount due",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _attr(data, "amount_due"),
    ),
    WatercareSensorDescription(
        key="overdue_amount",
        name="Overdue amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: _attr(data, "overdue_amount"),
    ),
    WatercareSensorDescription(
        key="meter_number",
        name="Meter number",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _attr(data, "meter_number"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WatercareConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Watercare sensor platform."""
    coordinator = entry.runtime_data
    labels = _labels(coordinator.endpoint)
    entities: list[SensorEntity] = [WatercareUsageSensor(entry, coordinator)]
    for description in SENSOR_DESCRIPTIONS:
        if description.key == "current_bill_cost":
            description = replace(description, name=labels["cost"])
        entities.append(WatercareSensor(entry, coordinator, description))
    async_add_entities(entities)


def _device_info(
    entry: WatercareConfigEntry, coordinator: WatercareCoordinator
) -> DeviceInfo:
    # This device represents a cloud account (Watercare's API), not one
    # physical meter, so it is a service rather than a piece of hardware --
    # and the meter id is not this device's serial number. The meter id is
    # instead exposed as its own diagnostic sensor (see "meter_number" in
    # SENSOR_DESCRIPTIONS).
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Watercare",
        manufacturer="Watercare Services",
        model="Water account",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://myaccount.watercare.co.nz/",
    )


class WatercareUsageSensor(
    CoordinatorEntity[WatercareCoordinator], SensorEntity
):
    """Water usage for the current billing period (or yesterday, on smart meters).

    Kept as its own class rather than a description: it carries the full
    attribute payload for backward compatibility, and its unique id is the
    migration target for pre-1.2.2 installs.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:water"
    _attr_device_class = SensorDeviceClass.WATER
    # Deliberately no state_class: this value is a per-billing-period total
    # that rises and falls (a new period restarts from zero), not a
    # monotonically increasing counter. Giving it a state_class makes HA
    # compile a bogus auto-generated statistic for it, which also shadows the
    # correct external `watercare:water_consumption` statistic in the Energy
    # dashboard's picker. Energy-dashboard data comes from that external
    # statistic (see coordinator._generate_statistics), not from this entity.
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_suggested_display_precision = 0

    def __init__(
        self, entry: WatercareConfigEntry, coordinator: WatercareCoordinator
    ) -> None:
        """Initialize Watercare Usage sensor."""
        super().__init__(coordinator)
        self._attr_name = _labels(coordinator.endpoint)["usage"]
        self._attr_unique_id = f"{entry.entry_id}_usage"
        self._attr_device_info = _device_info(entry, coordinator)

    @property
    def native_value(self) -> Any:
        """Return the usage in litres."""
        return self.coordinator.data.get("native_value")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the full billing payload, kept for backward compatibility."""
        return self.coordinator.data.get("attributes", {})


class WatercareSensor(CoordinatorEntity[WatercareCoordinator], SensorEntity):
    """A single value from the Watercare billing data."""

    _attr_has_entity_name = True
    entity_description: WatercareSensorDescription

    def __init__(
        self,
        entry: WatercareConfigEntry,
        coordinator: WatercareCoordinator,
        description: WatercareSensorDescription,
    ) -> None:
        """Initialize the sensor from its description."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(entry, coordinator)

    @property
    def native_value(self) -> Any:
        """Return the described value."""
        return self.entity_description.value_fn(self.coordinator.data)
