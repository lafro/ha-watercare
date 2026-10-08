"""Tests for the Watercare API client (HTTP mocked, no network)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.watercare.api import (
    WatercareApi,
    WatercareAuthError,
    WatercareConnectionError,
    _default_sign_in_session,
    parse_settings,
)

from .common import ACCOUNT_NUMBER, ACCOUNT_PAYLOAD, EMAIL, PASSWORD, default_periods

AUTHORIZE = re.compile(r"^https://wslpwb2cprd\.b2clogin\.com/.*/oAuth2/v2\.0/authorize")
SELF_ASSERTED = re.compile(r"^https://wslpwb2cprd\.b2clogin\.com/.*/SelfAsserted")
CONFIRMED = re.compile(r"^https://wslpwb2cprd\.b2clogin\.com/.*/confirmed")
TOKEN = re.compile(r"^https://wslpwb2cprd\.b2clogin\.com/.*/oauth2/v2\.0/token")
ACCOUNT_URL = "https://customerapp.api.water.co.nz/v1/account"
USAGE_URL = (
    f"https://customerapp.api.water.co.nz/v1/usage/{ACCOUNT_NUMBER}/mechanicalmonthly"
)
TRANSACTION = "StateProperties=eyJUSUQiOiJ0ZXN0In0"
LOGIN_PAGE = (
    "<html><script>\n"
    f'var SETTINGS = {{"transId":"{TRANSACTION}","csrf":"c"}};\n'
    "</script></html>"
)
TOKENS = {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}

Responder = Callable[[str, Any, Any], Awaitable[AiohttpClientMockResponse]]


def _sequence(*responses: dict[str, Any]) -> Responder:
    """Return a side effect that answers with each response in turn."""
    queue = list(responses)

    async def _respond(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        del data
        return AiohttpClientMockResponse(method, url, **queue.pop(0))

    return _respond


def _sign_in_ok(
    mock: AiohttpClientMocker, tokens: dict[str, Any] | None = None
) -> None:
    mock.get(AUTHORIZE, text=LOGIN_PAGE)
    mock.post(SELF_ASSERTED, json={"status": "200"})
    mock.get(
        CONFIRMED,
        status=HTTPStatus.FOUND,
        headers={"Location": "msauth://nz.co.watercare/x?code=auth-code"},
    )
    mock.get(TOKEN, json=tokens or TOKENS)


def _api(
    aioclient_mock: AiohttpClientMocker, **kwargs: Any
) -> tuple[WatercareApi, list[str]]:
    loop = asyncio.get_running_loop()
    stored: list[str] = []
    api = WatercareApi(
        EMAIL,
        PASSWORD,
        aioclient_mock.create_session(loop),
        token_callback=stored.append,
        sign_in_session=lambda: aioclient_mock.create_session(loop),
        **kwargs,
    )
    return api, stored


def test_parse_settings() -> None:
    assert parse_settings(LOGIN_PAGE) == {"transId": TRANSACTION, "csrf": "c"}
    assert parse_settings("<html></html>") is None
    assert parse_settings("var SETTINGS = {not json};") is None
    assert parse_settings("var SETTINGS = [1];") is None


async def test_default_sign_in_session_has_its_own_cookie_jar() -> None:
    session = _default_sign_in_session()
    try:
        assert isinstance(session.cookie_jar, aiohttp.CookieJar)
    finally:
        await session.close()


async def test_sign_in_stores_tokens(aioclient_mock: AiohttpClientMocker) -> None:
    _sign_in_ok(aioclient_mock)
    api, stored = _api(aioclient_mock)

    await api.async_sign_in()

    assert api.refresh_token == "refresh-1"
    assert stored == ["refresh-1"]
    posted = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    # The transaction id keeps its raw "=" characters, as in 1.4.x, and the
    # credentials travel only in the form body.
    assert f"tx={TRANSACTION}" in str(posted[0][1])
    assert posted[0][2] == {
        "request_type": "RESPONSE",
        "email": EMAIL,
        "password": PASSWORD,
    }
    assert PASSWORD not in str(posted[0][1])


async def test_sign_in_rejected_credentials(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.get(AUTHORIZE, text=LOGIN_PAGE)
    aioclient_mock.post(SELF_ASSERTED, json={"status": "400", "message": "Invalid"})
    api, _ = _api(aioclient_mock)

    with pytest.raises(WatercareAuthError):
        await api.async_sign_in()


async def test_sign_in_without_settings_is_a_connection_problem(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.get(AUTHORIZE, text="<html>Down for maintenance</html>")
    api, _ = _api(aioclient_mock)

    with pytest.raises(WatercareConnectionError):
        await api.async_sign_in()


async def test_sign_in_network_error(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(AUTHORIZE, exc=aiohttp.ClientConnectionError("boom"))
    api, _ = _api(aioclient_mock)

    with pytest.raises(WatercareConnectionError):
        await api.async_sign_in()


@pytest.mark.parametrize(
    ("status", "location", "error"),
    [
        (HTTPStatus.INTERNAL_SERVER_ERROR, "", WatercareConnectionError),
        (HTTPStatus.FOUND, "msauth://nz.co.watercare/x", WatercareConnectionError),
        (HTTPStatus.FOUND, "msauth://x?error=access_denied", WatercareAuthError),
        (HTTPStatus.FOUND, "msauth://x?state=1", WatercareAuthError),
    ],
)
async def test_sign_in_confirmation_failures(
    aioclient_mock: AiohttpClientMocker,
    status: HTTPStatus,
    location: str,
    error: type[Exception],
) -> None:
    aioclient_mock.get(AUTHORIZE, text=LOGIN_PAGE)
    aioclient_mock.post(SELF_ASSERTED, text="not json")
    aioclient_mock.get(CONFIRMED, status=status, headers={"Location": location})
    api, _ = _api(aioclient_mock)

    with pytest.raises(error):
        await api.async_sign_in()


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (HTTPStatus.INTERNAL_SERVER_ERROR, {}, WatercareConnectionError),
        (HTTPStatus.OK, ["not", "an", "object"], WatercareAuthError),
        (HTTPStatus.OK, {"refresh_token": "r"}, WatercareAuthError),
    ],
)
async def test_sign_in_token_failures(
    aioclient_mock: AiohttpClientMocker,
    status: HTTPStatus,
    payload: Any,
    error: type[Exception],
) -> None:
    aioclient_mock.get(AUTHORIZE, text=LOGIN_PAGE)
    aioclient_mock.post(SELF_ASSERTED, json={"status": "200"})
    aioclient_mock.get(
        CONFIRMED,
        status=HTTPStatus.FOUND,
        headers={"Location": "msauth://x?code=auth-code"},
    )
    aioclient_mock.get(TOKEN, status=status, json=payload)
    api, _ = _api(aioclient_mock)

    with pytest.raises(error):
        await api.async_sign_in()


async def test_refresh_without_token_returns_false(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    api, _ = _api(aioclient_mock)
    assert not await api.async_refresh_access_token()
    assert aioclient_mock.call_count == 0


async def test_refresh_keeps_an_unchanged_token_quiet(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(TOKEN, json={"access_token": "a", "expires_in": "bad"})
    api, stored = _api(aioclient_mock, refresh_token="stored")

    assert await api.async_refresh_access_token()
    assert api.refresh_token == "stored"
    assert stored == []


async def test_refresh_rotates_token(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TOKEN, json={"access_token": "a", "refresh_token": "rotated"})
    api, stored = _api(aioclient_mock, refresh_token="stored")

    assert await api.async_refresh_access_token()
    assert stored == ["rotated"]


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"}),
        (HTTPStatus.OK, ["list"]),
        (HTTPStatus.OK, {"expires_in": 5}),
    ],
)
async def test_refresh_refused(
    aioclient_mock: AiohttpClientMocker, status: HTTPStatus, payload: Any
) -> None:
    aioclient_mock.post(TOKEN, status=status, json=payload)
    api, _ = _api(aioclient_mock, refresh_token="stored")

    assert not await api.async_refresh_access_token()


async def test_refresh_network_error(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TOKEN, exc=TimeoutError())
    api, _ = _api(aioclient_mock, refresh_token="stored")

    with pytest.raises(WatercareConnectionError):
        await api.async_refresh_access_token()


async def test_ensure_token_prefers_refresh_then_sign_in(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(TOKEN, status=HTTPStatus.BAD_REQUEST, json={"error": "x"})
    _sign_in_ok(aioclient_mock)
    api, _ = _api(aioclient_mock, refresh_token="expired")

    await api.async_ensure_token()
    assert api.refresh_token == "refresh-1"

    # A valid access token means no further requests.
    calls = aioclient_mock.call_count
    await api.async_ensure_token()
    assert aioclient_mock.call_count == calls


async def test_get_account_and_periods(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TOKEN, json=TOKENS)
    aioclient_mock.get(ACCOUNT_URL, json=ACCOUNT_PAYLOAD)
    aioclient_mock.get(USAGE_URL, json=default_periods())
    api, _ = _api(aioclient_mock, refresh_token="stored")

    periods = await api.async_get_billing_periods()

    assert api.account is not None
    assert api.account.account_number == ACCOUNT_NUMBER
    assert periods == default_periods()
    # The cached account is reused on the next fetch.
    await api.async_get_billing_periods()
    account_calls = [
        call for call in aioclient_mock.mock_calls if str(call[1]) == ACCOUNT_URL
    ]
    assert len(account_calls) == 1


async def test_get_account_without_accounts(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(TOKEN, json=TOKENS)
    aioclient_mock.get(ACCOUNT_URL, json=[])
    api, _ = _api(aioclient_mock, refresh_token="stored")

    with pytest.raises(WatercareConnectionError):
        await api.async_get_account()


async def test_unauthorised_request_renews_once(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(TOKEN, json=TOKENS)
    aioclient_mock.get(
        ACCOUNT_URL,
        side_effect=_sequence(
            {"status": HTTPStatus.UNAUTHORIZED}, {"json": ACCOUNT_PAYLOAD}
        ),
    )
    api, _ = _api(aioclient_mock, refresh_token="stored")

    account = await api.async_get_account()

    assert account.account_number == ACCOUNT_NUMBER
    token_calls = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert len(token_calls) == 2


async def test_unauthorised_twice_is_an_auth_error(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(TOKEN, json=TOKENS)
    aioclient_mock.get(ACCOUNT_URL, status=HTTPStatus.UNAUTHORIZED)
    api, _ = _api(aioclient_mock, refresh_token="stored")

    with pytest.raises(WatercareAuthError):
        await api.async_get_account()


@pytest.mark.parametrize(
    "response",
    [
        {"status": HTTPStatus.SERVICE_UNAVAILABLE},
        {"exc": aiohttp.ServerDisconnectedError()},
        {"text": "{not json"},
    ],
)
async def test_request_failures_are_connection_errors(
    aioclient_mock: AiohttpClientMocker, response: dict[str, Any]
) -> None:
    aioclient_mock.post(TOKEN, json=TOKENS)
    aioclient_mock.get(ACCOUNT_URL, **response)
    api, _ = _api(aioclient_mock, refresh_token="stored")

    with pytest.raises(WatercareConnectionError):
        await api.async_get_account()


async def test_rotated_token_without_a_callback(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(TOKEN, json={"access_token": "a", "refresh_token": "rotated"})
    api = WatercareApi(
        EMAIL,
        PASSWORD,
        aioclient_mock.create_session(asyncio.get_running_loop()),
        refresh_token="stored",
    )

    assert await api.async_refresh_access_token()
    assert api.refresh_token == "rotated"


async def test_malformed_token_responses(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TOKEN, text="<html>busy</html>")
    aioclient_mock.get(AUTHORIZE, text=LOGIN_PAGE)
    aioclient_mock.post(SELF_ASSERTED, json={"status": "200"})
    aioclient_mock.get(
        CONFIRMED,
        status=HTTPStatus.FOUND,
        headers={"Location": "msauth://x?code=auth-code"},
    )
    aioclient_mock.get(TOKEN, text="<html>busy</html>")
    api, _ = _api(aioclient_mock, refresh_token="stored")

    # Refresh falls back; the sign-in's token exchange is a connection error.
    assert not await api.async_refresh_access_token()
    with pytest.raises(WatercareConnectionError):
        await api.async_sign_in()
