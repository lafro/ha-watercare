"""Watercare sensors.

Watercare only publishes completed billing periods, so these sensors describe
the last issued bill, not usage accruing now. The Energy dashboard reads the
external statistics (statistics.py), not these entities, so none of them has a
state class.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WatercareConfigEntry
from .const import DOMAIN
from .coordinator import WatercareCoordinator, WatercareData
from .models import parse_timestamp
from .tariffs import financial_year, financial_year_label

PARALLEL_UPDATES = 0

READING_TYPES = {"E": "Estimate", "A": "Actual"}


@dataclass(frozen=True, kw_only=True)
class WatercareSensorDescription(SensorEntityDescription):
    """Describes a Watercare sensor."""

    value_fn: Callable[[WatercareData], Any]
    attributes_fn: Callable[[WatercareData], dict[str, Any]] | None = None


def _round(value: Any) -> float | None:
    return round(float(value), 2) if value is not None else None


def _usage_attributes(data: WatercareData) -> dict[str, Any]:
    """Attributes kept on the usage sensor for dashboards built on 1.4.x."""
    period = data.latest_period
    cost = data.latest_cost
    tariff = data.latest_tariff
    account = data.account
    return {
        "billing_period_usage": period.usage_litres,
        "daily_average": period.daily_average,
        "billing_period_from": period.raw_from,
        "billing_period_to": period.raw_to,
        "reading_type": period.reading_type,
        "household_efficiency_band": period.efficiency_band,
        "usage_to_lower_band": period.usage_to_lower_band,
        "current_period_cost": _round(cost.total) if cost else None,
        "current_period_cost_consumption": _round(cost.water) if cost else None,
        "current_period_cost_wastewater": _round(cost.wastewater) if cost else None,
        "consumption_rate_per_1000L": _round_rate(tariff.water_rate)
        if tariff
        else None,
        "wastewater_rate_per_1000L": _round_rate(tariff.wastewater_rate)
        if tariff
        else None,
        "tariff_year": financial_year_label(financial_year(period.end)),
        "cost_currency": "NZD",
        "account_balance": account.account_balance if account else None,
        "amount_due": account.amount_due if account else None,
        "overdue_amount": account.overdue_amount if account else None,
        "payment_due_date": account.payment_due_date if account else None,
        "meter_type": account.meter_type if account else None,
    }


def _round_rate(value: Any) -> float:
    return round(float(value), 4)


def _timestamp(value: str | None) -> datetime | None:
    return parse_timestamp(value)


SENSORS: tuple[WatercareSensorDescription, ...] = (
    WatercareSensorDescription(
        key="usage",
        translation_key="last_bill_usage",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=lambda data: data.latest_period.usage_litres,
        attributes_fn=_usage_attributes,
    ),
    WatercareSensorDescription(
        key="current_bill_cost",
        translation_key="last_bill_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        suggested_display_precision=2,
        value_fn=lambda data: (
            _round(data.latest_cost.total) if data.latest_cost else None
        ),
    ),
    WatercareSensorDescription(
        key="daily_average",
        translation_key="daily_average",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.latest_period.daily_average,
    ),
    WatercareSensorDescription(
        key="billing_period_end",
        translation_key="billing_period_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _timestamp(data.latest_period.raw_to),
    ),
    WatercareSensorDescription(
        key="payment_due_date",
        translation_key="payment_due_date",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            _timestamp(data.account.payment_due_date) if data.account else None
        ),
    ),
    WatercareSensorDescription(
        key="reading_type",
        translation_key="reading_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: READING_TYPES.get(
            data.latest_period.reading_type or "", data.latest_period.reading_type
        ),
    ),
    WatercareSensorDescription(
        key="efficiency_band",
        translation_key="efficiency_band",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.latest_period.efficiency_band,
    ),
    WatercareSensorDescription(
        key="account_balance",
        translation_key="account_balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.account.account_balance if data.account else None,
    ),
    WatercareSensorDescription(
        key="amount_due",
        translation_key="amount_due",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.account.amount_due if data.account else None,
    ),
    WatercareSensorDescription(
        key="overdue_amount",
        translation_key="overdue_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NZD",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.account.overdue_amount if data.account else None,
    ),
    WatercareSensorDescription(
        key="meter_number",
        translation_key="meter_number",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.account.meter_number if data.account else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WatercareConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Watercare sensors."""
    del hass
    coordinator = entry.runtime_data
    async_add_entities(
        WatercareSensor(entry, coordinator, description) for description in SENSORS
    )


class WatercareSensor(CoordinatorEntity[WatercareCoordinator], SensorEntity):
    """One value from the latest Watercare bill or account record."""

    _attr_has_entity_name = True
    entity_description: WatercareSensorDescription

    def __init__(
        self,
        entry: WatercareConfigEntry,
        coordinator: WatercareCoordinator,
        description: WatercareSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # The device is the Watercare account (a cloud service), not a meter.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Watercare",
            manufacturer="Watercare Services",
            model="Water account",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://myaccount.watercare.co.nz/",
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return attributes, for the usage sensor only."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)
