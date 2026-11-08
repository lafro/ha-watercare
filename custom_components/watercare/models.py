"""Typed views of Watercare API payloads.

Parsing is tolerant: unknown keys are ignored, both timestamp forms the API
uses are accepted, and a malformed billing period is skipped rather than
blanking every entity. Raw payloads are never kept, so nothing here can leak
into logs or diagnostics by accident.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Literal

from .const import NZ_TIMEZONE

_CHANGED_TO_SMART = "transitionedMeterFromMMToSM"
# A bill is issued on, or a few days after, the last day of its billing
# period. A week leaves room without reaching back to the period before.
BILL_ISSUE_DAYS: Final = timedelta(days=7)


class WatercarePayloadError(ValueError):
    """Raised when a payload does not have the expected overall shape."""


class WatercarePropertyNotListedError(Exception):
    """The login lists several properties, none of them the entry's.

    Not a connection problem: retrying will not bring the property back.
    """


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a Watercare timestamp into an aware UTC datetime.

    Usage endpoints send milliseconds ("...T12:00:00.000Z"); the account
    endpoint's due date does not ("...T23:59:59Z"). Accept both. A timestamp
    with no UTC instant, such as "0001-01-01T00:00:00+13:00", is unparseable.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        return None


def local_date(value: datetime) -> date:
    """Return the Auckland calendar date of an instant.

    Watercare serialises billing dates as Auckland midnight in UTC, so
    "2026-07-02T12:00:00.000Z" is 3 July in Auckland.
    """
    return value.astimezone(NZ_TIMEZONE).date()


@dataclass(frozen=True, slots=True)
class BillingPeriod:
    """One completed billing period from the mechanicalmonthly endpoint."""

    start: date
    end: date
    usage_litres: int
    number_of_days: int
    reading_type: str | None
    daily_average: float | None
    # Cosmetic values passed through from the payload as they are. They take
    # no part in equality or hashing, so an unexpected shape (a list, say)
    # can never break code that keys on a period.
    efficiency_band: Any = field(compare=False)
    usage_to_lower_band: Any = field(compare=False)
    raw_from: str
    raw_to: str

    @classmethod
    def from_json(cls, item: Any) -> BillingPeriod | None:
        """Build a period from one API item, or None when it is unusable."""
        if not isinstance(item, dict):
            return None
        raw_from = item.get("billingPeriodFromDate")
        raw_to = item.get("billingPeriodToDate")
        start_ts = parse_timestamp(raw_from)
        end_ts = parse_timestamp(raw_to)
        usage = _usage_litres(item.get("waterUsage"))
        if start_ts is None or end_ts is None or usage is None:
            return None
        try:
            start = local_date(start_ts)
            end = local_date(end_ts)
        except OverflowError:
            # No Auckland date, such as a "9999-12-31T23:59:59Z" placeholder.
            return None
        if end < start:
            return None

        statistics = _mapping(item.get("statistics"))
        number_of_days = _optional_number(statistics.get("numberOfDays"))
        if number_of_days is None or number_of_days < 1:
            # Watercare counts both ends of the period (3 Jun to 3 Jul is
            # 31 days), so the fallback does too.
            number_of_days = (end - start).days + 1
        efficiency = _mapping(statistics.get("efficiency"))
        return cls(
            start=start,
            end=end,
            usage_litres=usage,
            number_of_days=int(number_of_days),
            reading_type=_optional_str(item.get("readingType")),
            daily_average=_optional_number(statistics.get("dailyAverage")),
            efficiency_band=efficiency.get("currentHouseholdBand"),
            usage_to_lower_band=efficiency.get("usageToLowerBand"),
            raw_from=str(raw_from),
            raw_to=str(raw_to),
        )


type RefusalReason = Literal["no_usage", "overlaps_previous", "overlaps_stored"]
"""Why the smart-meter guard refused a billing period."""


@dataclass(frozen=True, slots=True)
class RefusedPeriod:
    """A billing period left out because Watercare reports a smart meter."""

    period: BillingPeriod
    reason: RefusalReason


@dataclass(frozen=True, slots=True)
class ParsedPeriods:
    """Billing periods parsed from one response."""

    periods: tuple[BillingPeriod, ...]
    unfinished: int
    """Periods that end today or later, or have a placeholder date."""
    unreadable: int
    """Periods that could not be read at all."""
    duplicates: int
    refused: tuple[RefusedPeriod, ...] = ()
    """Periods the smart-meter guard left out."""


def parse_billing_periods(
    payload: Any, *, today: date | None = None, smart_meter: bool = False
) -> ParsedPeriods:
    """Parse the mechanicalmonthly response.

    Duplicate periods (same start and end) are kept once, so a repeated item
    can never be counted twice. With ``today`` (an Auckland date), a period
    that ends on or after it is left out. The endpoint only returns completed
    periods, so a later end is a placeholder or an unfinished period, whose
    usage would be spread over days that have not happened. An unfinished
    period would end today, and once stored its partial usage would stay in
    the history, which only appends; a period read today loads tomorrow.

    With ``smart_meter``, Watercare reports a smart meter, whose periods have
    never been seen. A period with no usage figure is refused rather than
    read as no water, and so is one that starts before the end of the period
    before it, rather than moved after it with its whole volume, which would
    count the shared days twice. A start on that end day is allowed.
    """
    if not isinstance(payload, list):
        raise WatercarePayloadError("Billing periods payload is not a list")
    seen: set[tuple[date, date]] = set()
    periods: list[BillingPeriod] = []
    unfinished = 0
    unreadable = 0
    duplicates = 0
    refused: list[RefusedPeriod] = []
    for item in payload:
        period = BillingPeriod.from_json(item)
        if period is None:
            if _has_placeholder_date(item):
                unfinished += 1
            else:
                unreadable += 1
            continue
        if today is not None and period.end >= today:
            unfinished += 1
            continue
        key = (period.start, period.end)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        if smart_meter and item.get("waterUsage") is None:
            refused.append(RefusedPeriod(period, "no_usage"))
            continue
        periods.append(period)
    periods.sort(key=lambda period: (period.end, period.start))
    if smart_meter:
        periods = _refuse_overlaps(periods, refused)
    return ParsedPeriods(
        tuple(periods), unfinished, unreadable, duplicates, tuple(refused)
    )


def _refuse_overlaps(
    periods: list[BillingPeriod], refused: list[RefusedPeriod]
) -> list[BillingPeriod]:
    """Keep the periods that start on or after the end of the one before."""
    kept: list[BillingPeriod] = []
    for period in periods:
        if kept and period.start < kept[-1].end:
            refused.append(RefusedPeriod(period, "overlaps_previous"))
            continue
        kept.append(period)
    return kept


def _has_placeholder_date(item: Any) -> bool:
    """Whether a period has a date with no Auckland date, such as year 9999."""
    if not isinstance(item, dict):
        return False
    for key in ("billingPeriodFromDate", "billingPeriodToDate"):
        value = item.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        try:
            local_date(parsed.astimezone(UTC))
        except OverflowError:
            # "0001-01-01T00:00:00+13:00" has no UTC instant, and
            # "9999-12-31T23:59:59Z" no Auckland date.
            return True
    return False


def _account_number(record: Any) -> str | None:
    """Return an account record's number as text, or None if it has none."""
    if not isinstance(record, dict):
        return None
    number = record.get("accountNumber")
    if not isinstance(number, str | int) or isinstance(number, bool):
        return None
    return str(number) or None


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """The fields of a v1/account record that the integration uses."""

    account_number: str
    account_balance: float | None
    amount_due: float | None
    overdue_amount: float | None
    payment_due_date: str | None
    meter_type: str | None
    meter_number: str | None
    meter_changed_to_smart: bool

    @property
    def smart_meter(self) -> bool:
        """Whether Watercare reports a smart meter, or a meter changed to one.

        Watercare's app knows the meter types ``mechanical``,
        ``smartmeter-single`` and ``smartmeter-other``. Only ``mechanical``
        meters have the billing periods this integration reads. A missing
        type says nothing either way, so it does not count.
        """
        if self.meter_changed_to_smart:
            return True
        return self.meter_type is not None and self.meter_type.lower() != "mechanical"

    @classmethod
    def from_json(
        cls, payload: Any, account_number: str | None = None
    ) -> AccountSummary | None:
        """Build a summary from one account in a v1/account response.

        A login whose list holds several records (one per property, it is
        thought; never seen) gives the one with ``account_number``, and
        raises ``WatercarePropertyNotListedError`` if none has it. Otherwise
        the first record is used, whatever its number: that is the one setup
        takes, and on a one-property login a change in how the number is
        written can then never stop a working install.
        """
        if not isinstance(payload, list) or not payload:
            return None
        record = payload[0]
        if account_number is not None and len(payload) > 1:
            record = next(
                (item for item in payload if _account_number(item) == account_number),
                None,
            )
            if record is None:
                raise WatercarePropertyNotListedError
        number = _account_number(record)
        if number is None:
            return None
        meters = record.get("meters")
        if not isinstance(meters, list):
            meters = []
        meter_number: str | None = None
        if meters and isinstance(meters[0], dict):
            meter_number = _optional_str(meters[0].get("id"))
        # Watercare's app flags a meter changed from manually read to smart.
        # No answer with the flag has been seen, so where it sits is not
        # known: look on the account and on each of its meters.
        changed_to_smart = any(
            isinstance(item, dict) and item.get(_CHANGED_TO_SMART) is True
            for item in (record, *meters)
        )
        return cls(
            account_number=number,
            account_balance=_optional_number(record.get("accountBalance")),
            amount_due=_optional_number(record.get("amountDue")),
            overdue_amount=_optional_number(record.get("overdueAmount")),
            payment_due_date=(
                _optional_str(record.get("dueDate"))
                if record.get("hasDueDate")
                else None
            ),
            # A blank or padded type says nothing more than a missing one.
            meter_type=_optional_str(record.get("meterType"), strip=True),
            meter_number=meter_number,
            meter_changed_to_smart=changed_to_smart,
        )


@dataclass(frozen=True, slots=True)
class IssuedBill:
    """One bill from the account's transactions: the amount Watercare charged."""

    date: date
    amount: float


def parse_bills(payload: Any) -> tuple[IssuedBill, ...]:
    """Parse the bills in a transactions response, oldest first.

    Payments and malformed items are skipped, and so is a date with no
    Auckland date, such as a "9999-12-31T23:59:59Z" placeholder. A
    transaction's date is a UTC timestamp; whether it is Auckland midnight of
    the bill's date, as the billing periods' dates are, has not been seen, so
    it is read as the Auckland date it falls on, which is the bill's date
    either way. Bills are ordered by their timestamps, then their amounts, so
    of two bills on the same date the newest is the same whatever order
    Watercare lists them in.
    """
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise WatercarePayloadError("Transactions payload has no list of items")
    issued: list[tuple[datetime, IssuedBill]] = []
    for item in items:
        if not isinstance(item, dict) or str(item.get("type")).lower() != "bill":
            continue
        timestamp = parse_timestamp(item.get("date"))
        amount = _optional_number(item.get("amount"))
        if timestamp is None or amount is None:
            continue
        try:
            day = local_date(timestamp)
        except OverflowError:
            continue
        issued.append((timestamp, IssuedBill(day, amount)))
    issued.sort(key=lambda entry: (entry[0], entry[1].amount))
    return tuple(bill for _, bill in issued)


def bill_period(
    bill: IssuedBill, periods: tuple[BillingPeriod, ...]
) -> BillingPeriod | None:
    """Return the billing period a bill charges for, or None.

    The transactions name no period, so a bill matches the latest period that
    ends on its date or up to ``BILL_ISSUE_DAYS`` before it.
    """
    matches = [
        period
        for period in periods
        if period.end <= bill.date <= period.end + BILL_ISSUE_DAYS
    ]
    return max(matches, key=lambda period: period.end, default=None)


def _usage_litres(value: Any) -> int | None:
    """Return a usage in litres; an explicit null counts as zero."""
    if value is None:
        return 0
    number = _optional_number(value)
    if number is None or number < 0:
        return None
    return int(number)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_str(value: Any, *, strip: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value)
    if strip:
        return text.strip() or None
    return text


def _optional_number(value: Any) -> float | None:
    """Return a finite number, or None.

    The JSON decoder accepts NaN and Infinity, which would otherwise break
    the integer conversions above.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
