"""Data update coordinator for the Watercare integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from homeassistant.core import callback
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
    async_wait_for_queue,
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
        self.last_import = ImportResult()
        """What the latest statistics update wrote."""
        self.missing_tariff_year: int | None = None
        """The financial year the tariff repair notice asks for, if any."""
        self._statistics_started = False
        self._statistics_periods: tuple[BillingPeriod, ...] | None = None
        self._statistics_task: asyncio.Task[None] | None = None

    async def _async_update_data(self) -> WatercareData:
        """Fetch bills and the account; the statistics follow in the background."""
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
        self._statistics_periods = periods
        self._async_schedule_statistics()

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
        reads from it, never between the rebuild's clear and import. One that
        lands after the rebuild is queued and before it is recorded as done
        leaves the queued rebuild to the recorder, and the next update
        rebuilds again, to the same rows.
        """
        while (periods := self._statistics_periods) is not None:
            self._statistics_periods = None
            try:
                result = await self._async_update_statistics(periods)
            except Exception:
                # A background task has no caller to report to. Nothing that
                # failed is recorded as done, so the next poll tries again.
                _LOGGER.exception(
                    "Could not update the Watercare statistics; the next poll "
                    "tries again"
                )
                continue
            self.last_import = result
            self.missing_tariff_year = self._async_check_tariff(
                result.missing_tariff_year
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
            # Queues the clear and the import together, without awaiting.
            result = async_rebuild(
                self.hass, periods, self.schedule, self.wastewater_ratio
            )
            # Queued is not written. At shutdown the recorder works through its
            # queue at the final-write stage. If that stage times out,
            # Recorder._async_close drops what is left, and that can even
            # separate the clear from the import. Record the rebuild as done
            # only once the recorder has taken all of it. Until then the
            # marker is unset, so whatever a shutdown drops, the next start
            # rebuilds. Recorded earlier, it could say the statistics were
            # rebuilt while they still hold the 1.4.x rows, or nothing. Taken
            # is not always written: an import the recorder retries or drops
            # after this leaves its statistic empty for now, and the next
            # update imports an empty statistic in full (docs/statistics.md).
            await async_wait_for_queue(self.hass)
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
