"""Tests for the Watercare config flow.

WatercareApi (the network/auth layer) is mocked throughout -- these tests
never touch the real Watercare API.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.watercare.api import WatercareAuthError
from custom_components.watercare.const import (
    CONF_ANNUAL_LINE_CHARGE,
    CONF_CONSUMPTION_RATE,
    CONF_ENDPOINT,
    CONF_WASTEWATER_RATE,
    CONF_WASTEWATER_RATIO,
    DEFAULT_ANNUAL_LINE_CHARGE,
    DEFAULT_CONSUMPTION_RATE,
    DEFAULT_WASTEWATER_RATE,
    DEFAULT_WASTEWATER_RATIO,
    DOMAIN,
)

USER_INPUT = {
    "username": "test@example.com",
    "password": "hunter2",
}

# A minimal-but-valid mechanicalmonthly billing-period payload. Needed
# because a successful config flow schedules a real async_setup_entry as a
# background task -- if that ever runs (e.g. during fixture teardown) with
# no usable response, the coordinator's first refresh raises UpdateFailed.
_VALID_BILLING_PERIODS = json.dumps(
    [
        {
            "billingPeriodFromDate": "2026-07-01T00:00:00.000Z",
            "billingPeriodToDate": "2026-07-31T00:00:00.000Z",
            "waterUsage": 10000,
            "readingType": "A",
            "statistics": {"numberOfDays": 31, "dailyAverage": 322.6},
        }
    ]
)


def _mock_api(account_number: str = "123456") -> MagicMock:
    """A WatercareApi stand-in that "authenticates" successfully.

    Also stands in for the *second*, independent WatercareApi instantiation
    that __init__.py's async_setup_entry makes for the coordinator, since a
    successful flow schedules that setup as a background task.
    """
    api = MagicMock()
    api.get_refresh_token = AsyncMock()
    api.get_data = AsyncMock(return_value=_VALID_BILLING_PERIODS)
    api.account_number = account_number
    api.account = {"accountNumber": account_number}
    return api


def _patch_watercare_api(mock_api: MagicMock):
    """Patch WatercareApi everywhere it gets instantiated: config_flow's own
    login check, and __init__.py's setup of the coordinator that follows a
    successful flow."""
    return (
        patch(
            "custom_components.watercare.config_flow.WatercareApi",
            return_value=mock_api,
        ),
        patch(
            "custom_components.watercare.WatercareApi",
            return_value=mock_api,
        ),
    )


async def test_successful_setup_creates_entry_with_fixed_endpoint(
    recorder_mock, enable_custom_integrations, hass
) -> None:
    mock_api = _mock_api(account_number="987654")
    patch_flow, patch_init = _patch_watercare_api(mock_api)

    with patch_flow, patch_init:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    entry = result2["result"]
    assert entry.unique_id == "987654"
    assert entry.data["username"] == "test@example.com"
    assert entry.data["password"] == "hunter2"

    # The endpoint is fixed to mechanicalmonthly regardless of user input --
    # it is not exposed as a choice in DATA_SCHEMA at all.
    assert entry.options[CONF_ENDPOINT] == "mechanicalmonthly"
    assert entry.options[CONF_CONSUMPTION_RATE] == DEFAULT_CONSUMPTION_RATE
    assert entry.options[CONF_WASTEWATER_RATE] == DEFAULT_WASTEWATER_RATE
    assert entry.options[CONF_WASTEWATER_RATIO] == DEFAULT_WASTEWATER_RATIO
    assert entry.options[CONF_ANNUAL_LINE_CHARGE] == DEFAULT_ANNUAL_LINE_CHARGE


async def test_custom_rates_are_written_into_options(
    recorder_mock, enable_custom_integrations, hass
) -> None:
    mock_api = _mock_api(account_number="111222")
    patch_flow, patch_init = _patch_watercare_api(mock_api)

    with patch_flow, patch_init:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                **USER_INPUT,
                CONF_CONSUMPTION_RATE: 2.5,
                CONF_WASTEWATER_RATE: 4.1,
                CONF_WASTEWATER_RATIO: 0.8,
                CONF_ANNUAL_LINE_CHARGE: 350,
            },
        )
        await hass.async_block_till_done()

    entry = result2["result"]
    assert entry.options[CONF_CONSUMPTION_RATE] == 2.5
    assert entry.options[CONF_WASTEWATER_RATE] == 4.1
    assert entry.options[CONF_WASTEWATER_RATIO] == 0.8
    assert entry.options[CONF_ANNUAL_LINE_CHARGE] == 350
    # Still fixed even when the user supplies other overrides.
    assert entry.options[CONF_ENDPOINT] == "mechanicalmonthly"


async def test_invalid_credentials_show_invalid_auth_error(
    recorder_mock, enable_custom_integrations, hass
) -> None:
    mock_api = MagicMock()
    mock_api.get_refresh_token = AsyncMock(
        side_effect=WatercareAuthError("bad credentials")
    )

    with patch(
        "custom_components.watercare.config_flow.WatercareApi", return_value=mock_api
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["step_id"] == "user"
    assert result2["errors"] == {"base": "invalid_auth"}


async def test_second_setup_of_same_account_aborts_already_configured(
    recorder_mock, enable_custom_integrations, hass
) -> None:
    mock_api = _mock_api(account_number="555555")
    patch_flow, patch_init = _patch_watercare_api(mock_api)

    with patch_flow, patch_init:
        first = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        first_result = await hass.config_entries.flow.async_configure(
            first["flow_id"], USER_INPUT
        )
        assert first_result["type"] == FlowResultType.CREATE_ENTRY

        second = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        second_result = await hass.config_entries.flow.async_configure(
            second["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert second_result["type"] == FlowResultType.ABORT
    assert second_result["reason"] == "already_configured"


async def test_no_account_returned_shows_cannot_connect(
    recorder_mock, enable_custom_integrations, hass
) -> None:
    """Sign-in succeeds but Watercare returns no account -- treat as a
    connection problem, not a silent success."""
    mock_api = MagicMock()
    mock_api.get_refresh_token = AsyncMock()
    mock_api.account_number = None
    mock_api.account = None

    with patch(
        "custom_components.watercare.config_flow.WatercareApi", return_value=mock_api
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}
