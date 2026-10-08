"""Tests for the config, reauth, reconfigure and options flows."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.watercare.api import (
    WatercareApi,
    WatercareAuthError,
    WatercareConnectionError,
)
from custom_components.watercare.config_flow import (
    async_validate_login,
    store_year_prices,
)
from custom_components.watercare.const import DOMAIN
from custom_components.watercare.models import AccountSummary

from .common import ACCOUNT_NUMBER, ACCOUNT_PAYLOAD, EMAIL, PASSWORD, make_entry

ACCOUNT = AccountSummary.from_json(ACCOUNT_PAYLOAD)
OTHER_ACCOUNT = AccountSummary.from_json([{"accountNumber": "2000002-02"}])


@pytest.fixture
def mock_login() -> Generator[AsyncMock]:
    with patch(
        "custom_components.watercare.config_flow.async_validate_login",
        return_value=(ACCOUNT, "new-refresh-token"),
    ) as login:
        yield login


@pytest.fixture
def mock_setup() -> Generator[AsyncMock]:
    with patch(
        "custom_components.watercare.async_setup_entry", return_value=True
    ) as setup:
        yield setup


async def test_user_flow_creates_entry(
    ha: HomeAssistant, mock_login: AsyncMock, mock_setup: AsyncMock
) -> None:
    result = await ha.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Watercare"
    assert result["data"] == {
        CONF_USERNAME: EMAIL,
        CONF_PASSWORD: PASSWORD,
        "refresh_token": "new-refresh-token",
    }
    assert result["options"] == {"wastewater_ratio": 0.785}
    assert result["result"].unique_id == ACCOUNT_NUMBER
    assert result["result"].minor_version == 2
    mock_setup.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (WatercareAuthError("no"), "invalid_auth"),
        (WatercareConnectionError("down"), "cannot_connect"),
        (RuntimeError("bug"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    ha: HomeAssistant,
    mock_login: AsyncMock,
    mock_setup: AsyncMock,
    error: Exception,
    reason: str,
) -> None:
    mock_login.side_effect = error
    result = await ha.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    mock_login.side_effect = None
    mock_login.return_value = (ACCOUNT, None)
    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "refresh_token" not in result["data"]


async def test_user_flow_rejects_a_duplicate_account(
    ha: HomeAssistant, mock_login: AsyncMock, mock_setup: AsyncMock
) -> None:
    make_entry().add_to_hass(ha)
    result = await ha.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_the_password(
    ha: HomeAssistant, mock_login: AsyncMock, mock_setup: AsyncMock
) -> None:
    entry = make_entry()
    entry.add_to_hass(ha)

    result = await entry.start_reauth_flow(ha)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == EMAIL

    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "changed-password"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "changed-password"
    assert entry.data["refresh_token"] == "new-refresh-token"
    mock_login.assert_awaited_with(ha, EMAIL, "changed-password")


async def test_reauth_error_and_wrong_account(
    ha: HomeAssistant, mock_login: AsyncMock, mock_setup: AsyncMock
) -> None:
    entry = make_entry()
    entry.add_to_hass(ha)
    result = await entry.start_reauth_flow(ha)

    mock_login.side_effect = WatercareAuthError("no")
    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_login.side_effect = None
    mock_login.return_value = (OTHER_ACCOUNT, None)
    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "other"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_PASSWORD] == PASSWORD


async def test_reauth_of_an_entry_without_unique_id(
    ha: HomeAssistant, mock_login: AsyncMock, mock_setup: AsyncMock
) -> None:
    entry = make_entry(unique_id=None)
    entry.add_to_hass(ha)
    result = await entry.start_reauth_flow(ha)

    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "changed-password"}
    )

    assert result["reason"] == "reauth_successful"


async def test_reconfigure_changes_the_login(
    ha: HomeAssistant, mock_login: AsyncMock, mock_setup: AsyncMock
) -> None:
    entry = make_entry()
    entry.add_to_hass(ha)

    result = await entry.start_reconfigure_flow(ha)
    assert result["step_id"] == "reconfigure"

    result = await ha.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "new@example.com", CONF_PASSWORD: "new-password"},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_USERNAME] == "new@example.com"
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert entry.data["statistics_version"] == 2


async def test_reconfigure_errors_and_wrong_account(
    ha: HomeAssistant, mock_login: AsyncMock, mock_setup: AsyncMock
) -> None:
    entry = make_entry()
    entry.add_to_hass(ha)
    result = await entry.start_reconfigure_flow(ha)

    mock_login.side_effect = WatercareConnectionError("down")
    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    mock_login.side_effect = None
    mock_login.return_value = (OTHER_ACCOUNT, None)
    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["reason"] == "wrong_account"


async def test_options_flow_prefills_and_stores_only_differences(
    ha: HomeAssistant, mock_setup: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-10-08T00:00:00+13:00")
    entry = make_entry()
    entry.add_to_hass(ha)

    result = await ha.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"] == {"financial_year": "2026/27"}
    suggested = {
        str(key): key.description["suggested_value"]  # type: ignore[index]
        for key in result["data_schema"].schema
    }
    assert suggested == {
        "wastewater_ratio": 0.785,
        "water_rate": 2.46,
        "wastewater_rate": 4.28,
        "fixed_charge": 355.9,
    }

    # Published prices: nothing extra is stored.
    result = await ha.config_entries.options.async_configure(
        result["flow_id"], {**suggested, "wastewater_ratio": 0.95}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"wastewater_ratio": 0.95}

    # Different prices: stored for this financial year only.
    result = await ha.config_entries.options.async_init(entry.entry_id)
    result = await ha.config_entries.options.async_configure(
        result["flow_id"], {**suggested, "water_rate": 2.5}
    )
    assert entry.options == {
        "wastewater_ratio": 0.785,
        "tariff_overrides": {
            "2026": {"water_rate": 2.5, "wastewater_rate": 4.28, "fixed_charge": 355.9}
        },
    }


def test_store_year_prices_removes_an_override_matching_the_table() -> None:
    options = {
        "wastewater_ratio": 0.785,
        "tariff_overrides": {
            "2026": {"water_rate": 2.5, "wastewater_rate": 4.28, "fixed_charge": 355.9},
            "2027": {"water_rate": 3.0, "wastewater_rate": 5.0, "fixed_charge": 400.0},
        },
    }
    published = {"water_rate": 2.46, "wastewater_rate": 4.28, "fixed_charge": 355.9}

    result = store_year_prices(options, 2026, published)

    assert result == {
        "wastewater_ratio": 0.785,
        "tariff_overrides": {
            "2027": {"water_rate": 3.0, "wastewater_rate": 5.0, "fixed_charge": 400.0}
        },
    }
    assert store_year_prices({"tariff_overrides": {}}, 2026, published) == {}


async def test_validate_login_signs_in_and_reads_the_account(ha: HomeAssistant) -> None:
    async def _sign_in(self: WatercareApi) -> None:
        self._refresh_token = "issued"

    async def _account(self: WatercareApi) -> AccountSummary:
        assert ACCOUNT is not None
        return ACCOUNT

    with (
        patch.object(
            WatercareApi, "async_sign_in", autospec=True, side_effect=_sign_in
        ),
        patch.object(
            WatercareApi, "async_get_account", autospec=True, side_effect=_account
        ),
    ):
        account, token = await async_validate_login(ha, EMAIL, PASSWORD)

    assert account == ACCOUNT
    assert token == "issued"


async def test_reconfigure_without_unique_id_or_refresh_token(
    ha: HomeAssistant, mock_login: AsyncMock, mock_setup: AsyncMock
) -> None:
    entry = make_entry(unique_id=None)
    entry.add_to_hass(ha)
    mock_login.return_value = (ACCOUNT, None)
    result = await entry.start_reconfigure_flow(ha)

    result = await ha.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: EMAIL, CONF_PASSWORD: "new-password"}
    )

    assert result["reason"] == "reconfigure_successful"
    assert "refresh_token" not in entry.data
