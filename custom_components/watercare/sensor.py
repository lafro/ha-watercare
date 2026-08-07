"""Watercare sensors."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
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
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WatercareConfigEntry
from .const import DOMAIN
from .coordinator import WatercareCoordinator

_LOGGER = logging.getLogger(__name__)

READING_TYPES = {"E": "Estimate", "A": "Actual"}


def _attr(data: dict[str, Any], key: str) -> Any:
    return data.get("attributes", {}).get(key)


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, kw_only=True)
class WatercareSensorDescription(SensorEntityDescription):
    """Describes a Watercare sensor fed from coordinator data."""

    value_fn: Callable[[dict[str, Any]], Any]


# The billing-period sensors mirror the mechanical-meter (billing periods)
# endpoints. On the dailywithstats endpoint some source keys are absent and
# the affected sensors read "unknown" — acceptable until the smart-meter
# paths can be tested for real.
SENSOR_DESCRIPTIONS: tuple[WatercareSensorDescription, ...] = (
    WatercareSensorDescription(
        key="current_bill_cost",
        name="Current bill cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: _attr(data, "current_period_cost"),
    ),
    WatercareSensorDescription(
        key="daily_average",
        name="Daily average",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-percent",
        value_fn=lambda data: _attr(data, "daily_average"),
    ),
    WatercareSensorDescription(
        key="billing_period_end",
        name="Billing period end",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _timestamp(_attr(data, "billing_period_to")),
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
    # Only populated when Watercare exposes billing balances for the account
    # (typically not on direct-debit accounts), so these start disabled.
    WatercareSensorDescription(
        key="account_balance",
        name="Account balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: _attr(data, "account_balance"),
    ),
    WatercareSensorDescription(
        key="amount_due",
        name="Amount due",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: _attr(data, "amount_due"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WatercareConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Watercare sensor platform."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [WatercareUsageSensor(entry, coordinator)]
    entities.extend(
        WatercareSensor(entry, coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    )
    async_add_entities(entities)


def _device_info(entry: WatercareConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Watercare",
        manufacturer="Watercare Services",
        model="Water account",
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
    _attr_name = "Current bill usage"
    _attr_icon = "mdi:water"
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS

    def __init__(
        self, entry: WatercareConfigEntry, coordinator: WatercareCoordinator
    ) -> None:
        """Initialize Watercare Usage sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_usage"
        self._attr_device_info = _device_info(entry)

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
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> Any:
        """Return the described value."""
        return self.entity_description.value_fn(self.coordinator.data)
