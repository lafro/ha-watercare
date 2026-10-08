"""Home Assistant sessions for the Watercare sign-in.

The B2C sign-in needs a cookie jar of its own, so it cannot use the shared
session. Each sign-in gets a short-lived session from Home Assistant's own
helper instead, which brings Home Assistant's connector, SSL context and user
agent. The session is detached when the sign-in ends: closing it would only
log a warning, because the connector belongs to Home Assistant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import REQUEST_TIMEOUT, SignInSessionFactory, sign_in_cookie_jar


def sign_in_session_factory(hass: HomeAssistant) -> SignInSessionFactory:
    """Return the sign-in session factory for ``WatercareApi``."""

    @asynccontextmanager
    async def _sign_in_session() -> AsyncIterator[aiohttp.ClientSession]:
        session = async_create_clientsession(
            hass,
            auto_cleanup=False,
            cookie_jar=sign_in_cookie_jar(),
            timeout=REQUEST_TIMEOUT,
        )
        try:
            yield session
        finally:
            session.detach()

    return _sign_in_session
