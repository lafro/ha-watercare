"""Constants for the Watercare integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final
from zoneinfo import ZoneInfo

from homeassistant.const import Platform

DOMAIN: Final = "watercare"
NZ_TIMEZONE: Final = ZoneInfo("Pacific/Auckland")
PLATFORMS: Final = [Platform.SENSOR]

# Bills are monthly and account state changes at most daily. Two cheap polls a
# day pick up a new bill on the day it is issued.
UPDATE_INTERVAL: Final = timedelta(hours=12)

# Config entry data.
CONF_REFRESH_TOKEN: Final = "refresh_token"  # noqa: S105 - a key name, not a secret
DATA_STATISTICS_VERSION: Final = "statistics_version"
# Version 2 is the 1.5.0 format: one row per day, anchored to stored history,
# priced with the tariff in force on each day. Entries without this marker get
# a one-off rebuild of their statistics (see statistics.async_rebuild).
STATISTICS_VERSION: Final = 2

# Config entry options.
CONF_WASTEWATER_RATIO: Final = "wastewater_ratio"
CONF_TARIFF_OVERRIDES: Final = "tariff_overrides"
CONF_WATER_RATE: Final = "water_rate"
CONF_WASTEWATER_RATE: Final = "wastewater_rate"
CONF_FIXED_CHARGE: Final = "fixed_charge"

# Residential wastewater volume is 78.5% of metered water (Watercare's
# published standard; apartments are typically 95%).
DEFAULT_WASTEWATER_RATIO: Final = 0.785

# Options written by 1.4.x and earlier (one flat tariff for all history).
LEGACY_CONSUMPTION_RATE: Final = "consumption_rate"
LEGACY_WASTEWATER_RATE: Final = "wastewater_rate"
LEGACY_ANNUAL_LINE_CHARGE: Final = "annual_line_charge"
LEGACY_ENDPOINT: Final = "endpoint"
LEGACY_EMAIL: Final = "email"

# External statistics. These ids are unchanged since 1.2.x so the Energy
# dashboard keeps its configuration across the upgrade.
STAT_CONSUMPTION: Final = f"{DOMAIN}:water_consumption"
STAT_TOTAL_COST: Final = f"{DOMAIN}:water_cost"
STAT_CONSUMPTION_COST: Final = f"{DOMAIN}:consumption_cost"
STAT_WASTEWATER_COST: Final = f"{DOMAIN}:wastewater_cost"
ALL_STATISTIC_IDS: Final = (
    STAT_CONSUMPTION,
    STAT_TOTAL_COST,
    STAT_CONSUMPTION_COST,
    STAT_WASTEWATER_COST,
)

# Repair issues.
ISSUE_TARIFF_MISSING: Final = "tariff_missing"
ISSUE_REBUILD_SKIPPED: Final = "statistics_rebuild_skipped"

DOCS_URL: Final = "https://github.com/lafro/ha-watercare"
TARIFF_DOCS_URL: Final = f"{DOCS_URL}/blob/main/docs/tariffs.md"
STATISTICS_DOCS_URL: Final = f"{DOCS_URL}/blob/main/docs/statistics.md"
