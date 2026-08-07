"""Tests for WatercareCoordinator._async_update_data's exception mapping.

Real bug history (FIX 4): a transient v1/account failure on the first poll
used to surface as WatercareAuthError, which the coordinator mapped to
ConfigEntryAuthFailed -- wrongly telling the user to reauthenticate even
though their credentials were fine. WatercareConnectionError and
aiohttp.ClientError must instead map to UpdateFailed (retried on the normal
schedule, no reauth demanded); WatercareAuthError -- reserved for genuine
credential/sign-in rejection -- must still map to ConfigEntryAuthFailed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.watercare.api import (
    WatercareAuthError,
    WatercareConnectionError,
)
from tests.helpers import REAL_TARIFF, make_coordinator


def _coordinator_with_get_data_raising(hass, exc: BaseException):
    api = MagicMock()
    api.account = None
    api.account_number = None
    api.get_data = AsyncMock(side_effect=exc)
    return make_coordinator(hass, api=api, **REAL_TARIFF)


async def test_watercare_auth_error_maps_to_config_entry_auth_failed(hass) -> None:
    """Genuine credential/sign-in rejection still triggers reauth."""
    coordinator = _coordinator_with_get_data_raising(
        hass, WatercareAuthError("bad credentials")
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_watercare_connection_error_maps_to_update_failed(hass) -> None:
    """A missing account after successful sign-in is a connection problem,
    not a credentials problem -- must not send the user into reauth."""
    coordinator = _coordinator_with_get_data_raising(
        hass, WatercareConnectionError("no account number returned")
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_aiohttp_client_error_maps_to_update_failed(hass) -> None:
    coordinator = _coordinator_with_get_data_raising(
        hass, aiohttp.ClientError("connection reset")
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_none_response_still_raises_update_failed(hass) -> None:
    """Pre-existing behaviour: get_data() returning None (rather than
    raising) must still be handled as UpdateFailed."""
    api = MagicMock()
    api.account = None
    api.account_number = None
    api.get_data = AsyncMock(return_value=None)
    coordinator = make_coordinator(hass, api=api, **REAL_TARIFF)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
