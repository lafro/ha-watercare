"""Privacy-safe diagnostics for the Watercare integration.

Diagnostics get pasted into public issues, so they hold only shapes, counts,
dates and statuses: no credentials, tokens, email, account or meter numbers,
addresses, balances, usage or costs.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import WatercareConfigEntry
from .const import (
    CONF_REFRESH_TOKEN,
    CONF_WASTEWATER_RATIO,
    LEGACY_EMAIL,
)
from .tariffs import (
    LATEST_PUBLISHED_YEAR,
    financial_year_label,
    schedule_from_options,
)

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    LEGACY_EMAIL,
    "account_number",
    "meter_number",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WatercareConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    del hass
    coordinator = entry.runtime_data
    data = coordinator.data
    last_exception = coordinator.last_exception
    overrides = schedule_from_options(entry.options).overrides
    result: dict[str, Any] = {
        "config_entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "wastewater_ratio": entry.options.get(CONF_WASTEWATER_RATIO),
            "tariff_override_years": sorted(
                financial_year_label(year) for year in overrides
            ),
        },
        "tariffs": {
            "latest_published_year": financial_year_label(LATEST_PUBLISHED_YEAR)
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_exception_type": (
                type(last_exception).__name__ if last_exception is not None else None
            ),
            "statistics_status": coordinator.statistics_status,
        },
    }
    if data is not None:
        period = data.latest_period
        imported = data.import_result
        result["coordinator"].update(
            {
                "account_present": data.account is not None,
                "meter_type": data.account.meter_type if data.account else None,
                "billing_periods": data.period_count,
                "skipped_periods": data.skipped_periods,
                "duplicate_periods": data.duplicate_periods,
                "latest_period": {
                    "start": period.start.isoformat(),
                    "end": period.end.isoformat(),
                    "number_of_days": period.number_of_days,
                    "reading_type": period.reading_type,
                },
                "latest_cost_available": data.latest_cost is not None,
                "missing_tariff_year": (
                    financial_year_label(data.missing_tariff_year)
                    if data.missing_tariff_year is not None
                    else None
                ),
                "last_import": {
                    "consumption_rows": imported.consumption_rows,
                    "cost_rows": imported.cost_rows,
                    "rebuilt": imported.rebuilt,
                    "first_day_without_tariff": (
                        imported.first_day_without_tariff.isoformat()
                        if imported.first_day_without_tariff
                        else None
                    ),
                },
            }
        )
    return result
