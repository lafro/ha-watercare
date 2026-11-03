"""Data update coordinator for the Watercare integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    WatercareApi,
    WatercareAuthError,
    WatercareConnectionError,
    WatercareError,
    WatercareMaintenanceError,
    WatercareUpgradeRequiredError,
)
from .const import (
    CONF_WASTEWATER_RATIO,
    DEFAULT_WASTEWATER_RATIO,
    DOMAIN,
    ISSUE_SMART_METER,
    ISSUE_TARIFF_MISSING,
    ISSUE_UPGRADE_REQUIRED,
    NZ_TIMEZONE,
    SMART_METER_DOCS_URL,
    TARIFF_DOCS_URL,
    UPDATE_INTERVAL,
    UPGRADE_DOCS_URL,
)
from .models import (
    AccountSummary,
    BillingPeriod,
    IssuedBill,
    RefusalReason,
    RefusedPeriod,
    WatercarePayloadError,
    bill_period,
    parse_billing_periods,
    parse_bills,
)
from .statistics import (
    BillCost,
    ImportResult,
    async_import,
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

_REFUSED: dict[RefusalReason, str] = {
    "no_usage": "has no water use figure",
    "overlaps_previous": "overlaps the period before it",
    "overlaps_stored": "overlaps days already recorded",
}


@dataclass(frozen=True, slots=True)
class WatercareData:
    """A snapshot of one successful poll."""

    account: AccountSummary | None
    latest_period: BillingPeriod
    latest_cost: BillCost | None
    latest_tariff: Tariff | None
    period_count: int
    unfinished_periods: int
    unreadable_periods: int
    duplicate_periods: int
    refused_periods: int
    last_bill: IssuedBill | None = None
    """The newest bill in the account's transactions, kept from an earlier poll
    when this one could not read them."""
    last_bill_period: BillingPeriod | None = None
    last_bill_fetch_failed: bool = False


class WatercareCoordinator(DataUpdateCoordinator[WatercareData]):
    """Fetch Watercare bills twice a day and keep the statistics current.

    A poll only talks to Watercare. The statistics are updated afterwards in
    a background task tied to the config entry, only once Home Assistant has
    started (``async_start_statistics``) and never after the entry unloads
    (``async_stop_statistics``), so neither setup nor a poll ever waits for
    the recorder. The recorder does not work through its queue until Home
    Assistant has started, and Home Assistant does not finish starting while
    a config entry is still setting up: waiting for it in setup held start-up
    until bootstrap cancelled the setup (1.5.0).
    """

    config_entry: WatercareConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: WatercareConfigEntry, api: WatercareApi
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Watercare",
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.schedule: TariffSchedule = schedule_from_options(entry.options)
        self.wastewater_ratio = Decimal(
            str(entry.options.get(CONF_WASTEWATER_RATIO, DEFAULT_WASTEWATER_RATIO))
        )
        self.statistics_status = "pending"
        """Whether a statistics update has finished since setup ("imported")."""
        self.last_import = ImportResult()
        """What the latest statistics update wrote."""
        self.missing_tariff_year: int | None = None
        """The financial year the tariff repair notice asks for, if any."""
        self._statistics_started = False
        self._statistics_periods: tuple[BillingPeriod, ...] | None = None
        self._statistics_task: asyncio.Task[None] | None = None
        self._last_bill: IssuedBill | None = None
        self._last_bill_failing = False
        self._warned_unreadable = 0
        self._warned_refused: set[tuple[date, date, RefusalReason]] = set()

    async def _async_update_data(self) -> WatercareData:
        """Fetch bills and the account; the statistics follow in the background."""
        try:
            account = await self.api.async_get_account()
            # Before the bills: a smart-meter account may have none, or get
            # an error for them.
            self._async_check_meter(account)
            payload = await self.api.async_get_billing_periods()
        except WatercareAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except WatercareMaintenanceError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="service_maintenance"
            ) from err
        except WatercareUpgradeRequiredError as err:
            self._async_check_upgrade(required=True)
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="upgrade_required"
            ) from err
        except WatercareConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        self._async_check_upgrade(required=False)

        try:
            parsed = parse_billing_periods(
                payload,
                today=dt_util.now(NZ_TIMEZONE).date(),
                smart_meter=account.smart_meter,
            )
        except WatercarePayloadError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="unexpected_response"
            ) from err
        if parsed.unfinished or parsed.duplicates:
            # Debug only: an unfinished period is left out on every poll.
            _LOGGER.debug(
                "Left out %d billing periods that end today or later and %d duplicates",
                parsed.unfinished,
                parsed.duplicates,
            )
        if not parsed.periods:
            # The error says why. The per-period warnings stay out: a setup
            # retry starts a new coordinator, so they would repeat each retry.
            _LOGGER.debug(
                "No usable billing period: %d unreadable, %d refused",
                parsed.unreadable,
                len(parsed.refused),
            )
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key=(
                    "smart_meter"
                    if account.smart_meter
                    # Bills were sent but none could be read: not a missing bill.
                    else "unexpected_response"
                    if parsed.unreadable
                    else "no_billing_periods"
                ),
            )
        if parsed.unreadable > self._warned_unreadable:
            self._warned_unreadable = parsed.unreadable
            _LOGGER.warning(
                "Could not read %d of the billing periods Watercare sent; their "
                "days have no water use in the statistics",
                parsed.unreadable,
            )
        self._log_refused(parsed.refused)

        periods = parsed.periods
        latest = periods[-1]
        self._statistics_periods = periods
        self._async_schedule_statistics()
        fetch_failed = not await self._async_update_last_bill()
        last_bill = self._last_bill

        return WatercareData(
            account=account,
            latest_period=latest,
            latest_cost=bill_cost(latest, self.schedule, self.wastewater_ratio),
            latest_tariff=self.schedule.for_day(pricing_date(latest)),
            period_count=len(periods),
            unfinished_periods=parsed.unfinished,
            unreadable_periods=parsed.unreadable,
            duplicate_periods=parsed.duplicates,
            refused_periods=len(parsed.refused),
            last_bill=last_bill,
            last_bill_period=bill_period(last_bill, periods) if last_bill else None,
            last_bill_fetch_failed=fetch_failed,
        )

    async def _async_update_last_bill(self) -> bool:
        """Read the newest bill's amount; return whether the transactions loaded.

        The amount adds to the bills the poll already has, so a failure here
        keeps the bill read before and never fails the poll. An unexpected
        error is logged once until a read succeeds again.
        """
        try:
            bills = parse_bills(await self.api.async_get_transactions())
        except (WatercareError, WatercarePayloadError) as err:
            _LOGGER.debug(
                "Could not read the Watercare bills' amounts: %s", type(err).__name__
            )
            return False
        except Exception as err:  # noqa: BLE001 - the amount never fails the poll
            if not self._last_bill_failing:
                _LOGGER.warning(
                    "Could not read the newest Watercare bill's amount because of an "
                    "unexpected error; Last bill amount keeps the amount read "
                    "earlier, if any"
                )
            # Only the error's type: its message could quote the response.
            _LOGGER.debug(
                "Reading the Watercare bills' amounts failed: unexpected %s",
                type(err).__name__,
            )
            self._last_bill_failing = True
            return False
        self._last_bill_failing = False
        self._last_bill = bills[-1] if bills else None
        return True

    def _log_refused(self, refused: Iterable[RefusedPeriod]) -> None:
        """Warn once per setup about each bill the smart-meter guard refused."""
        for item in refused:
            key = (item.period.start, item.period.end, item.reason)
            if key in self._warned_refused:
                continue
            self._warned_refused.add(key)
            _LOGGER.warning(
                "Skipped the Watercare billing period from %s to %s, which %s, "
                "because Watercare reports a smart meter",
                item.period.start.isoformat(),
                item.period.end.isoformat(),
                _REFUSED[item.reason],
            )

    @callback
    def async_start_statistics(self) -> None:
        """Keep the statistics current from now on.

        Called once Home Assistant has started (``async_setup_entry`` uses
        ``async_at_started``), with the bills of the poll that set the entry
        up, and from then on after every poll.
        """
        self._statistics_started = True
        self._async_schedule_statistics()

    @callback
    def async_stop_statistics(self) -> None:
        """Start no more statistics updates: the entry is unloading.

        Unloading cancels the running update, but a poll can still be in
        flight (one started by ``homeassistant.update_entity``, say). When it
        finishes, it must not start an update that nothing would cancel.
        """
        self._statistics_started = False
        self._statistics_periods = None

    @callback
    def _async_schedule_statistics(self) -> None:
        """Update the statistics in the background, one update at a time."""
        if not self._statistics_started:
            return
        if self._statistics_task is not None and not self._statistics_task.done():
            # The running update takes the newer bills when it finishes.
            return
        self._statistics_task = self.config_entry.async_create_background_task(
            self.hass, self._async_run_statistics(), f"{DOMAIN} statistics"
        )

    async def _async_run_statistics(self) -> None:
        """Bring the statistics up to the latest bills polled.

        Unloading the entry or stopping Home Assistant cancels this task. A
        cancellation can only take effect while it waits for the recorder or
        reads from it, before any row is queued: the rows are queued together,
        with nothing awaited in between.
        """
        while (periods := self._statistics_periods) is not None:
            self._statistics_periods = None
            try:
                result = await self._async_update_statistics(periods)
            except Exception:
                # A background task has no caller to report to. The next
                # update reads the stored history again and carries on from it.
                _LOGGER.exception(
                    "Could not update the Watercare statistics; the next update, "
                    "within 12 hours, tries again"
                )
                continue
            self.last_import = result
            self.statistics_status = "imported"
            self._log_refused(result.refused)
            self.missing_tariff_year = self._async_check_tariff(
                result.missing_tariff_year
            )

    async def _async_update_statistics(
        self, periods: tuple[BillingPeriod, ...]
    ) -> ImportResult:
        """Add the days after the stored history, or every day if none is."""
        account = self.api.account
        return await async_import(
            self.hass,
            periods,
            self.schedule,
            self.wastewater_ratio,
            smart_meter=account is not None and account.smart_meter,
        )

    @callback
    def _async_check_meter(self, account: AccountSummary) -> None:
        """Raise or clear the repair issue for a smart meter.

        Only manually read meters have the billing periods this integration
        reads, and no smart-meter answer has been seen to build support on.
        Whatever periods Watercare still lists load, behind the guard in
        ``parse_billing_periods`` and ``async_import``.
        """
        issue_id = f"{ISSUE_SMART_METER}_{self.config_entry.entry_id}"
        if not account.smart_meter:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SMART_METER,
            learn_more_url=SMART_METER_DOCS_URL,
        )

    @callback
    def _async_check_upgrade(self, *, required: bool) -> None:
        """Raise or clear the repair issue for Watercare asking for an update."""
        issue_id = f"{ISSUE_UPGRADE_REQUIRED}_{self.config_entry.entry_id}"
        if not required:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_UPGRADE_REQUIRED,
            learn_more_url=UPGRADE_DOCS_URL,
        )

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
