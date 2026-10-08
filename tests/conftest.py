"""Shared fixtures.

The integration depends on the recorder, so tests that load it ask for
``ha``: an in-memory recorder first (it must exist before ``hass``), then
custom integrations enabled. Pure-function tests need neither.

Importing ``custom_components.watercare`` here, before any ``hass`` fixture
runs, makes ``custom_components`` resolve to this repository rather than the
test harness's own placeholder package.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from syrupy.assertion import SnapshotAssertion

from custom_components.watercare.api import WatercareApi
from custom_components.watercare.models import AccountSummary

from .common import ACCOUNT_PAYLOAD, default_periods

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True, scope="session")
def quiet_sqlalchemy() -> None:
    """Keep the harness's SQL echo out of test output."""
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Use Home Assistant's snapshot format whatever order plugins load in."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
async def ha(
    recorder_mock: Recorder,
    enable_custom_integrations: None,
    hass: HomeAssistant,
) -> HomeAssistant:
    """Home Assistant with a recorder and custom integrations enabled."""
    del recorder_mock, enable_custom_integrations
    return hass


@pytest.fixture
def mock_api() -> Generator[dict[str, AsyncMock]]:
    """Patch the Watercare client's network calls with synthetic data."""
    account = AccountSummary.from_json(ACCOUNT_PAYLOAD)
    assert account is not None

    async def _get_account(self: WatercareApi) -> AccountSummary:
        self._account = account
        return account

    with (
        patch.object(
            WatercareApi, "async_get_account", autospec=True, side_effect=_get_account
        ) as get_account,
        patch.object(
            WatercareApi,
            "async_get_billing_periods",
            autospec=True,
            return_value=default_periods(),
        ) as get_periods,
    ):
        yield {"account": get_account, "periods": get_periods}
