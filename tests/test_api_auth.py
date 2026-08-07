"""Tests for WatercareApi.get_data's authenticate-vs-connection error split.

The token-expiry reauth branch must classify failures the same way the
first-use branch does: a full re-login that yields no *token* is an auth
problem (-> WatercareAuthError -> reauth), but a re-login that succeeds yet
returns no *account* is a transient connection/data problem
(-> WatercareConnectionError -> UpdateFailed), NOT a credentials fault.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.watercare.api import (
    WatercareApi,
    WatercareAuthError,
    WatercareConnectionError,
)


def _api_with_expired_token() -> WatercareApi:
    """An API that already has an account but a stale access token.

    This forces get_data down the `elif self._access_token_is_expired()`
    branch (the reauth-on-expiry path), not the first-use path.
    """
    api = WatercareApi("e@example.com", "pw")
    api._accountNumber = "5090805-02"
    api._token = "stale"
    api._access_token_expires_at = 0.0  # already expired
    # Silent refresh fails, forcing a full get_refresh_token() re-login.
    api.get_api_token = AsyncMock(return_value=False)
    return api


async def test_reauth_branch_missing_account_raises_connection_error() -> None:
    """Re-login succeeds (token set) but account fetch fails -> connection error."""
    api = _api_with_expired_token()

    async def fake_relogin() -> None:
        api._token = "fresh"
        api._accountNumber = None  # account fetch failed transiently

    api.get_refresh_token = AsyncMock(side_effect=fake_relogin)

    with pytest.raises(WatercareConnectionError):
        await api.get_data(endpoint="mechanicalmonthly")


async def test_reauth_branch_missing_token_raises_auth_error() -> None:
    """Re-login fails to produce a token -> genuine auth failure."""
    api = _api_with_expired_token()

    async def fake_relogin() -> None:
        api._token = None
        api._accountNumber = None

    api.get_refresh_token = AsyncMock(side_effect=fake_relogin)

    with pytest.raises(WatercareAuthError):
        await api.get_data(endpoint="mechanicalmonthly")
