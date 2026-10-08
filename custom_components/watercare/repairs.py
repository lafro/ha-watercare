"""Repair flows for the Watercare integration.

Watercare reprices every 1 July and publishes no prices through its API. When
the integration has no tariff for the current financial year, cost statistics
pause (rather than guess) and a repair issue asks for the new prices from the
bill. Fixing it stores them for that year only.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .config_flow import store_year_prices, tariff_schema
from .const import DOMAIN, ISSUE_TARIFF_MISSING
from .tariffs import financial_year_label, schedule_from_options


class TariffRepairFlow(RepairsFlow):
    """Enter the prices for a financial year the integration does not know."""

    def __init__(self, entry_id: str, year: int) -> None:
        """Initialise the flow."""
        self._entry_id = entry_id
        self._year = year

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Show the prices form."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")

        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                entry, options=store_year_prices(entry.options, self._year, user_input)
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(data={})

        suggested = schedule_from_options(entry.options).best_known(self._year)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                tariff_schema(), suggested.as_options()
            ),
            description_placeholders={
                "financial_year": financial_year_label(self._year)
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the flow that fixes a Watercare repair issue."""
    del hass
    if (
        issue_id.startswith(f"{ISSUE_TARIFF_MISSING}_")
        and data is not None
        and isinstance(data.get("entry_id"), str)
        and isinstance(data.get("financial_year"), int)
    ):
        return TariffRepairFlow(data["entry_id"], data["financial_year"])
    msg = f"Unknown {DOMAIN} repair issue: {issue_id}"
    raise ValueError(msg)
