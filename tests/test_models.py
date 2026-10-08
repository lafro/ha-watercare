"""Tests for payload parsing."""

from __future__ import annotations

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
        ("2026-07-15T12:00:00.000Z", datetime(2026, 7, 15, 12, tzinfo=UTC)),
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
    assert local_date(datetime(2026, 7, 15, 12, tzinfo=UTC)) == date(2026, 7, 16)
    assert local_date(datetime(2026, 1, 15, 11, tzinfo=UTC)) == date(2026, 1, 16)


def test_period_from_json() -> None:
    period = BillingPeriod.from_json(
        api_period(date(2026, 6, 16), date(2026, 7, 16), 10000, reading="E")
    )
    assert period is not None
    assert period.start == date(2026, 6, 16)
    assert period.end == date(2026, 7, 16)
    assert period.usage_litres == 10000
    assert period.number_of_days == 31
    assert period.reading_type == "E"
    assert period.daily_average == 323
    assert period.efficiency_band == 3
    assert period.usage_to_lower_band == 12
    assert period.raw_to.endswith(".000Z")


@pytest.mark.parametrize(
    "item",
    [
        None,
        [],
        {"billingPeriodToDate": "2026-07-15T12:00:00.000Z", "waterUsage": 1},
        {"billingPeriodFromDate": "2026-07-15T12:00:00.000Z", "waterUsage": 1},
        {
            "billingPeriodFromDate": "2026-07-15T12:00:00.000Z",
            "billingPeriodToDate": "2026-06-15T12:00:00.000Z",
            "waterUsage": 1,
        },
        {
            "billingPeriodFromDate": "2026-06-15T12:00:00.000Z",
            "billingPeriodToDate": "2026-07-15T12:00:00.000Z",
            "waterUsage": -1,
        },
        {
            "billingPeriodFromDate": "2026-06-15T12:00:00.000Z",
            "billingPeriodToDate": "2026-07-15T12:00:00.000Z",
            "waterUsage": True,
        },
        {
            "billingPeriodFromDate": "2026-06-15T12:00:00.000Z",
            "billingPeriodToDate": "2026-07-15T12:00:00.000Z",
            "waterUsage": "1000",
        },
    ],
)
def test_period_rejects_unusable_items(item: object) -> None:
    assert BillingPeriod.from_json(item) is None


def test_period_tolerates_missing_statistics() -> None:
    item = {
        "billingPeriodFromDate": "2026-06-15T12:00:00.000Z",
        "billingPeriodToDate": "2026-07-15T12:00:00.000Z",
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


@pytest.mark.parametrize("days", [0, -3, "31", None, True])
def test_period_falls_back_to_inclusive_day_count(days: object) -> None:
    item = api_period(date(2026, 6, 16), date(2026, 7, 16), 10000)
    item["statistics"]["numberOfDays"] = days
    period = BillingPeriod.from_json(item)
    assert period is not None
    assert period.number_of_days == 31


def test_parse_billing_periods_sorts_skips_and_dedupes() -> None:
    newest = api_period(date(2026, 7, 17), date(2026, 8, 16), 9000)
    oldest = api_period(date(2026, 6, 16), date(2026, 7, 16), 10000)
    parsed = parse_billing_periods([newest, {"junk": True}, oldest, dict(newest)])
    assert [period.end for period in parsed.periods] == [
        date(2026, 7, 16),
        date(2026, 8, 16),
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
    assert account.account_balance == 82.51
    assert account.amount_due == 82.51
    assert account.overdue_amount == 0
    assert account.payment_due_date == "2026-10-06T23:59:59Z"


def test_account_summary_partial_record() -> None:
    account = AccountSummary.from_json(
        [
            {
                "accountNumber": 123,
                "hasDueDate": False,
                "dueDate": "2026-10-06T23:59:59Z",
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
