"""Synthetic test data. Nothing here is a real account, meter or person."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.watercare.const import (
    CONF_WASTEWATER_RATIO,
    DATA_STATISTICS_VERSION,
    DOMAIN,
    NZ_TIMEZONE,
    STATISTICS_VERSION,
)

ACCOUNT_NUMBER = "1000001-01"
METER_NUMBER = "TEST-METER-0001"
EMAIL = "someone@example.com"
PASSWORD = "synthetic-password"
REFRESH_TOKEN = "synthetic-refresh-token"
ENTRY_ID = "watercare_test_entry"

ACCOUNT_PAYLOAD: list[dict[str, Any]] = [
    {
        "userAccountId": 1,
        "accountNumber": ACCOUNT_NUMBER,
        "accountName": "Test Account",
        "amountDue": 82.51,
        "accountBalance": 82.51,
        "overdueAmount": 0,
        "dueDate": "2026-10-06T23:59:59Z",
        "hasDueDate": True,
        "meterType": "mechanical",
        "meters": [{"id": METER_NUMBER}],
    }
]


def api_date(day: date) -> str:
    """Return a date the way Watercare sends it: Auckland midnight in UTC."""
    instant = datetime.combine(day, time.min, tzinfo=NZ_TIMEZONE).astimezone(UTC)
    return instant.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def api_period(
    start: date,
    end: date,
    usage: int,
    *,
    days: int | None = None,
    reading: str = "A",
) -> dict[str, Any]:
    """Return one mechanicalmonthly item."""
    number_of_days = days if days is not None else (end - start).days + 1
    return {
        "billingPeriodFromDate": api_date(start),
        "billingPeriodToDate": api_date(end),
        "waterUsage": usage,
        "readingType": reading,
        "statistics": {
            "dailyAverage": round(usage / number_of_days),
            "numberOfDays": number_of_days,
            "efficiency": {"currentHouseholdBand": 3, "usageToLowerBand": 12},
        },
    }


def default_periods() -> list[dict[str, Any]]:
    """Three consecutive bills, newest first as the API sends them."""
    return [
        api_period(date(2026, 8, 17), date(2026, 9, 15), 11000, reading="E"),
        api_period(date(2026, 7, 17), date(2026, 8, 16), 9000),
        api_period(date(2026, 6, 16), date(2026, 7, 16), 10000),
    ]


def make_entry(
    *,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    version: int = 1,
    minor_version: int = 2,
    unique_id: str | None = ACCOUNT_NUMBER,
    statistics_version: int | None = STATISTICS_VERSION,
) -> MockConfigEntry:
    """Return a config entry for the synthetic account."""
    entry_data: dict[str, Any] = {CONF_USERNAME: EMAIL, CONF_PASSWORD: PASSWORD}
    if statistics_version is not None:
        entry_data[DATA_STATISTICS_VERSION] = statistics_version
    if data is not None:
        entry_data = data
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        title="Watercare",
        version=version,
        minor_version=minor_version,
        unique_id=unique_id,
        data=entry_data,
        options=options if options is not None else {CONF_WASTEWATER_RATIO: 0.785},
    )
