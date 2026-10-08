"""Shared test helpers for constructing Watercare objects."""

from __future__ import annotations

from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.watercare.const import DOMAIN
from custom_components.watercare.coordinator import WatercareCoordinator

REAL_TARIFF = {
    "consumption_rate": 2.296,
    "wastewater_rate": 3.994,
    "wastewater_ratio": 0.785,
    "annual_line_charge": 332,
}


def make_coordinator(
    hass,
    *,
    consumption_rate: float = REAL_TARIFF["consumption_rate"],
    wastewater_rate: float = REAL_TARIFF["wastewater_rate"],
    wastewater_ratio: float = REAL_TARIFF["wastewater_ratio"],
    annual_line_charge: float = REAL_TARIFF["annual_line_charge"],
    endpoint: str = "mechanicalmonthly",
    api: MagicMock | None = None,
    entry: MockConfigEntry | None = None,
) -> WatercareCoordinator:
    """Build a real WatercareCoordinator instance without touching the network.

    DataUpdateCoordinator.__init__ does no I/O -- it just records attributes
    -- so this is a genuine instance of the production class, not a stub.
    """
    if entry is None:
        entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
        entry.add_to_hass(hass)
    if api is None:
        api = MagicMock()
        api.account = None
        api.account_number = None
    return WatercareCoordinator(
        hass,
        entry,
        api,
        consumption_rate,
        wastewater_rate,
        wastewater_ratio,
        annual_line_charge,
        endpoint,
    )
