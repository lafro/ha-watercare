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
# Version 2 is the format since 1.5.0: one row per day, anchored to stored
# history. New entries carry it, so that going back to 1.5.1, which rebuilt
# the statistics of entries without it, continues them instead.
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

# External statistics. These ids are unchanged since 1.2.x, so the Energy
# dashboard keeps its configuration.
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
ISSUE_SMART_METER: Final = "smart_meter"
ISSUE_UPGRADE_REQUIRED: Final = "upgrade_required"
# Raised by 1.5.x and 1.6.0b1 and kept across restarts; setup deletes it.
RETIRED_ISSUE_REBUILD_SKIPPED: Final = "statistics_rebuild_skipped"

DOCS_URL: Final = "https://github.com/lafro/ha-watercare"
TARIFF_DOCS_URL: Final = f"{DOCS_URL}/blob/main/docs/tariffs.md"
SMART_METER_DOCS_URL: Final = f"{DOCS_URL}#known-limitations"
UPGRADE_DOCS_URL: Final = f"{DOCS_URL}#troubleshooting"
