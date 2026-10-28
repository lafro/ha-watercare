"""Contracts for the synthetic Watercare the journeys run the real client against."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

import pytest

from custom_components.watercare import api as client
from custom_components.watercare.api import (
    WatercareApi,
    WatercareAuthError,
    WatercareConnectionError,
)
from custom_components.watercare.models import AccountSummary, parse_billing_periods

from .watercare_http import (
    ACCOUNT_NUMBER,
    EMAIL,
    FIRST_READING,
    ISSUE_DAYS,
    METER_NUMBER,
    PASSWORD,
    SECOND_ACCOUNT_NUMBER,
    SECOND_METER_NUMBER,
    FakeWatercare,
    Response,
    bills,
    challenge_of,
    operation,
    read_token,
    reading_day,
)

_TODAY = date(2026, 10, 28)
_B2C = f"{client._B2C_BASE}/{client._POLICY}"
_API = client._API_BASE


def _api(fake: FakeWatercare, **kwargs: Any) -> tuple[WatercareApi, list[str]]:
    """Return the integration's real client, talking to the fake."""

    @contextlib.asynccontextmanager
    async def sign_in_session() -> AsyncIterator[Any]:
        yield fake

    stored: list[str] = []
    api = WatercareApi(
        kwargs.pop("email", EMAIL),
        kwargs.pop("password", PASSWORD),
        fake,  # type: ignore[arg-type]
        token_callback=stored.append,
        sign_in_session=sign_in_session,
        **kwargs,
    )
    return api, stored


def _operations(fake: FakeWatercare) -> list[str]:
    return [str(entry["operation"]) for entry in fake.log]


# --------------------------------------------------------------------- bills


def test_the_bills_are_a_function_of_the_date() -> None:
    issued = bills(_TODAY)
    assert issued == bills(_TODAY)
    assert issued[0].start == FIRST_READING + timedelta(days=1)
    # Contiguous: each bill starts the day after the one before it ended.
    for earlier, later in pairwise(issued):
        assert later.start == earlier.end + timedelta(days=1)
        assert later.litres != earlier.litres
    assert all(bill.litres % 1000 == 0 for bill in issued)
    assert all(6000 <= bill.litres <= 14000 for bill in issued)
    assert {bill.reading for bill in issued} == {"A", "E"}
    assert {bill.days for bill in issued} >= {28, 31}
    # One bill spans 1 July, so it is priced at the earlier year's prices.
    assert any(bill.start < date(bill.end.year, 7, 1) <= bill.end for bill in issued)
    # A bill is issued ISSUE_DAYS after its reading, and not before.
    newest = issued[-1]
    assert bills(newest.end + timedelta(days=ISSUE_DAYS)) == issued
    assert bills(newest.end + timedelta(days=ISSUE_DAYS - 1)) == issued[:-1]
    assert reading_day(2024, 6) == FIRST_READING


def test_a_bill_is_a_mechanicalmonthly_item_the_integration_reads() -> None:
    issued = bills(_TODAY)
    parsed = parse_billing_periods([bill.payload() for bill in reversed(issued)])
    assert (parsed.skipped, parsed.duplicates) == (0, 0)
    assert [(p.start, p.end, p.usage_litres) for p in parsed.periods] == [
        (bill.start, bill.end, bill.litres) for bill in issued
    ]
    assert [p.number_of_days for p in parsed.periods] == [b.days for b in issued]


# ------------------------------------------------------------------- sign-in


async def test_the_client_signs_in_and_reads_the_account_and_bills() -> None:
    fake = FakeWatercare(as_of=_TODAY)
    api, stored = _api(fake)
    await api.async_sign_in()
    assert len(stored) == 1
    token = read_token(stored[0], "refresh")
    assert token is not None
    assert (token.account, token.generation) == (ACCOUNT_NUMBER, 1)
    account = await api.async_get_account()
    assert (account.account_number, account.meter_number) == (
        ACCOUNT_NUMBER,
        METER_NUMBER,
    )
    periods = parse_billing_periods(await api.async_get_billing_periods())
    assert len(periods.periods) == len(bills(_TODAY))
    assert _operations(fake) == [
        "authorize",
        "self_asserted",
        "confirmed",
        "token",
        "account",
        "billing_periods",
    ]
    assert fake.log[1]["accepted"] is True
    assert {entry["status"] for entry in fake.log} == {200, 302}
    # Nothing a credential or token goes into the log.
    assert PASSWORD not in str(fake.log)
    assert stored[0] not in str(fake.log)


async def test_a_wrong_password_or_unknown_email_is_rejected() -> None:
    for email, password in ((EMAIL, "not-the-password"), ("x@example.com", PASSWORD)):
        fake = FakeWatercare()
        api, stored = _api(fake, email=email, password=password)
        with pytest.raises(WatercareAuthError):
            await api.async_sign_in()
        assert stored == []
        assert _operations(fake) == ["authorize", "self_asserted"]
        assert fake.log[1] == {
            "operation": "self_asserted",
            "status": 200,
            "accepted": False,
        }


async def test_watercare_down_answers_every_api_call_with_its_status() -> None:
    fake = FakeWatercare(api_status=503)
    api, _stored = _api(fake)
    with pytest.raises(WatercareConnectionError, match="HTTP 503"):
        await api.async_get_account()
    assert fake.log[-1] == {"operation": "account", "status": 503}
    fake.api_status = None
    assert (await api.async_get_account()).account_number == ACCOUNT_NUMBER


async def test_a_restarted_fake_takes_the_stored_refresh_token() -> None:
    first = FakeWatercare()
    signed_in, stored = _api(first)
    await signed_in.async_sign_in()
    restarted = FakeWatercare(as_of=_TODAY)
    api, rotated = _api(restarted, refresh_token=stored[-1])
    await api.async_get_billing_periods()
    assert _operations(restarted) == ["refresh", "account", "billing_periods"]
    assert len(rotated) == 1
    assert rotated[0] != stored[-1]


async def test_a_new_generation_ends_every_earlier_sign_in() -> None:
    fake = FakeWatercare()
    api, stored = _api(fake)
    await api.async_get_account()
    fake.generation = 2
    fake.password = "a-new-synthetic-password"
    fake.log.clear()
    with pytest.raises(WatercareAuthError):
        await api.async_get_account()
    assert [(e["operation"], e["status"]) for e in fake.log] == [
        ("account", 401),
        ("refresh", 400),
        ("authorize", 200),
        ("self_asserted", 200),
    ]
    assert fake.log[-1]["accepted"] is False
    renewed, _stored = _api(fake, password=fake.password, refresh_token=stored[-1])
    assert (await renewed.async_get_account()).account_number == ACCOUNT_NUMBER


async def test_each_login_reaches_its_own_account() -> None:
    fake = FakeWatercare(
        logins={EMAIL: ACCOUNT_NUMBER, "other@example.com": SECOND_ACCOUNT_NUMBER}
    )
    api, _stored = _api(fake, email="other@example.com")
    account = await api.async_get_account()
    assert (account.account_number, account.meter_number) == (
        SECOND_ACCOUNT_NUMBER,
        SECOND_METER_NUMBER,
    )
    assert fake.log[-1]["account"] == SECOND_ACCOUNT_NUMBER
    await api.async_get_billing_periods()
    # A token holds one account: another's bills are a request never to send.
    access = api._access_token
    with pytest.raises(AssertionError, match="does not hold"):
        fake.get(
            f"{_API}v1/usage/{ACCOUNT_NUMBER}/mechanicalmonthly",
            headers={"authorization": f"Bearer {access}"},
        )


async def test_withheld_bills_are_not_issued_yet() -> None:
    fake = FakeWatercare(as_of=_TODAY, withheld=2)
    assert fake.bills() == bills(_TODAY)[:-2]
    api, _stored = _api(fake)
    periods = parse_billing_periods(await api.async_get_billing_periods())
    assert periods.periods[-1].end == bills(_TODAY)[-3].end
    fake.withheld = 0
    assert fake.bills() == bills(_TODAY)


# ------------------------------------------------- requests never to be sent


def _authorize(fake: FakeWatercare, **params: str) -> str:
    query = {
        "response_type": "code",
        "code_challenge_method": "S256",
        "client_id": client._CLIENT_ID,
        "scope": client._SCOPE,
        "code_challenge": challenge_of("verifier"),
        **params,
    }
    page = fake.get(f"{_B2C}/oAuth2/v2.0/authorize", params=query)
    settings = client.parse_settings(page._body.decode())
    assert settings is not None
    return str(settings["transId"])


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"client_id": "another"}, "another client"),
        ({"code_challenge_method": "plain"}, "no PKCE"),
        ({"scope": "openid profile"}, "offline_access"),
        ({"code_challenge": ""}, "no code challenge"),
    ],
)
def test_the_login_page_wants_this_client_with_pkce(
    params: dict[str, str], match: str
) -> None:
    with pytest.raises(AssertionError, match=match):
        _authorize(FakeWatercare(), **params)


def test_the_sign_in_steps_are_checked() -> None:
    fake = FakeWatercare()
    transaction = _authorize(fake)
    self_asserted = f"{_B2C}/SelfAsserted?tx={transaction}&p={client._POLICY}"
    form = {"request_type": "RESPONSE", "email": EMAIL, "password": PASSWORD}
    with pytest.raises(AssertionError, match="CSRF"):
        fake.post(self_asserted, headers={"X-CSRF-TOKEN": "x"}, data=form)
    with pytest.raises(AssertionError, match="never gave out"):
        fake.post(f"{_B2C}/SelfAsserted?tx=other&p={client._POLICY}", data=form)
    confirmed = f"{_B2C}/api/CombinedSigninAndSignup/confirmed"
    query = {"tx": transaction, "p": client._POLICY, "csrf_token": "synthetic-csrf-1"}
    with pytest.raises(AssertionError, match="before the email and password"):
        fake.get(confirmed, params=query, allow_redirects=False)
    fake.post(self_asserted, headers={"X-CSRF-TOKEN": "synthetic-csrf-1"}, data=form)
    with pytest.raises(AssertionError, match="must not be followed"):
        fake.get(confirmed, params=query, allow_redirects=True)
    redirect = fake.get(confirmed, params=query, allow_redirects=False)
    assert redirect.status == 302
    code = redirect.headers["location"].split("code=", 1)[1]
    token = f"{_B2C}/oauth2/v2.0/token"
    exchange = {
        "grant_type": "authorization_code",
        "client_id": client._CLIENT_ID,
        "code": code,
    }
    with pytest.raises(AssertionError, match="does not match the challenge"):
        fake.get(token, params={**exchange, "code_verifier": "another"})
    with pytest.raises(AssertionError, match="never issued, or used twice"):
        fake.get(token, params={**exchange, "code_verifier": "verifier"})


def test_any_other_request_is_never_to_be_sent() -> None:
    fake = FakeWatercare()
    for method, url in (
        ("GET", f"{_API}v2/account"),
        ("POST", f"{_API}v1/account"),
        ("GET", "https://example.invalid/v1/account"),
        ("GET", f"{client._B2C_BASE}/another_policy/oAuth2/v2.0/authorize"),
    ):
        assert operation(method, url) is None
        with pytest.raises(AssertionError, match="unexpected request"):
            fake.request(method, url)
    with pytest.raises(AssertionError, match="neither code nor refresh"):
        fake.post(f"{_B2C}/oauth2/v2.0/token", data={"grant_type": "password"})
    assert fake.log == []


def test_tokens_that_are_not_the_fakes_are_refused() -> None:
    for value in ("", "synthetic-access.", "synthetic-access.!!", "Bearer x"):
        assert read_token(value, "access") is None
    fake = FakeWatercare()
    answer = fake.get(f"{_API}v1/account", headers={"authorization": "Bearer x"})
    assert answer.status == 401
    refused = fake.post(
        f"{_B2C}/oauth2/v2.0/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client._CLIENT_ID,
            "refresh_token": "not-a-token",
        },
    )
    assert refused.status == 400


async def test_responses_read_like_aiohttps() -> None:
    response = Response(b'{"a": 1}', headers={"Content-Type": "application/json"})
    async with response as answered:
        assert answered.headers["content-type"] == "application/json"
        assert await answered.json(content_type=None) == {"a": 1}
        assert await answered.text() == '{"a": 1}'
        assert await answered.read() == b'{"a": 1}'
    response.release()
    with pytest.raises(ValueError, match="Expecting value"):
        await Response(b"<html>").json()


def test_the_shared_payloads_are_the_tests_synthetic_account() -> None:
    from .common import ACCOUNT_PAYLOAD  # noqa: PLC0415

    account = AccountSummary.from_json(ACCOUNT_PAYLOAD)
    assert account is not None
    assert (account.account_number, account.meter_number) == (
        ACCOUNT_NUMBER,
        METER_NUMBER,
    )
