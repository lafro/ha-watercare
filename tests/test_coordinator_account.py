"""Tests for coordinator._account_attributes.

The v1/account payload is the only source of balance/amount-due/meter-type
data -- the usage endpoints never carry it -- so this mapping is what feeds
the account_balance, amount_due, overdue_amount and payment_due_date
sensors.
"""

from __future__ import annotations

from custom_components.watercare.coordinator import _account_attributes


def test_maps_balance_due_overdue_and_meter_type() -> None:
    account = {
        "accountBalance": -45.67,
        "amountDue": 120.5,
        "overdueAmount": 0,
        "meterType": "Mechanical",
        "hasDueDate": True,
        "dueDate": "2026-08-20T23:59:59Z",
    }

    attrs = _account_attributes(account)

    assert attrs["account_balance"] == -45.67
    assert attrs["amount_due"] == 120.5
    assert attrs["overdue_amount"] == 0
    assert attrs["meter_type"] == "Mechanical"
    assert attrs["payment_due_date"] == "2026-08-20T23:59:59Z"


def test_payment_due_date_is_none_when_has_due_date_is_false() -> None:
    """A stale/irrelevant dueDate value must not leak out when unset."""
    account = {
        "accountBalance": 0,
        "amountDue": 0,
        "overdueAmount": 0,
        "meterType": "Mechanical",
        "hasDueDate": False,
        "dueDate": "2026-08-20T23:59:59Z",
    }

    attrs = _account_attributes(account)

    assert attrs["payment_due_date"] is None


def test_payment_due_date_is_set_when_has_due_date_is_true() -> None:
    account = {"hasDueDate": True, "dueDate": "2026-09-01T23:59:59Z"}

    attrs = _account_attributes(account)

    assert attrs["payment_due_date"] == "2026-09-01T23:59:59Z"


def test_none_account_returns_empty_dict() -> None:
    """No v1/account record yet (e.g. first-ever poll) must not raise."""
    assert _account_attributes(None) == {}


def test_empty_dict_account_returns_empty_dict() -> None:
    assert _account_attributes({}) == {}


def test_maps_meter_number_from_first_meter() -> None:
    """FIX 6: the meter_number sensor is fed from meters[0].id, not from the
    usage payload."""
    account = {"meters": [{"id": "M0012345"}, {"id": "M9999999"}]}

    attrs = _account_attributes(account)

    assert attrs["meter_number"] == "M0012345"


def test_meter_number_is_none_when_meters_missing() -> None:
    assert _account_attributes({"accountBalance": 0})["meter_number"] is None


def test_meter_number_is_none_when_meters_is_an_empty_list() -> None:
    assert _account_attributes({"meters": []})["meter_number"] is None
