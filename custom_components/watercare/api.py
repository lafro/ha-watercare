"""Client for the Watercare customer-app API.

Sign-in uses Azure AD B2C with PKCE and the self-asserted (email and
password) flow, the only credential path Watercare's tenant offers. That flow
needs its own cookie jar, so it runs in a short-lived session from the
``sign_in_session`` factory (in Home Assistant, one built by Home Assistant's
own helper; see session.py). All other calls use the session Home Assistant
provides.

A refresh token is kept and handed to ``token_callback`` whenever it changes,
so Home Assistant can store it and later restarts resume with a token refresh
instead of a full sign-in.

Logging never includes credentials, tokens, account or meter identifiers,
URLs that contain them, or response bodies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Final
from urllib.parse import parse_qs, quote

import aiohttp

from .models import AccountSummary

_LOGGER = logging.getLogger(__name__)

_CLIENT_ID: Final = "799c26af-c35b-4010-bd04-b6a7ebdba811"
_REDIRECT_URI: Final = "msauth://nz.co.watercare/yRDm0vmCd9zdnwt1eCLGp8KfdLY%3D"
_API_BASE: Final = "https://customerapp.api.water.co.nz/"
_B2C_BASE: Final = "https://wslpwb2cprd.b2clogin.com/tfp/wslpwb2cprd.onmicrosoft.com"
_POLICY: Final = "B2C_1_sign_up_or_sign_in_mobile"
_SCOPE: Final = f"{_CLIENT_ID} openid offline_access profile"
_SETTINGS_PREFIX: Final = "var SETTINGS = "
_TOKEN_EXPIRY_MARGIN: Final = 60
_REDIRECT_STATUSES: Final = frozenset({200, 301, 302, 307, 308})
_HTTP_OK: Final = 200
_HTTP_UNAUTHORIZED: Final = 401
REQUEST_TIMEOUT: Final = aiohttp.ClientTimeout(total=60)


class WatercareError(Exception):
    """Base class for Watercare client errors."""


class WatercareAuthError(WatercareError):
    """Watercare rejected the credentials or the sign-in flow."""


class WatercareConnectionError(WatercareError):
    """A transient or service problem that is not a credentials problem.

    Home Assistant must not start reauthentication for these.
    """


def _code_verifier() -> str:
    return secrets.token_urlsafe(100)[:128]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


type SignInSessionFactory = Callable[
    [], AbstractAsyncContextManager[aiohttp.ClientSession]
]
"""Returns a context manager for one sign-in's session, released on exit."""


def sign_in_cookie_jar() -> aiohttp.CookieJar:
    """Return a cookie jar for one B2C sign-in.

    B2C's cookies hold characters that aiohttp would otherwise quote, which
    breaks the flow.
    """
    return aiohttp.CookieJar(quote_cookie=False)


def _default_sign_in_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        cookie_jar=sign_in_cookie_jar(), timeout=REQUEST_TIMEOUT
    )


def parse_settings(page: str) -> Mapping[str, Any] | None:
    """Read the ``var SETTINGS = {...};`` object B2C embeds in its login page.

    It is a Microsoft B2C platform bootstrap object, not Watercare markup.
    """
    for line in page.splitlines():
        stripped = line.strip()
        if stripped.startswith(_SETTINGS_PREFIX) and stripped.endswith(";"):
            try:
                value = json.loads(
                    stripped.removeprefix(_SETTINGS_PREFIX).removesuffix(";")
                )
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
    return None


class WatercareApi:
    """Watercare customer-app API client."""

    def __init__(
        self,
        email: str,
        password: str,
        session: aiohttp.ClientSession,
        *,
        refresh_token: str | None = None,
        token_callback: Callable[[str], None] | None = None,
        sign_in_session: SignInSessionFactory | None = None,
    ) -> None:
        """Initialise the client.

        ``sign_in_session`` provides the session for one B2C sign-in, with
        its own cookie jar (``sign_in_cookie_jar``). Home Assistant passes
        one built by its own helper; without it, a plain aiohttp session is
        used and closed afterwards.
        """
        self._sign_in_session = sign_in_session or _default_sign_in_session
        self._email = email
        self._password = password
        self._session = session
        self._refresh_token = refresh_token
        self._token_callback = token_callback
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._account: AccountSummary | None = None

    @property
    def account(self) -> AccountSummary | None:
        """Return the most recent account summary."""
        return self._account

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token."""
        return self._refresh_token

    def _access_token_valid(self) -> bool:
        return (
            self._access_token is not None
            and time.monotonic() < self._access_token_expires_at
        )

    def _store_tokens(self, payload: Mapping[str, Any]) -> None:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise WatercareAuthError("Watercare returned no access token")
        self._access_token = access_token
        expires_in = payload.get("expires_in")
        try:
            lifetime = int(expires_in) if expires_in is not None else 0
        except TypeError, ValueError:
            lifetime = 0
        self._access_token_expires_at = time.monotonic() + max(
            lifetime - _TOKEN_EXPIRY_MARGIN, 0
        )
        refresh_token = payload.get("refresh_token")
        if (
            isinstance(refresh_token, str)
            and refresh_token
            and refresh_token != self._refresh_token
        ):
            self._refresh_token = refresh_token
            if self._token_callback is not None:
                self._token_callback(refresh_token)

    async def async_sign_in(self) -> None:
        """Sign in with the email and password (B2C self-asserted flow)."""
        _LOGGER.debug("Signing in to Watercare")
        try:
            async with self._sign_in_session() as session:
                await self._async_sign_in(session)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            # ValueError: a response that should have been JSON was not.
            raise WatercareConnectionError(
                f"Could not complete the Watercare sign-in: {type(err).__name__}"
            ) from err

    async def _async_sign_in(self, session: aiohttp.ClientSession) -> None:
        verifier = _code_verifier()
        request_id = str(uuid.uuid4())
        authorize = {
            "response_type": "code",
            "code_challenge_method": "S256",
            "client_id": _CLIENT_ID,
            "client-request-id": request_id,
            "scope": _SCOPE,
            "prompt": "select_account",
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": _code_challenge(verifier),
        }
        async with session.get(
            f"{_B2C_BASE}/{_POLICY}/oAuth2/v2.0/authorize", params=authorize
        ) as response:
            page = await response.text()
        settings = parse_settings(page)
        if settings is None:
            # A maintenance page, a rate limit or a changed flow: not a
            # credentials problem.
            raise WatercareConnectionError(
                "The Watercare sign-in page had no sign-in settings"
            )
        transaction = str(settings.get("transId", ""))
        csrf = str(settings.get("csrf", ""))

        # The query string is built by hand, exactly as the proven 1.4.x flow
        # did, so the transaction id keeps its raw "=" characters.
        async with session.post(
            f"{_B2C_BASE}/{_POLICY}/SelfAsserted?tx={transaction}&p={_POLICY}",
            headers={"X-CSRF-TOKEN": csrf},
            data={
                "request_type": "RESPONSE",
                "email": self._email,
                "password": self._password,
            },
        ) as response:
            body = await response.text()
        # B2C reports bad credentials as JSON with a non-200 "status" while
        # the HTTP status stays 200.
        try:
            check = json.loads(body)
        except json.JSONDecodeError:
            check = {}
        if isinstance(check, dict) and str(check.get("status", "200")) != "200":
            raise WatercareAuthError("Watercare rejected the email or password")

        async with session.get(
            f"{_B2C_BASE}/{_POLICY}/api/CombinedSigninAndSignup/confirmed",
            params={
                "rememberMe": "false",
                "csrf_token": csrf,
                "tx": transaction,
                "p": _POLICY,
            },
            allow_redirects=False,
        ) as response:
            status = response.status
            location = response.headers.get("Location", "")
        if status not in _REDIRECT_STATUSES:
            raise WatercareConnectionError(
                f"Watercare sign-in confirmation failed with HTTP {status}"
            )
        if "?" not in location:
            raise WatercareConnectionError("Watercare sign-in returned no redirect")
        query = parse_qs(location.split("?", 1)[1])
        if "error" in query:
            raise WatercareAuthError("Watercare refused the sign-in")
        codes = query.get("code")
        if not codes:
            raise WatercareAuthError("Watercare returned no authorisation code")

        async with session.get(
            f"{_B2C_BASE}/{_POLICY}/oauth2/v2.0/token",
            params={
                "client_id": _CLIENT_ID,
                "client-request-id": request_id,
                "client_info": "1",
                "code": codes[0],
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "scope": _SCOPE,
            },
        ) as response:
            if response.status != _HTTP_OK:
                raise WatercareConnectionError(
                    f"Watercare token exchange failed with HTTP {response.status}"
                )
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise WatercareAuthError("Watercare token response was not an object")
        self._store_tokens(payload)
        _LOGGER.debug("Signed in to Watercare")

    async def async_refresh_access_token(self) -> bool:
        """Refresh the access token with the refresh token.

        Returns False when there is no refresh token or Watercare refuses it,
        so the caller can fall back to a full sign-in.
        """
        if not self._refresh_token:
            return False
        try:
            async with self._session.post(
                f"{_B2C_BASE}/{_POLICY}/oauth2/v2.0/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": _CLIENT_ID,
                    "refresh_token": self._refresh_token,
                },
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status != _HTTP_OK:
                    _LOGGER.debug(
                        "Watercare refused the refresh token (HTTP %s)",
                        response.status,
                    )
                    return False
                try:
                    payload = await response.json(content_type=None)
                except ValueError:
                    _LOGGER.debug("Watercare returned a malformed token response")
                    return False
        except (aiohttp.ClientError, TimeoutError) as err:
            raise WatercareConnectionError(
                f"Could not refresh the Watercare token: {type(err).__name__}"
            ) from err
        if not isinstance(payload, dict):
            return False
        try:
            self._store_tokens(payload)
        except WatercareAuthError:
            return False
        _LOGGER.debug("Refreshed the Watercare access token")
        return True

    async def async_ensure_token(self) -> None:
        """Make sure a usable access token is held."""
        if self._access_token_valid():
            return
        if await self.async_refresh_access_token():
            return
        await self.async_sign_in()

    async def _async_get(self, path: str) -> Any:
        """GET an API path as JSON, refreshing the token once on a 401."""
        for attempt in range(2):
            await self.async_ensure_token()
            try:
                async with self._session.get(
                    f"{_API_BASE}{path}",
                    headers={"authorization": f"Bearer {self._access_token}"},
                    timeout=REQUEST_TIMEOUT,
                ) as response:
                    if response.status == _HTTP_OK:
                        return await response.json(content_type=None)
                    status = response.status
            except (aiohttp.ClientError, TimeoutError) as err:
                raise WatercareConnectionError(
                    f"Watercare API request failed: {type(err).__name__}"
                ) from err
            except ValueError as err:
                raise WatercareConnectionError(
                    "Watercare API returned malformed JSON"
                ) from err
            if status == _HTTP_UNAUTHORIZED and attempt == 0:
                _LOGGER.debug("Watercare rejected the access token; renewing it")
                self._access_token = None
                continue
            if status == _HTTP_UNAUTHORIZED:
                raise WatercareAuthError(
                    "Watercare rejected a freshly issued access token"
                )
            raise WatercareConnectionError(
                f"Watercare API request failed with HTTP {status}"
            )
        raise WatercareConnectionError(
            "Watercare API request failed"
        )  # pragma: no cover

    async def async_get_account(self) -> AccountSummary:
        """Fetch the account record (balance, due date, meter)."""
        payload = await self._async_get("v1/account")
        account = AccountSummary.from_json(payload)
        if account is None:
            raise WatercareConnectionError("Watercare returned no account")
        self._account = account
        return account

    async def async_get_billing_periods(self) -> Any:
        """Fetch the completed billing periods (mechanical meters)."""
        account = self._account or await self.async_get_account()
        number = quote(account.account_number, safe="")
        return await self._async_get(f"v1/usage/{number}/mechanicalmonthly")
