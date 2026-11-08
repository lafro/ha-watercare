"""Tests for payload parsing."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest

from custom_components.watercare.models import (
    AccountSummary,
    BillingPeriod,
    IssuedBill,
    WatercarePayloadError,
    WatercarePropertyNotListedError,
    bill_period,
    local_date,
    parse_billing_periods,
    parse_bills,
    parse_timestamp,
)

from .common import (
    ACCOUNT_NUMBER,
    ACCOUNT_PAYLOAD,
    METER_NUMBER,
    SECOND_ACCOUNT_NUMBER,
    SECOND_METER_NUMBER,
    THIRD_ACCOUNT_NUMBER,
    api_period,
    api_transaction,
    default_periods,
    properties_payload,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-02T12:00:00.000Z", datetime(2026, 7, 2, 12, tzinfo=UTC)),
        ("2026-08-06T23:59:59Z", datetime(2026, 8, 6, 23, 59, 59, tzinfo=UTC)),
        ("2026-08-06T23:59:59", datetime(2026, 8, 6, 23, 59, 59, tzinfo=UTC)),
        ("2026-08-07T11:59:59+12:00", datetime(2026, 8, 6, 23, 59, 59, tzinfo=UTC)),
        ("not a date", None),
        # Out of range in UTC: placeholders a day from either end of time.
        ("0001-01-01T00:00:00+13:00", None),
        ("9999-12-31T23:59:59-13:00", None),
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
def test_non_finite_usage_makes_the_period_unreadable(usage: float) -> None:
    # json.loads (aiohttp's default decoder) accepts NaN and Infinity.
    item = api_period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    item["waterUsage"] = usage
    assert BillingPeriod.from_json(item) is None
    parsed = parse_billing_periods(
        [item, api_period(date(2026, 7, 4), date(2026, 8, 3), 8000)]
    )
    assert (parsed.unreadable, parsed.unfinished) == (1, 0)
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


@pytest.mark.parametrize("key", ["billingPeriodFromDate", "billingPeriodToDate"])
@pytest.mark.parametrize(
    "placeholder",
    [
        "9999-12-31T23:59:59Z",  # out of range in Auckland time
        "0001-01-01T00:00:00+13:00",  # out of range in UTC
    ],
)
def test_a_date_with_no_auckland_date_is_left_out_as_unfinished(
    key: str, placeholder: str
) -> None:
    item = api_period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    item[key] = placeholder
    assert BillingPeriod.from_json(item) is None
    parsed = parse_billing_periods(
        [item, api_period(date(2026, 7, 4), date(2026, 8, 3), 8000)]
    )
    # A placeholder, as for a period still open, not an unreadable one.
    assert (parsed.unfinished, parsed.unreadable) == (1, 0)
    assert [period.end for period in parsed.periods] == [date(2026, 8, 3)]


@pytest.mark.parametrize(
    ("change", "unfinished"),
    [
        pytest.param({"billingPeriodToDate": "9999-12-31T23:59:59"}, 1, id="naive"),
        pytest.param({"billingPeriodToDate": "not a date"}, 0, id="unparseable"),
        pytest.param({"billingPeriodToDate": None}, 0, id="missing"),
        pytest.param({"waterUsage": -1}, 0, id="negative-usage"),
    ],
)
def test_only_a_placeholder_date_counts_as_unfinished(
    change: dict[str, object], unfinished: int
) -> None:
    item = {**api_period(date(2026, 6, 3), date(2026, 7, 3), 12000), **change}
    parsed = parse_billing_periods([item, "not an object"])
    assert (parsed.unfinished, parsed.unreadable) == (unfinished, 2 - unfinished)


def test_periods_ending_today_or_later_are_unfinished() -> None:
    today = date(2026, 10, 8)
    far_future = api_period(date(2026, 9, 3), date(2026, 10, 3), 9000)
    far_future["billingPeriodToDate"] = "9999-12-31T00:00:00Z"
    payload = [
        api_period(date(2026, 8, 4), date(2026, 9, 2), 13000),
        api_period(date(2026, 9, 3), date(2026, 10, 7), 9000),  # ends yesterday
        api_period(date(2026, 10, 8), date(2026, 10, 8), 100),  # ends today
        api_period(date(2026, 10, 9), date(2026, 10, 9), 100),  # ends tomorrow
        far_future,
    ]
    parsed = parse_billing_periods(payload, today=today)
    assert [period.end for period in parsed.periods] == [
        date(2026, 9, 2),
        date(2026, 10, 7),
    ]
    assert (parsed.unfinished, parsed.unreadable) == (3, 0)
    # Without a today, as in the tests of other parts, nothing is cut.
    assert parse_billing_periods(payload).unfinished == 0


def test_parse_billing_periods_sorts_skips_and_dedupes() -> None:
    newest = api_period(date(2026, 7, 4), date(2026, 8, 3), 8000)
    oldest = api_period(date(2026, 6, 3), date(2026, 7, 3), 12000)
    parsed = parse_billing_periods([newest, {"junk": True}, oldest, dict(newest)])
    assert [period.end for period in parsed.periods] == [
        date(2026, 7, 3),
        date(2026, 8, 3),
    ]
    assert (parsed.unreadable, parsed.unfinished) == (1, 0)
    assert parsed.duplicates == 1
    assert parsed.refused == ()


@pytest.mark.parametrize("usage", ["missing", None])
def test_a_smart_meter_period_without_usage_is_refused(usage: object) -> None:
    item = api_period(date(2026, 7, 4), date(2026, 8, 3), 8000)
    if usage == "missing":
        del item["waterUsage"]
    else:
        item["waterUsage"] = usage
    payload = [item, api_period(date(2026, 6, 3), date(2026, 7, 3), 12000)]

    smart = parse_billing_periods(payload, smart_meter=True)
    assert [period.end for period in smart.periods] == [date(2026, 7, 3)]
    assert [(r.period.end, r.reason) for r in smart.refused] == [
        (date(2026, 8, 3), "no_usage")
    ]
    # A manually read meter keeps 1.4.1's reading: no figure is no water.
    manual = parse_billing_periods(payload)
    assert [period.usage_litres for period in manual.periods] == [12000, 0]
    assert manual.refused == ()


def test_a_smart_meter_period_that_overlaps_the_one_before_is_refused() -> None:
    payload = [
        api_period(date(2026, 7, 20), date(2026, 8, 20), 9000),  # overlaps
        api_period(date(2026, 7, 4), date(2026, 8, 3), 8000),
        api_period(date(2026, 6, 3), date(2026, 7, 3), 12000),
    ]
    smart = parse_billing_periods(payload, smart_meter=True)
    assert [period.end for period in smart.periods] == [
        date(2026, 7, 3),
        date(2026, 8, 3),
    ]
    assert [(r.period.start, r.reason) for r in smart.refused] == [
        (date(2026, 7, 20), "overlaps_previous")
    ]
    # A manually read meter keeps it; the import moves it after the days
    # already planned, as 1.4.1 did.
    assert len(parse_billing_periods(payload).periods) == 3


def test_a_smart_meter_period_may_start_on_the_day_the_one_before_ends() -> None:
    payload = [
        api_period(date(2026, 7, 3), date(2026, 8, 3), 8000),
        api_period(date(2026, 6, 3), date(2026, 7, 3), 12000),
    ]
    smart = parse_billing_periods(payload, smart_meter=True)
    assert len(smart.periods) == 2
    assert smart.refused == ()


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
    assert not account.meter_changed_to_smart
    assert not account.smart_meter


@pytest.mark.parametrize(
    ("changes", "changed", "smart"),
    [
        ({"meterType": "smartmeter-single"}, False, True),
        ({"meterType": "smartmeter-other"}, False, True),
        ({"meterType": "Mechanical"}, False, False),
        ({"meterType": None}, False, False),
        ({"meterType": ""}, False, False),
        ({"meterType": "   "}, False, False),
        ({"meterType": " mechanical "}, False, False),
        ({"meterType": " smartmeter-single "}, False, True),
        ({"transitionedMeterFromMMToSM": True}, True, True),
        ({"transitionedMeterFromMMToSM": False}, False, False),
        ({"transitionedMeterFromMMToSM": "true"}, False, False),
        (
            {"meters": [{"id": METER_NUMBER, "transitionedMeterFromMMToSM": True}]},
            True,
            True,
        ),
    ],
)
def test_account_summary_smart_meter(
    changes: dict[str, object], *, changed: bool, smart: bool
) -> None:
    account = AccountSummary.from_json([{**ACCOUNT_PAYLOAD[0], **changes}])
    assert account is not None
    assert account.meter_changed_to_smart is changed
    assert account.smart_meter is smart


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
    assert not account.smart_meter


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
    assert AccountSummary.from_json(payload, ACCOUNT_NUMBER) is None


@pytest.mark.parametrize(
    "listed",
    [
        (SECOND_ACCOUNT_NUMBER, ACCOUNT_NUMBER),
        (ACCOUNT_NUMBER, SECOND_ACCOUNT_NUMBER),
        (THIRD_ACCOUNT_NUMBER, SECOND_ACCOUNT_NUMBER, ACCOUNT_NUMBER),
    ],
)
def test_account_summary_keeps_its_property_on_a_login_with_several(
    listed: tuple[str, ...],
) -> None:
    account = AccountSummary.from_json(properties_payload(*listed), ACCOUNT_NUMBER)
    assert account is not None
    assert account.account_number == ACCOUNT_NUMBER
    assert account.meter_number == METER_NUMBER


def test_account_summary_without_a_property_takes_the_first_listed() -> None:
    # As when the integration is added: the first property Watercare lists.
    payload = properties_payload(SECOND_ACCOUNT_NUMBER, ACCOUNT_NUMBER)
    account = AccountSummary.from_json(payload)
    assert account is not None
    assert account.account_number == SECOND_ACCOUNT_NUMBER
    assert account.meter_number == SECOND_METER_NUMBER


def test_account_summary_of_one_listed_property_is_used_whatever_its_number() -> None:
    # A login that lists one property is read as in 1.6.0b2, so a change in
    # how its account number is written can never stop a working install.
    payload = properties_payload(SECOND_ACCOUNT_NUMBER)
    account = AccountSummary.from_json(payload, ACCOUNT_NUMBER)
    assert account is not None
    assert account.account_number == SECOND_ACCOUNT_NUMBER


def test_account_summary_matches_a_numeric_account_number() -> None:
    payload = [{"accountNumber": 1234567}, {"accountNumber": 7654321}]
    account = AccountSummary.from_json(payload, "7654321")
    assert account is not None
    assert account.account_number == "7654321"


@pytest.mark.parametrize(
    "payload",
    [
        properties_payload(SECOND_ACCOUNT_NUMBER, THIRD_ACCOUNT_NUMBER),
        # A record with no usable account number never matches.
        [{"accountNumber": None}, *properties_payload(SECOND_ACCOUNT_NUMBER)],
    ],
)
def test_account_summary_refuses_a_login_without_its_property(
    payload: list[object],
) -> None:
    with pytest.raises(WatercarePropertyNotListedError):
        AccountSummary.from_json(payload, ACCOUNT_NUMBER)
    with pytest.raises(WatercarePropertyNotListedError):
        AccountSummary.from_json(payload, "True")


@pytest.mark.parametrize(
    "payload",
    [
        ["not a dict", {"accountNumber": None}],
        [{"accountNumber": True}, {"accountNumber": ""}],
    ],
)
def test_account_summary_of_several_records_without_numbers_is_unreadable(
    payload: list[object],
) -> None:
    # An answer that names no property is unreadable, as a one-record answer
    # without a number is, not a login that lacks the entry's property.
    assert AccountSummary.from_json(payload, ACCOUNT_NUMBER) is None


# A transaction's date is a UTC timestamp. Whether it is Auckland midnight of
# the bill's date, as the billing periods' dates are, or UTC midnight of it,
# has not been seen: both read as the same Auckland date.
@pytest.mark.parametrize(
    "stamp",
    [
        "2026-09-03T12:00:00.000Z",  # Auckland midnight of 4 September (NZST)
        "2026-09-04T00:00:00.000Z",  # UTC midnight of 4 September
        "2026-09-04T00:00:00Z",
        "2026-09-04",  # a date alone reads as UTC midnight
    ],
)
def test_a_bills_date_is_its_auckland_date_in_either_form(stamp: str) -> None:
    item = {"date": stamp, "type": "bill", "amount": 98.76, "balance": 98.76}
    (bill,) = parse_bills({"items": [item]})
    assert bill == IssuedBill(date(2026, 9, 4), 98.76)
    parsed = parse_billing_periods(default_periods())
    period = bill_period(bill, parsed.periods)
    assert period is not None
    assert (period.start, period.end) == (date(2026, 8, 4), date(2026, 9, 2))


def test_daylight_saving_dates_read_as_the_same_day() -> None:
    # In January, Auckland midnight is 11:00 UTC the day before.
    for stamp in ("2027-01-04T11:00:00.000Z", "2027-01-05T00:00:00.000Z"):
        (bill,) = parse_bills({"items": [{"date": stamp, "type": "bill", "amount": 1}]})
        assert bill.date == date(2027, 1, 5)


def test_parse_bills_keeps_only_usable_bills_oldest_first() -> None:
    items = [
        api_transaction(date(2026, 9, 4), 98.76, balance=98.76),
        api_transaction(date(2026, 8, 19), 76.54, kind="payment"),
        {"date": "2026-08-10T00:00:00Z", "type": "Bill", "amount": 12},
        api_transaction(date(2026, 7, 5), 102.34, balance=102.34),
        {"date": "not a date", "type": "bill", "amount": 1},
        {"date": "2026-06-05T00:00:00Z", "type": "bill", "amount": "1.00"},
        {"date": "2026-06-06T00:00:00Z", "type": "bill", "amount": True},
        {"type": "bill", "amount": 1},
        # "No date" placeholders, out of range in Auckland time or in UTC.
        {"date": "9999-12-31T23:59:59Z", "type": "bill", "amount": 1},
        {"date": "0001-01-01T00:00:00+13:00", "type": "bill", "amount": 1},
        "not an item",
    ]
    assert parse_bills({"items": items}) == (
        IssuedBill(date(2026, 7, 5), 102.34),
        IssuedBill(date(2026, 8, 10), 12),
        IssuedBill(date(2026, 9, 4), 98.76),
    )
    assert parse_bills({"items": []}) == ()


def test_bills_on_one_date_are_ordered_by_time_then_amount() -> None:
    # All three are 4 September in Auckland, whatever order Watercare uses.
    midnight = {"date": "2026-09-03T12:00:00Z", "type": "bill", "amount": 50}
    later = {"date": "2026-09-04T03:00:00Z", "type": "bill", "amount": 20}
    larger = {"date": "2026-09-04T03:00:00Z", "type": "bill", "amount": 30}
    for items in ([midnight, later, larger], [larger, later, midnight]):
        assert parse_bills({"items": items}) == (
            IssuedBill(date(2026, 9, 4), 50),
            IssuedBill(date(2026, 9, 4), 20),
            IssuedBill(date(2026, 9, 4), 30),
        )


@pytest.mark.parametrize("payload", [None, [], {}, {"items": None}, {"items": {}}])
def test_parse_bills_rejects_other_shapes(payload: object) -> None:
    with pytest.raises(WatercarePayloadError):
        parse_bills(payload)


@pytest.mark.parametrize(
    ("issued", "end"),
    [
        (date(2026, 9, 2), date(2026, 9, 2)),  # on the period's last day
        (date(2026, 9, 9), date(2026, 9, 2)),  # a week after it
        (date(2026, 9, 10), None),  # more than a week after any period
        (date(2026, 8, 5), date(2026, 8, 3)),  # an older bill, its own period
        (date(2026, 9, 1), None),  # before the newest period ended
    ],
)
def test_a_bill_matches_the_period_that_ended_up_to_a_week_before(
    issued: date, end: date | None
) -> None:
    periods = parse_billing_periods(default_periods()).periods
    period = bill_period(IssuedBill(issued, 1.0), periods)
    assert (period.end if period else None) == end
