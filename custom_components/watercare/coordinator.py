"""Data update coordinator for the Watercare integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import WatercareApi, WatercareAuthError, WatercareConnectionError
from .const import (
    CONF_WASTEWATER_RATIO,
    DATA_STATISTICS_VERSION,
    DEFAULT_WASTEWATER_RATIO,
    DOMAIN,
    ISSUE_REBUILD_SKIPPED,
    ISSUE_TARIFF_MISSING,
    NZ_TIMEZONE,
    STATISTICS_DOCS_URL,
    STATISTICS_VERSION,
    TARIFF_DOCS_URL,
    UPDATE_INTERVAL,
)
from .models import (
    AccountSummary,
    BillingPeriod,
    WatercarePayloadError,
    parse_billing_periods,
)
from .statistics import (
    BillCost,
    ImportResult,
    async_import,
    async_rebuild,
    async_rebuild_would_lose_history,
    bill_cost,
    pricing_date,
)
from .tariffs import (
    Tariff,
    TariffSchedule,
    financial_year,
    financial_year_label,
    schedule_from_options,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import WatercareConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WatercareData:
    """A snapshot of one successful poll."""

    account: AccountSummary | None
    latest_period: BillingPeriod
    latest_cost: BillCost | None
    latest_tariff: Tariff | None
    period_count: int
    skipped_periods: int
    duplicate_periods: int
    import_result: ImportResult = field(default_factory=ImportResult)
    missing_tariff_year: int | None = None


class WatercareCoordinator(DataUpdateCoordinator[WatercareData]):
    """Fetch Watercare bills twice a day and keep the statistics current."""

    config_entry: WatercareConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: WatercareConfigEntry, api: WatercareApi
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.schedule: TariffSchedule = schedule_from_options(entry.options)
        self.wastewater_ratio = Decimal(
            str(entry.options.get(CONF_WASTEWATER_RATIO, DEFAULT_WASTEWATER_RATIO))
        )
        self.statistics_status = (
            "current"
            if entry.data.get(DATA_STATISTICS_VERSION) == STATISTICS_VERSION
            else "rebuild_pending"
        )

    async def _async_update_data(self) -> WatercareData:
        """Fetch bills and the account, then update the statistics."""
        try:
            account = await self.api.async_get_account()
            payload = await self.api.async_get_billing_periods()
        except WatercareAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except WatercareConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err

        try:
            parsed = parse_billing_periods(payload)
        except WatercarePayloadError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="unexpected_response"
            ) from err
        if not parsed.periods:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="no_billing_periods"
            )
        if parsed.skipped or parsed.duplicates:
            _LOGGER.debug(
                "Ignored %d malformed and %d duplicate billing periods",
                parsed.skipped,
                parsed.duplicates,
            )

        periods = parsed.periods
        latest = periods[-1]
        result = await self._async_update_statistics(periods)
        missing_year = self._async_check_tariff(result.missing_tariff_year)

        return WatercareData(
            account=account,
            latest_period=latest,
            latest_cost=bill_cost(
                latest, periods, self.schedule, self.wastewater_ratio
            ),
            latest_tariff=self.schedule.for_day(pricing_date(latest)),
            period_count=len(periods),
            skipped_periods=parsed.skipped,
            duplicate_periods=parsed.duplicates,
            import_result=result,
            missing_tariff_year=missing_year,
        )

    async def _async_update_statistics(
        self, periods: tuple[BillingPeriod, ...]
    ) -> ImportResult:
        """Import new days, or rebuild once for entries from before 1.5.0."""
        entry = self.config_entry
        if entry.data.get(DATA_STATISTICS_VERSION) == STATISTICS_VERSION:
            return await async_import(
                self.hass, periods, self.schedule, self.wastewater_ratio
            )

        if await async_rebuild_would_lose_history(self.hass, periods):
            # The stored history holds bills Watercare no longer returns, so a
            # rebuild would lose them. Keep the stored rows and carry on.
            _LOGGER.warning(
                "Not rebuilding the Watercare statistics: the stored history "
                "includes bills Watercare no longer returns. New days are added "
                "after the stored history instead"
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_REBUILD_SKIPPED}_{entry.entry_id}",
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_REBUILD_SKIPPED,
                learn_more_url=STATISTICS_DOCS_URL,
            )
            self.statistics_status = "rebuild_skipped"
            result = await async_import(
                self.hass, periods, self.schedule, self.wastewater_ratio
            )
        else:
            result = await async_rebuild(
                self.hass, periods, self.schedule, self.wastewater_ratio
            )
            self.statistics_status = "rebuilt"

        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, DATA_STATISTICS_VERSION: STATISTICS_VERSION}
        )
        return result

    def _async_check_tariff(self, unpriced_year: int | None) -> int | None:
        """Raise or clear the repair issue for a financial year without prices.

        The year asked for is the earliest one that blocks the cost
        statistics, or else the current year if it has no prices yet.
        """
        today: date = dt_util.now(NZ_TIMEZONE).date()
        year: int | None = unpriced_year
        if year is None and not self.schedule.has_year(financial_year(today)):
            year = financial_year(today)
        issue_id = f"{ISSUE_TARIFF_MISSING}_{self.config_entry.entry_id}"
        if year is None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return None
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            data={"entry_id": self.config_entry.entry_id, "financial_year": year},
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TARIFF_MISSING,
            translation_placeholders={"financial_year": financial_year_label(year)},
            learn_more_url=TARIFF_DOCS_URL,
        )
        return year
