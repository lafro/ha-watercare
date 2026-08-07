"""Tests for coordinator._period_days.

Watercare's billing-period API usually reports statistics.numberOfDays
directly; when that is missing (or non-positive), the period length must be
derived from the from/to dates instead, inclusive of both end points.
"""

from __future__ import annotations

from custom_components.watercare.coordinator import _period_days


def test_uses_supplied_number_of_days_when_present() -> None:
    period = {
        "statistics": {"numberOfDays": 31},
        # Deliberately inconsistent with the dates, to prove the supplied
        # value is used rather than the date subtraction.
        "billingPeriodFromDate": "2026-07-01T00:00:00.000Z",
        "billingPeriodToDate": "2026-07-02T00:00:00.000Z",
    }

    assert _period_days(period) == 31


def test_falls_back_to_date_subtraction_when_statistics_missing() -> None:
    period = {
        "billingPeriodFromDate": "2026-07-01T00:00:00.000Z",
        "billingPeriodToDate": "2026-07-31T00:00:00.000Z",
    }

    # 30 calendar days apart, but the period covers both the first and the
    # last day, so it is 31 days long.
    assert _period_days(period) == 31


def test_falls_back_when_number_of_days_is_missing_from_statistics() -> None:
    period = {
        "statistics": {},
        "billingPeriodFromDate": "2026-01-01T00:00:00.000Z",
        "billingPeriodToDate": "2026-01-15T00:00:00.000Z",
    }

    assert _period_days(period) == 15


def test_falls_back_when_number_of_days_is_not_positive() -> None:
    period = {
        "statistics": {"numberOfDays": 0},
        "billingPeriodFromDate": "2026-02-01T00:00:00.000Z",
        "billingPeriodToDate": "2026-02-05T00:00:00.000Z",
    }

    assert _period_days(period) == 5


def test_single_day_period_is_one_day_not_zero() -> None:
    period = {
        "billingPeriodFromDate": "2026-03-10T00:00:00.000Z",
        "billingPeriodToDate": "2026-03-10T00:00:00.000Z",
    }

    assert _period_days(period) == 1
