"""Typed views of Watercare API payloads.

Parsing is tolerant: unknown keys are ignored, both timestamp forms the API
uses are accepted, and a malformed billing period is skipped rather than
blanking every entity. Raw payloads are never kept, so nothing here can leak
into logs or diagnostics by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .const import NZ_TIMEZONE


class WatercarePayloadError(ValueError):
    """Raised when a payload does not have the expected overall shape."""


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a Watercare timestamp into an aware UTC datetime.

    Usage endpoints send milliseconds ("...T12:00:00.000Z"); the account
    endpoint's due date does not ("...T23:59:59Z"). Accept both.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def local_date(value: datetime) -> date:
    """Return the Auckland calendar date of an instant.

    Watercare serialises billing dates as Auckland midnight in UTC, so
    "2026-07-15T12:00:00.000Z" is 16 July in Auckland.
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
    efficiency_band: Any
    usage_to_lower_band: Any
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
        start = local_date(start_ts)
        end = local_date(end_ts)
        if end < start:
            return None

        statistics = _mapping(item.get("statistics"))
        number_of_days = _optional_number(statistics.get("numberOfDays"))
        if number_of_days is None or number_of_days < 1:
            # Watercare counts both ends of the period (16 Jun to 16 Jul is
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


@dataclass(frozen=True, slots=True)
class ParsedPeriods:
    """Billing periods parsed from one response."""

    periods: tuple[BillingPeriod, ...]
    skipped: int
    duplicates: int


def parse_billing_periods(payload: Any) -> ParsedPeriods:
    """Parse the mechanicalmonthly response.

    Duplicate periods (same start and end) are kept once, so a repeated item
    can never be counted twice.
    """
    if not isinstance(payload, list):
        raise WatercarePayloadError("Billing periods payload is not a list")
    seen: set[tuple[date, date]] = set()
    periods: list[BillingPeriod] = []
    skipped = 0
    duplicates = 0
    for item in payload:
        period = BillingPeriod.from_json(item)
        if period is None:
            skipped += 1
            continue
        key = (period.start, period.end)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        periods.append(period)
    periods.sort(key=lambda period: (period.end, period.start))
    return ParsedPeriods(tuple(periods), skipped, duplicates)


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

    @classmethod
    def from_json(cls, payload: Any) -> AccountSummary | None:
        """Build a summary from the first account in a v1/account response."""
        if not isinstance(payload, list) or not payload:
            return None
        record = payload[0]
        if not isinstance(record, dict):
            return None
        account_number = record.get("accountNumber")
        if not isinstance(account_number, str | int) or isinstance(
            account_number, bool
        ):
            return None
        if not str(account_number):
            return None
        meters = record.get("meters")
        meter_number: str | None = None
        if isinstance(meters, list) and meters and isinstance(meters[0], dict):
            meter_number = _optional_str(meters[0].get("id"))
        return cls(
            account_number=str(account_number),
            account_balance=_optional_number(record.get("accountBalance")),
            amount_due=_optional_number(record.get("amountDue")),
            overdue_amount=_optional_number(record.get("overdueAmount")),
            payment_due_date=(
                _optional_str(record.get("dueDate"))
                if record.get("hasDueDate")
                else None
            ),
            meter_type=_optional_str(record.get("meterType")),
            meter_number=meter_number,
        )


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


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
