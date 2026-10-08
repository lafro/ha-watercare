"""Data update coordinator for the Watercare integration.

Owns fetching, parsing, cost calculation, and the import of long-term
statistics. Entities read their state from ``coordinator.data``, which holds
``{"native_value": <litres>, "attributes": {...}}``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, UTC
from typing import Any

import aiohttp

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
    StatisticMeanType,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WatercareApi, WatercareAuthError, WatercareConnectionError
from .const import DOMAIN, NZ_TIMEZONE

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(hours=12)


def _period_days(period: dict[str, Any]) -> int:
    """Length of a billing period in days.

    Watercare supplies numberOfDays; fall back to the dates if it is absent.
    """
    supplied = period.get("statistics", {}).get("numberOfDays")
    if isinstance(supplied, int) and supplied > 0:
        return supplied
    return (
        datetime.strptime(period["billingPeriodToDate"], "%Y-%m-%dT%H:%M:%S.%fZ")
        - datetime.strptime(period["billingPeriodFromDate"], "%Y-%m-%dT%H:%M:%S.%fZ")
    ).days + 1


def _account_attributes(account: dict[str, Any] | None) -> dict[str, Any]:
    """Billing fields from v1/account. The usage endpoints never carry these."""
    if not account:
        return {}
    return {
        "account_balance": account.get("accountBalance"),
        "amount_due": account.get("amountDue"),
        "overdue_amount": account.get("overdueAmount"),
        "payment_due_date": account.get("dueDate") if account.get("hasDueDate") else None,
        "meter_type": account.get("meterType"),
        "meter_number": (account.get("meters") or [{}])[0].get("id"),
    }


class WatercareCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch Watercare usage on a schedule and publish statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: WatercareApi,
        consumption_rate: float,
        wastewater_rate: float,
        wastewater_ratio: float,
        annual_line_charge: float,
        endpoint: str,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} usage",
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.consumption_rate = consumption_rate
        self.wastewater_rate = wastewater_rate
        self.wastewater_ratio = wastewater_ratio
        self.annual_line_charge = annual_line_charge
        self.endpoint = endpoint

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch and process the latest data from Watercare."""
        _LOGGER.debug("Beginning update using endpoint: %s", self.endpoint)
        try:
            response = await self.api.get_data(endpoint=self.endpoint)
        except WatercareAuthError as err:
            # Puts the entry into the "needs attention" reauth state instead
            # of failing silently in the logs. Reserved for cases where
            # Watercare itself rejected the credentials or sign-in flow.
            raise ConfigEntryAuthFailed(str(err)) from err
        except (WatercareConnectionError, aiohttp.ClientError) as err:
            # Transient/connection problems are not a credentials problem --
            # do not send the user into reauth for these.
            raise UpdateFailed(str(err)) from err

        if response is None:
            raise UpdateFailed("No response received from the Watercare API")

        # --- Endpoint dispatch -------------------------------------------
        # Only the mechanical-meter billing-period endpoint (mechanicalmonthly)
        # is processed. Smart-meter support (dailywithstats, monthly,
        # halfhourly) was removed: it was inherited from the upstream project,
        # untested, and had known defects (wrong period naming, an assumed
        # payload shape, a timezone bug in the daily path). const.py's
        # ENDPOINT_OPTIONS/ENDPOINT_DISPLAY_NAMES/STATISTIC_TYPES still
        # document those endpoints for when this is revisited.
        #
        # To re-add a smart-meter endpoint:
        #   1. Re-expose it as a choice in config_flow.py's DATA_SCHEMA and in
        #      WatercareOptionsFlowHandler's options schema (both currently
        #      hard-select mechanicalmonthly via DEFAULT_ENDPOINT).
        #   2. Add a `_process_<name>` method on this class that parses that
        #      endpoint's payload and calls async_add_external_statistics for
        #      its statistics, following the shape of `_process_data` below.
        #   3. Branch on `self.endpoint` here to call it, e.g.:
        #        if self.endpoint == "dailywithstats":
        #            return await self._process_daily_data(response)
        # -------------------------------------------------------------------
        return await self._process_data(response)

    def _calculate_cost(self, usage_litres, number_of_days):
        """Calculate the total cost based on usage and configured rates."""
        usage_thousands = usage_litres / 1000.0

        consumption_cost = usage_thousands * self.consumption_rate
        wastewater_cost = (
            usage_thousands * self.wastewater_rate * self.wastewater_ratio
        )
        line_charge = (self.annual_line_charge / 365) * number_of_days
        total_cost = consumption_cost + wastewater_cost + line_charge

        return {
            "total": total_cost,
            "consumption": consumption_cost,
            "wastewater": wastewater_cost,
            "line_charge": line_charge,
        }

    async def _process_data(self, response: str) -> dict[str, Any]:
        """Process a billing-periods API response."""
        try:
            billing_periods = json.loads(response)
        except (TypeError, json.JSONDecodeError) as err:
            raise UpdateFailed(f"Failed to parse Watercare API response: {err}") from err

        _LOGGER.debug("Processing data: %s", billing_periods)

        # Guard against a malformed payload (e.g. a dict or string instead of
        # the expected list of billing periods) before max()/.get() get a
        # chance to throw and blank out every entity.
        if not isinstance(billing_periods, list) or not billing_periods:
            raise UpdateFailed("No billing periods in the Watercare API response")

        # Get the most recent billing period for current usage. Sort rather
        # than trusting the API's ordering.
        latest_period = max(
            billing_periods, key=lambda p: p.get("billingPeriodToDate") or ""
        )
        daily_average = latest_period.get("statistics", {}).get("dailyAverage", 0)

        # An explicit `null` for waterUsage becomes 0, matching what
        # _generate_statistics already tolerates for the same field.
        billing_period_usage = latest_period.get("waterUsage") or 0
        number_of_days = _period_days(latest_period)

        cost_breakdown = self._calculate_cost(billing_period_usage, number_of_days)

        attributes = {
            "billing_period_usage": billing_period_usage,
            "daily_average": daily_average,
            "billing_period_from": latest_period.get("billingPeriodFromDate"),
            "billing_period_to": latest_period.get("billingPeriodToDate"),
            "reading_type": latest_period.get("readingType"),
            "household_efficiency_band": latest_period.get("statistics", {})
            .get("efficiency", {})
            .get("currentHouseholdBand"),
            "usage_to_lower_band": latest_period.get("statistics", {})
            .get("efficiency", {})
            .get("usageToLowerBand"),
            "current_period_cost": round(cost_breakdown["total"], 2),
            "current_period_cost_consumption": round(cost_breakdown["consumption"], 2),
            "current_period_cost_wastewater": round(cost_breakdown["wastewater"], 2),
            "consumption_rate_per_1000L": self.consumption_rate,
            "wastewater_rate_per_1000L": self.wastewater_rate,
            "endpoint": self.endpoint,
            "cost_currency": "NZD",
        }

        attributes.update(_account_attributes(self.api.account))

        # Generate external statistics for Energy Dashboard
        await self._generate_statistics(billing_periods)

        return {"native_value": billing_period_usage, "attributes": attributes}

    async def _generate_statistics(self, billing_periods) -> None:
        """Generate external statistics from billing period data.

        Two deliberate design decisions live in this method:

        (a) The running cumulative sums below are recomputed from zero over
            every period the API returns, on every poll -- nothing is read
            back from previously stored statistics. This is safe because
            mechanicalmonthly returns the FULL billing history (observed: 4+
            years in a single response), and it buys a useful property: if
            Watercare corrects a past period from Estimate to Actual, that
            correction propagates consistently into every later cumulative
            sum, because each point is re-stamped at the same `start` and
            simply overwritten with the new value. If Watercare ever started
            returning only a sliding window instead of the full history, this
            would need to read the last stored sum and add to it instead of
            recomputing from scratch.

        (b) The cost statistics below are calculated at self.consumption_rate
            / self.wastewater_rate / self.annual_line_charge -- the tariff
            currently configured, not whatever was configured historically.
            Because (a) recomputes the entire history every poll, changing
            the tariff (which reloads the config entry) retroactively
            recomputes the ENTIRE cost-statistic history at the new rate.
            That is intentional for this single flat-rate model -- it lets a
            wrong rate be corrected after the fact -- but it does mean
            historical cost figures reflect the current rate, not necessarily
            what was actually charged/paid at the time.
        """
        if not billing_periods:
            return

        period_statistics = []
        cost_statistics = []
        consumption_cost_statistics = []
        wastewater_cost_statistics = []
        running_sum = 0
        cost_running_sum = 0
        consumption_cost_running_sum = 0
        wastewater_cost_running_sum = 0

        # Sort periods by date (oldest first) for cumulative calculation
        sorted_periods = sorted(
            billing_periods, key=lambda x: x.get("billingPeriodToDate", "")
        )

        for period in sorted_periods:
            end_date_str = period.get("billingPeriodToDate")
            if end_date_str:
                try:
                    # Parse and convert to NZ timezone
                    end_date = datetime.strptime(end_date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
                    end_date = end_date.replace(tzinfo=UTC).astimezone(
                        NZ_TIMEZONE
                    )

                    period_usage = period.get("waterUsage", 0)
                    running_sum += period_usage

                    number_of_days = _period_days(period)

                    cost_breakdown = self._calculate_cost(period_usage, number_of_days)
                    cost_running_sum += cost_breakdown["total"]
                    consumption_cost_running_sum += cost_breakdown["consumption"]
                    wastewater_cost_running_sum += cost_breakdown["wastewater"]

                    # Create StatisticData with running sum (critical for Energy Dashboard)
                    period_statistics.append(
                        StatisticData(start=end_date, sum=running_sum)
                    )

                    cost_statistics.append(
                        StatisticData(start=end_date, sum=cost_running_sum)
                    )

                    if self.consumption_rate > 0:
                        consumption_cost_statistics.append(
                            StatisticData(
                                start=end_date, sum=consumption_cost_running_sum
                            )
                        )

                    if self.wastewater_rate > 0:
                        wastewater_cost_statistics.append(
                            StatisticData(
                                start=end_date, sum=wastewater_cost_running_sum
                            )
                        )

                except (ValueError, TypeError) as e:
                    _LOGGER.warning(f"Failed to parse date {end_date_str}: {e}")
                    continue

        if period_statistics:
            metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Water Consumption",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:water_consumption",
                unit_of_measurement=UnitOfVolume.LITERS,
                mean_type=StatisticMeanType.NONE,
                # The energy dashboard's water picker filters on unit_class, so
                # leaving this None hides the statistic from that picker entirely.
                unit_class="volume",
            )

            _LOGGER.debug(
                f"Adding {len(period_statistics)} water consumption statistics"
            )
            async_add_external_statistics(self.hass, metadata, period_statistics)
        else:
            _LOGGER.warning("No valid consumption statistics generated")

        if cost_statistics:
            cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Total Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:water_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(f"Adding {len(cost_statistics)} water cost statistics")
            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)
        else:
            _LOGGER.warning("No valid cost statistics generated")

        if consumption_cost_statistics and self.consumption_rate > 0:
            consumption_cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Consumption Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:consumption_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(
                f"Adding {len(consumption_cost_statistics)} consumption cost statistics"
            )
            async_add_external_statistics(
                self.hass, consumption_cost_metadata, consumption_cost_statistics
            )

        if wastewater_cost_statistics and self.wastewater_rate > 0:
            wastewater_cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Wastewater Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:wastewater_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(
                f"Adding {len(wastewater_cost_statistics)} wastewater cost statistics"
            )
            async_add_external_statistics(
                self.hass, wastewater_cost_metadata, wastewater_cost_statistics
            )

