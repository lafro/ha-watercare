"""Watercare API."""

import aiohttp
import logging
from typing import Any
from collections.abc import Mapping
import json
import secrets
import hashlib
import base64
import time
import uuid
from urllib.parse import parse_qs

_LOGGER = logging.getLogger(__name__)


class WatercareAuthError(Exception):
    """Raised when Watercare rejects the credentials or sign-in flow."""


class WatercareConnectionError(Exception):
    """Raised for transient/connection problems that are not a credentials issue.

    E.g. sign-in succeeded but the account record could not be fetched. This
    must not trigger Home Assistant's reauth flow, since the user's
    credentials are not at fault.
    """


class WatercareApi:
    """Define the Watercare API."""

    def __init__(self, email, password, session: aiohttp.ClientSession | None = None):
        """Initialise the API.

        session is an optional shared aiohttp session used for plain API
        calls. The B2C sign-in dance always builds its own session because it
        depends on cookies that must not leak into a shared jar.
        """
        self._client_id = "799c26af-c35b-4010-bd04-b6a7ebdba811"
        self._redirect_uri = "msauth://nz.co.watercare/yRDm0vmCd9zdnwt1eCLGp8KfdLY%3D"
        self._url_base = "https://customerapp.api.water.co.nz/"
        self._url_token_base = (
            "https://wslpwb2cprd.b2clogin.com/tfp/wslpwb2cprd.onmicrosoft.com"
        )
        self._p = "B2C_1_sign_up_or_sign_in_mobile"

        self._email = email
        self._password = password
        self._session = session

        self._accountNumber = None
        self._account: dict | None = None
        self._token = None
        self._refresh_token = None
        self._refresh_token_expires_in = 0
        self._access_token_expires_in = 0
        self._access_token_expires_at = 0.0

    @property
    def account_number(self):
        """Return the Watercare account number, once known."""
        return self._accountNumber

    @property
    def account(self) -> dict | None:
        """Return the most recent v1/account record for this account."""
        return self._account

    def _api_session(self) -> tuple[aiohttp.ClientSession, bool]:
        """Return (session, owned) for plain API calls."""
        if self._session is not None and not self._session.closed:
            return self._session, False
        return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(quote_cookie=False)), True

    def _access_token_is_expired(self) -> bool:
        """Return whether the access token is missing or near expiry."""
        return not self._token or time.monotonic() >= self._access_token_expires_at

    def get_setting_json(self, page: str) -> Mapping[str, Any] | None:
        """Get the settings from json result."""
        for line in page.splitlines():
            if line.startswith("var SETTINGS = ") and line.endswith(";"):
                json_string = line.removeprefix("var SETTINGS = ").removesuffix(";")
                return json.loads(json_string)
        return None

    def generate_code_verifier(self):
        """Generate code verifier for OAuth steps."""
        code_verifier = secrets.token_urlsafe(100)
        return code_verifier[:128]

    def generate_code_challenge(self, code_verifier):
        """Generate code challenge for OAuth steps."""
        code_challenge = hashlib.sha256(code_verifier.encode()).digest()
        return base64.urlsafe_b64encode(code_challenge).rstrip(b"=").decode()

    async def get_refresh_token(self):
        """Get the refresh token."""
        _LOGGER.debug("API get_refresh_token")
        jar = aiohttp.CookieJar(quote_cookie=False)
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            url = f"{self._url_token_base}/{self._p}/oAuth2/v2.0/authorize"

            code_verifier = self.generate_code_verifier()
            code_challenge = self.generate_code_challenge(code_verifier)
            client_request_id = str(uuid.uuid4())
            scope = f"{self._client_id} openid offline_access profile"

            params = {
                "response_type": "code",
                "code_challenge_method": "S256",
                "client_id": self._client_id,
                "client-request-id": client_request_id,
                "scope": scope,
                "prompt": "select_account",
                "redirect_uri": self._redirect_uri,
                "code_challenge": code_challenge,
            }

            async with session.get(url, params=params) as response:
                response_text = await response.text()

            settings_json = self.get_setting_json(response_text)
            _LOGGER.debug(f"settings_json: {settings_json}")

            if settings_json is None:
                # Watercare served something other than the B2C login page
                # (maintenance page, rate limit, flow change).
                raise WatercareAuthError(
                    "Could not find sign-in settings on the Watercare login page"
                )

            trans_id = settings_json.get("transId")
            csrf = settings_json.get("csrf")

            url = f"{self._url_token_base}/{self._p}/SelfAsserted?tx={trans_id}&p={self._p}"
            payload = {
                "request_type": "RESPONSE",
                "email": self._email,
                "password": self._password,
            }
            headers = {"X-CSRF-TOKEN": csrf}

            async with session.post(url, headers=headers, data=payload) as response:
                credential_check_text = await response.text()

            # B2C reports credential problems here as JSON with a non-200
            # "status" field while the HTTP status stays 200.
            try:
                credential_check = json.loads(credential_check_text)
            except json.JSONDecodeError:
                credential_check = {}
            if str(credential_check.get("status", "200")) != "200":
                message = credential_check.get("message") or "Sign-in was rejected"
                _LOGGER.debug("Watercare sign-in rejected: %s", message)
                raise WatercareAuthError(message)

            url = f"{self._url_token_base}/{self._p}/api/CombinedSigninAndSignup/confirmed"
            params = {
                "rememberMe": "false",
                "csrf_token": csrf,
                "tx": trans_id,
                "p": self._p,
            }

            headers = {}
            async with session.get(
                url, headers=headers, params=params, allow_redirects=False
            ) as response:
                if response.status not in [200, 301, 302, 307, 308]:
                    response_text = await response.text()
                    _LOGGER.error(
                        "Failed to confirm sign in. Status: %s, Response: %s",
                        response.status,
                        response_text,
                    )
                    raise ValueError(
                        f"Sign-in confirmation failed with status {response.status}"
                    )

                location = response.headers.get("Location", "")
                if not location:
                    _LOGGER.error("No Location header in response")
                    raise ValueError("No redirect location in sign-in response")

                query_params = parse_qs(location.split("?", 1)[1])
                if "error" in query_params:
                    description = (
                        query_params.get("error_description") or ["unknown error"]
                    )[0]
                    _LOGGER.error(
                        "Error in response: %s (%s)",
                        query_params["error"][0],
                        description,
                    )
                    raise WatercareAuthError(f"Authentication error: {description}")

            if "code" not in query_params:
                raise WatercareAuthError("No authorization code in sign-in response")
            code = query_params["code"][0]

            url = f"{self._url_token_base}/{self._p}/oauth2/v2.0/token"
            params = {
                "client_id": self._client_id,
                "client-request-id": client_request_id,
                "client_info": 1,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "scope": scope,
            }

            headers = {}
            async with session.get(url, headers=headers, params=params) as response:
                response_data = await response.json()
                self._refresh_token = response_data.get("refresh_token")
                self._token = response_data.get("access_token")
                self._refresh_token_expires_in = response_data.get(
                    "refresh_token_expires_in"
                )
                self._access_token_expires_in = response_data.get("expires_in")
                expires_in = int(self._access_token_expires_in or 0)
                self._access_token_expires_at = time.monotonic() + max(
                    expires_in - 60, 0
                )

            _LOGGER.debug("Refresh token retrieved successfully.")
            await self.get_accounts()

    async def get_api_token(self):
        """Refresh the Watercare access token."""
        if not self._refresh_token:
            return False

        token_data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
        }

        session, owned = self._api_session()
        try:
            url = f"{self._url_token_base}/{self._p}/oauth2/v2.0/token"
            async with session.post(url, data=token_data) as response:
                if response.status == 200:
                    token_result = await response.json()
                    self._token = token_result.get("access_token")
                    self._refresh_token = (
                        token_result.get("refresh_token") or self._refresh_token
                    )
                    self._access_token_expires_in = token_result.get("expires_in", 0)
                    expires_in = int(self._access_token_expires_in or 0)
                    self._access_token_expires_at = time.monotonic() + max(
                        expires_in - 60, 0
                    )
                    _LOGGER.debug("Watercare access token refreshed successfully")
                    return bool(self._token)
                else:
                    _LOGGER.warning(
                        "Failed to refresh Watercare access token: %s",
                        response.status,
                    )
                    return False
        finally:
            if owned:
                await session.close()

    async def get_accounts(self):
        """Get the first account that we see."""
        headers = {"authorization": "Bearer " + (self._token or "")}
        session, owned = self._api_session()
        try:
            async with session.get(
                self._url_base + "v1/account", headers=headers
            ) as result:
                if result.status == 200:
                    data = await result.json()
                    _LOGGER.debug(f"Accounts: {data}")
                    if data and isinstance(data, list) and len(data) > 0:
                        # Keep the whole record: it carries the meter type, meter
                        # serial, balance, amount due and due date, none of which
                        # appear on the usage endpoints.
                        self._account = data[0]
                        self._accountNumber = data[0].get("accountNumber")
                        if self._accountNumber:
                            _LOGGER.debug(f"AccountNumber: {self._accountNumber}")
                        else:
                            _LOGGER.error("Account number not found in the response")
                    else:
                        _LOGGER.error("No accounts found in the response")
                else:
                    _LOGGER.error(
                        "Failed to fetch customer accounts %s", await result.text()
                    )
        finally:
            if owned:
                await session.close()

    async def get_data(
        self, endpoint: str, start_date: str = None, end_date: str = None
    ):
        """Get data from the API."""
        if endpoint not in [
            "halfhourly",
            "dailywithstats",
            "monthly",
            "mechanicalmonthly",
        ]:
            raise ValueError("Invalid endpoint specified")

        # Authenticate fully on first use. On later polls, proactively refresh
        # the short-lived access token while retaining the account number.
        # Either path may call get_refresh_token(), which performs its own
        # account fetch -- so `just_authenticated` tracks that, and the
        # unconditional get_accounts() below is skipped in that case. This
        # keeps every poll to exactly one v1/account request instead of two.
        # WatercareAuthError from the sign-in dance propagates to the caller,
        # which lets Home Assistant start a reauth flow.
        just_authenticated = False
        if not self._accountNumber:
            _LOGGER.debug("No account number found, starting authentication process")
            await self.get_refresh_token()
            just_authenticated = True
            if not self._accountNumber:
                # Sign-in itself succeeded (no WatercareAuthError was raised),
                # but no account came back -- a data/connection problem, not
                # bad credentials, so this must not trigger reauth.
                raise WatercareConnectionError(
                    "Authenticated but no account number returned"
                )
        elif self._access_token_is_expired():
            if not await self.get_api_token():
                _LOGGER.debug(
                    "Refresh token unavailable or expired; authenticating again"
                )
                await self.get_refresh_token()
                just_authenticated = True
                if not self._token:
                    # Re-login itself failed to yield a token: an auth problem.
                    raise WatercareAuthError("Watercare reauthentication failed")
                if not self._accountNumber:
                    # Logged in fine but the account fetch failed: a connection
                    # problem, not credentials -- must not trigger reauth.
                    raise WatercareConnectionError(
                        "Authenticated but no account number returned"
                    )

        if not just_authenticated:
            # Refresh the account record each poll so balance, amount due and
            # due date stay current; it is a small request and only runs
            # twice a day. (When we just authenticated above,
            # get_refresh_token() already did this as part of signing in.)
            await self.get_accounts()

        url = f"{self._url_base}v1/usage/{self._accountNumber}/{endpoint}"
        if start_date and end_date:
            url += f"?from={start_date}&to={end_date}"

        _LOGGER.debug(f"Calling API URL: {url}")

        # Retry once after a 401. This also covers early token invalidation by
        # Watercare rather than only relying on the advertised expiry time.
        session, owned = self._api_session()
        try:
            for attempt in range(2):
                headers = {"authorization": "Bearer " + (self._token or "")}
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        data = await response.text()
                        _LOGGER.debug("API Response status: %s", response.status)
                        _LOGGER.debug(
                            "API Response data length: %s", len(data) if data else 0
                        )
                        return data

                    if response.status == 401 and attempt == 0:
                        _LOGGER.warning(
                            "Watercare access token was rejected; refreshing and retrying"
                        )
                        if await self.get_api_token():
                            continue

                        await self.get_refresh_token()
                        if self._token:
                            continue

                    if response.status == 401:
                        raise WatercareAuthError(
                            "Watercare rejected the access token after a refresh"
                        )
                    _LOGGER.error("Could not fetch consumption: %s", response.status)
                    return None

            return None
        finally:
            if owned:
                await session.close()
