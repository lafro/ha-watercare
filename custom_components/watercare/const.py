"""Constants for Watercare integration."""

from zoneinfo import ZoneInfo

from homeassistant.const import Platform

NZ_TIMEZONE = ZoneInfo("Pacific/Auckland")

DOMAIN = "watercare"
SENSOR_NAME = "Watercare"

# Configuration keys
CONF_CONSUMPTION_RATE = "consumption_rate"
CONF_WASTEWATER_RATE = "wastewater_rate"
CONF_WASTEWATER_RATIO = "wastewater_ratio"
CONF_ANNUAL_LINE_CHARGE = "annual_line_charge"
CONF_ENDPOINT = "endpoint"

# Default cost rate per 1000L (NZD) - typical NZ Watercare rates
DEFAULT_CONSUMPTION_RATE = 2.296  # $2.296 per 1000L
DEFAULT_WASTEWATER_RATE = 3.994  # $3.994 per 1000L
DEFAULT_WASTEWATER_RATIO = 0.785  # 78.50% of water usage is wastewater
DEFAULT_ANNUAL_LINE_CHARGE = 332  # $332 per annum fixed wastewater charge (FY2026/27)
# The integration is mechanical-monthly only for now (see coordinator.py's
# _async_update_data for why). This stays a named constant, not a literal in
# the coordinator, so re-adding smart meters is a config_flow/const change,
# not a coordinator rewrite.
DEFAULT_ENDPOINT = "mechanicalmonthly"

# Available API endpoints. Only "mechanicalmonthly" is currently selectable
# (config_flow.py no longer exposes a data-source choice) and only it is
# processed by the coordinator. The others document what Watercare's API
# offers for when smart-meter support is re-added.
ENDPOINT_OPTIONS = {
    "mechanicalmonthly": "Monthly Billing Periods (Non-Smart Meters)",
    "dailywithstats": "Daily Usage with Statistics (Smart Meters)",
    "monthly": "Monthly Usage (Smart Meters)",
    "halfhourly": "Half-hourly Usage (Smart Meters)",
}

# Endpoint display names for statistics. Retained for smart-meter re-add;
# unused while only mechanicalmonthly (handled without this lookup) runs.
ENDPOINT_DISPLAY_NAMES = {
    "mechanicalmonthly": "Water",
    "dailywithstats": "Daily",
    "monthly": "Monthly",
    "halfhourly": "Half-hourly",
}

# Statistic type names. Retained for smart-meter re-add; unused while only
# mechanicalmonthly (handled without this lookup) runs.
STATISTIC_TYPES = {
    "consumption": "Consumption",
    "cost": "Cost",
    "consumption_cost": "Consumption Cost",
    "wastewater_cost": "Wastewater Cost",
}

PLATFORMS = [
    Platform.SENSOR,
]
