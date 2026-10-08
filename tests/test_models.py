"""Tests for payload parsing."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest

from custom_components.watercare.models import (
    AccountSummary,
    BillingPeriod,
    WatercarePayloadError,
    local_date,
    parse_billing_periods,
    parse_timestamp,
)

from .common import ACCOUNT_NUMBER, ACCOUNT_PAYLOAD, METER_NUMBER, api_period


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-02T12:00:00.000Z", datetime(2026, 7, 2, 12, tzinfo=UTC)),
        ("2026-08-06T23:59:59Z", datetime(2026, 8, 6, 23, 59, 59, tzinfo=UTC)),
        ("2026-08-06T23:59:59", datetime(2026, 8, 6, 23, 59, 59, tzinfo=UTC)),
        ("2026-08-07T11:59:59+12:00", datetime(2026, 8, 6, 23, 59, 59, tzinfo=UTC)),
        ("not a date", None),
        (None, None),
        (12, None),
    ],
)
def test_parse_timestamp(value: object, expected: datetime | None) -> None:
    assert parse_timestamp(value) == expected


def test_local_date_is_auckland_date() -> None:
    # Auckland midnight in winter (UTC+12) and summer (UTC+13).
    assert local_date(datetime(2026, 7, 2, 12, tzinfo=UTC)) == date(2026, 7, 3)
    assert local_date(datetime(2026, 1, 15, 11, tzinfo=UTC)) == date(2026, 1, 16)


def test_period_from_json() -> None:
    period = BillingPeriod.from_json(
        api_period(date(2026, 6, 3), date(2026, 7, 3), 12000, reading="E")
    )
    assert period is not None
    assert period.start == date(2026, 6, 3)
    assert period.end == date(2026, 7, 3)
    assert period.usage_litres == 12000
    assert period.number_of_days == 31
    assert period.reading_type == "E"
    assert period.daily_average == 387
    assert period.efficiency_band == 2
    assert period.usage_to_lower_band == 25
    assert period.raw_to.endswith(".000Z")


@pytest.mark.parametrize(
    "item",
    [
        None,
        [],
        {"billingPeriodToDate": "2026-07-02T12:00:00.000Z", "waterUsage": 1},
        {"billingPeriodFromDate": "2026-07-02T12:00:00.000Z", "waterUsage": 1},
        {
            "billingPeriodFromDate": "2026-07-02T12:00:00.000Z",
            "billingPeriodToDate": "2026-06-02T12:00:00.000Z",
            "waterUsage": 1,
        },
        {
            "billingPeriodFromDate": "2026-06-02T12:00:00.000Z",
            "billingPeriodToDate": "2026-07-02T12:00:00.000Z",
            "waterUsage": -1,
        },
        {
            "billingPeriodFromDate": "2026-06-02T12:00:00.000Z",
            "billingPeriodToDate": "2026-07-02T12:00:00.000Z",
            "waterUsage": True,
        },
        {
            "billingPeriodFromDate": "2026-06-02T12:00:00.000Z",
            "billingPeriodToDate": "2026-07-02T12:00:00.000Z",
            "waterUsage": "1000",
        },
    ],
)
def test_period_rejects_unusable_items(item: object) -> None:
    assert BillingPeriod.from_json(item) is None


def test_period_tolerates_missing_statistics() -> None:
    item = {
        "billingPeriodFromDate": "2026-06-02T12:00:00.000Z",
        "billingPeriodToDate": "2026-07-02T12:00:00.000Z",
        "waterUsage": None,
        "statistics": "unexpected",
    }
    period = BillingPeriod.from_json(item)
    assert period is not None
    assert period.usage_litres == 0
    # Inclusive day count, like Watercare's own numberOfDays.
    assert period.number_of_days == 31
    assert period.daily_average is None
    assert period.efficiency_band is None
    assert period.reading_type is None


@pytest.mark.parametrize("days", [0, -3, "31", None, True, math.nan, math.inf])
def test_period_falls_back_to_inclusive_day_count(days: object) -> None:
    item = api_period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    item["statistics"]["numberOfDays"] = days
    period = BillingPeriod.from_json(item)
    assert period is not None
    assert period.number_of_days == 31


@pytest.mark.parametrize("usage", [math.nan, math.inf, -math.inf])
def test_non_finite_usage_skips_the_period(usage: float) -> None:
    # json.loads (aiohttp's default decoder) accepts NaN and Infinity.
    item = api_period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    item["waterUsage"] = usage
    assert BillingPeriod.from_json(item) is None
    parsed = parse_billing_periods(
        [item, api_period(date(2026, 7, 4), date(2026, 8, 3), 8000)]
    )
    assert parsed.skipped == 1
    assert len(parsed.periods) == 1


def test_non_finite_statistics_and_balances_are_dropped() -> None:
    item = api_period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    item["statistics"]["dailyAverage"] = math.nan
    period = BillingPeriod.from_json(item)
    assert period is not None
    assert period.daily_average is None
    account = AccountSummary.from_json(
        [{"accountNumber": "1000001-01", "accountBalance": math.inf}]
    )
    assert account is not None
    assert account.account_balance is None


def test_parse_billing_periods_sorts_skips_and_dedupes() -> None:
    newest = api_period(date(2026, 7, 4), date(2026, 8, 3), 8000)
    oldest = api_period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    parsed = parse_billing_periods([newest, {"junk": True}, oldest, dict(newest)])
    assert [period.end for period in parsed.periods] == [
        date(2026, 7, 3),
        date(2026, 8, 3),
    ]
    assert parsed.skipped == 1
    assert parsed.duplicates == 1


@pytest.mark.parametrize("payload", [None, {"usage": []}, "[]"])
def test_parse_billing_periods_rejects_non_lists(payload: object) -> None:
    with pytest.raises(WatercarePayloadError):
        parse_billing_periods(payload)


def test_account_summary() -> None:
    account = AccountSummary.from_json(ACCOUNT_PAYLOAD)
    assert account is not None
    assert account.account_number == ACCOUNT_NUMBER
    assert account.meter_number == METER_NUMBER
    assert account.meter_type == "mechanical"
    assert account.account_balance == 123.45
    assert account.amount_due == 123.45
    assert account.overdue_amount == 0
    assert account.payment_due_date == "2026-09-25T23:59:59Z"


def test_account_summary_partial_record() -> None:
    account = AccountSummary.from_json(
        [
            {
                "accountNumber": 123,
                "hasDueDate": False,
                "dueDate": "2026-09-25T23:59:59Z",
                "accountBalance": "12.00",
                "meters": [],
            }
        ]
    )
    assert account is not None
    assert account.account_number == "123"
    assert account.payment_due_date is None
    assert account.account_balance is None
    assert account.meter_number is None
    assert account.meter_type is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        ["not a dict"],
        [{"accountNumber": None}],
        [{"accountNumber": True}],
        [{"accountNumber": ""}],
    ],
)
def test_account_summary_rejects_unusable_payloads(payload: object) -> None:
    assert AccountSummary.from_json(payload) is None
