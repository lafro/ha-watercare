"""Data update coordinator for the Watercare integration.

Owns fetching, parsing, cost calculation, and the import of long-term
statistics. Entities read their state from ``coordinator.data``, which holds
``{"native_value": <litres>, "attributes": {...}}``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import pytz

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

from .api import WatercareApi, WatercareAuthError
from .const import (
    DOMAIN,
    NZ_TIMEZONE,
    ENDPOINT_DISPLAY_NAMES,
    STATISTIC_TYPES,
)

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
            # of failing silently in the logs.
            raise ConfigEntryAuthFailed(str(err)) from err

        if response is None:
            raise UpdateFailed("No response received from the Watercare API")

        if self.endpoint == "dailywithstats":
            return await self._process_daily_data(response)
        # mechanicalmonthly, monthly, halfhourly all use billing periods.
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

    def _get_statistic_name(self, statistic_type: str) -> str:
        """Generate consistent statistic names based on endpoint and type."""
        endpoint_name = ENDPOINT_DISPLAY_NAMES.get(
            self.endpoint, self.endpoint.title()
        )
        type_name = STATISTIC_TYPES.get(statistic_type, statistic_type.title())
        return f"Watercare {endpoint_name} {type_name}"

    async def _process_data(self, response: str) -> dict[str, Any]:
        """Process a billing-periods API response."""
        try:
            billing_periods = json.loads(response)
        except (TypeError, json.JSONDecodeError) as err:
            raise UpdateFailed(f"Failed to parse Watercare API response: {err}") from err

        _LOGGER.debug("Processing data: %s", billing_periods)

        if not billing_periods:
            raise UpdateFailed("No billing periods in the Watercare API response")

        # Get the most recent billing period for current usage. Sort rather
        # than trusting the API's ordering.
        latest_period = max(
            billing_periods, key=lambda p: p.get("billingPeriodToDate") or ""
        )
        daily_average = latest_period.get("statistics", {}).get("dailyAverage", 0)

        billing_period_usage = latest_period.get("waterUsage", 0)
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
        """Generate external statistics from billing period data."""
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
                    end_date = pytz.utc.localize(end_date).astimezone(NZ_TIMEZONE)

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

    async def _process_daily_data(self, response: str) -> dict[str, Any]:
        """Process the daily data (smart meters)."""
        try:
            parsed_data = json.loads(response)
        except json.JSONDecodeError as err:
            raise UpdateFailed(
                "Failed to parse JSON response for dailywithstats endpoint"
            ) from err

        _LOGGER.debug(f"Parsed data: {parsed_data}")
        usage_data = parsed_data.get("usage", [])
        statistic_data = parsed_data.get("statistics", {})

        litresRunningSum = 0
        daily_consumption = {}

        for entry in usage_data:
            timestamp_str = entry.get("timestamp")
            litres = entry.get("litres", 0)
            timestamp = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            timestamp = pytz.utc.localize(timestamp).astimezone(NZ_TIMEZONE)
            date_str = timestamp.strftime("%Y-%m-%d")

            daily_consumption[date_str] = daily_consumption.get(date_str, 0) + litres

        _LOGGER.debug(f"Daily consumption: {daily_consumption}")

        # Assign yesterday's consumption to state
        yesterday_date = (datetime.now(NZ_TIMEZONE) - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        yesterday_consumption = daily_consumption.get(yesterday_date, 0)
        _LOGGER.debug(f"yesterday_consumption: {yesterday_consumption}")

        # Calculate cost for yesterday's consumption
        cost_breakdown = self._calculate_cost(yesterday_consumption, 1)

        efficiency_data = statistic_data.get("efficiency", {})
        attributes = {
            "yesterday_consumption": yesterday_consumption,
            "current_period_cost": round(cost_breakdown["total"], 2),
            "current_period_cost_consumption": round(cost_breakdown["consumption"], 2),
            "current_period_cost_wastewater": round(cost_breakdown["wastewater"], 2),
            "consumption_rate_per_1000L": self.consumption_rate,
            "wastewater_rate_per_1000L": self.wastewater_rate,
            "endpoint": self.endpoint,
            "cost_currency": "NZD",
            "reading_type": parsed_data.get("readingType"),
            "currentPeriodAverage": statistic_data.get("currentPeriodAverage"),
            "differenceToPreviousPeriod": statistic_data.get(
                "differenceToPreviousPeriod"
            ),
            "currentHouseholdBand": efficiency_data.get("currentHouseholdBand"),
            "usageToLowerBand": efficiency_data.get("usageToLowerBand"),
        }

        # Generate statistics for daily data
        day_statistics = []
        cost_statistics = []
        consumption_cost_statistics = []
        wastewater_cost_statistics = []
        running_cost_sum = 0
        consumption_cost_running_sum = 0
        wastewater_cost_running_sum = 0
        first = True

        for date, litres in daily_consumption.items():
            start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=NZ_TIMEZONE)

            # HASSIO statistics requires us to add values as a sum of all previous values.
            litresRunningSum += litres

            # Calculate cost for this day
            daily_cost_breakdown = self._calculate_cost(litres, 1)
            running_cost_sum += daily_cost_breakdown["total"]
            consumption_cost_running_sum += daily_cost_breakdown["consumption"]
            wastewater_cost_running_sum += daily_cost_breakdown["wastewater"]

            if first:
                reset = start
                first = False

            day_statistics.append(
                StatisticData(start=start, sum=litresRunningSum, last_reset=reset)
            )

            cost_statistics.append(StatisticData(start=start, sum=running_cost_sum))

            if self.consumption_rate > 0:
                consumption_cost_statistics.append(
                    StatisticData(start=start, sum=consumption_cost_running_sum)
                )

            if self.wastewater_rate > 0:
                wastewater_cost_statistics.append(
                    StatisticData(start=start, sum=wastewater_cost_running_sum)
                )

        if day_statistics:
            day_metadata = StatisticMetaData(
                has_sum=True,
                name=self._get_statistic_name("consumption"),
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:daily_consumption",
                unit_of_measurement=UnitOfVolume.LITERS,
                mean_type=StatisticMeanType.NONE,
                # Same as above: without "volume" the energy dashboard's water
                # picker will not list this statistic.
                unit_class="volume",
            )

            _LOGGER.debug(f"Adding {len(day_statistics)} daily consumption statistics")
            async_add_external_statistics(self.hass, day_metadata, day_statistics)
        else:
            _LOGGER.warning("No daily statistics found, skipping update")

        if cost_statistics:
            cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Daily Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:daily_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(f"Adding {len(cost_statistics)} daily cost statistics")
            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)

        if consumption_cost_statistics and self.consumption_rate > 0:
            consumption_cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Daily Consumption Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:daily_consumption_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(
                f"Adding {len(consumption_cost_statistics)} daily consumption cost statistics"
            )
            async_add_external_statistics(
                self.hass, consumption_cost_metadata, consumption_cost_statistics
            )

        if wastewater_cost_statistics and self.wastewater_rate > 0:
            wastewater_cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Daily Wastewater Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:daily_wastewater_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(
                f"Adding {len(wastewater_cost_statistics)} daily wastewater cost statistics"
            )
            async_add_external_statistics(
                self.hass, wastewater_cost_metadata, wastewater_cost_statistics
            )

        attributes.update(_account_attributes(self.api.account))

        return {"native_value": yesterday_consumption, "attributes": attributes}
