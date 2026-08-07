"""Config flow for Watercare integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

from .api import WatercareApi, WatercareAuthError
from .const import (
    DOMAIN,
    CONF_CONSUMPTION_RATE,
    CONF_WASTEWATER_RATE,
    CONF_WASTEWATER_RATIO,
    CONF_ANNUAL_LINE_CHARGE,
    CONF_ENDPOINT,
    DEFAULT_CONSUMPTION_RATE,
    DEFAULT_WASTEWATER_RATE,
    DEFAULT_WASTEWATER_RATIO,
    DEFAULT_ANNUAL_LINE_CHARGE,
    DEFAULT_ENDPOINT,
    ENDPOINT_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

RATE_DEFAULTS = {
    CONF_ENDPOINT: DEFAULT_ENDPOINT,
    CONF_CONSUMPTION_RATE: DEFAULT_CONSUMPTION_RATE,
    CONF_WASTEWATER_RATE: DEFAULT_WASTEWATER_RATE,
    CONF_WASTEWATER_RATIO: DEFAULT_WASTEWATER_RATIO,
    CONF_ANNUAL_LINE_CHARGE: DEFAULT_ANNUAL_LINE_CHARGE,
}

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_ENDPOINT, default=DEFAULT_ENDPOINT): vol.In(ENDPOINT_OPTIONS),
        vol.Optional(
            CONF_CONSUMPTION_RATE, default=DEFAULT_CONSUMPTION_RATE
        ): vol.Coerce(float),
        vol.Optional(CONF_WASTEWATER_RATE, default=DEFAULT_WASTEWATER_RATE): vol.Coerce(
            float
        ),
        vol.Optional(
            CONF_WASTEWATER_RATIO, default=DEFAULT_WASTEWATER_RATIO
        ): vol.Coerce(float),
        vol.Optional(
            CONF_ANNUAL_LINE_CHARGE, default=DEFAULT_ANNUAL_LINE_CHARGE
        ): vol.Coerce(float),
    }
)


async def _validate_login(email: str, password: str) -> str | None:
    """Check credentials against Watercare; return the account number."""
    api = WatercareApi(email, password)
    await api.get_refresh_token()
    return api.account_number


class WatercareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Watercare."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                account = await _validate_login(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except WatercareAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 - surface anything else as connection trouble
                _LOGGER.exception("Unexpected error validating Watercare credentials")
                errors["base"] = "cannot_connect"
            else:
                if not account:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(str(account))
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Watercare",
                        data={
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                        },
                        options={
                            key: user_input.get(key, default)
                            for key, default in RATE_DEFAULTS.items()
                        },
                    )

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication when Watercare rejects the credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password and revalidate."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        username = entry.data.get(CONF_USERNAME) or entry.data.get("email")

        if user_input is not None:
            try:
                await _validate_login(username, user_input[CONF_PASSWORD])
            except WatercareAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Watercare reauth")
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": username or ""},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WatercareOptionsFlowHandler:
        """Get the options flow for this handler."""
        return WatercareOptionsFlowHandler()


class WatercareOptionsFlowHandler(OptionsFlow):
    """Handle options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Show saved options first, then values from initial setup, then the
        # built-in defaults.
        current = {**self.config_entry.data, **self.config_entry.options}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENDPOINT,
                        default=current.get(CONF_ENDPOINT, DEFAULT_ENDPOINT),
                    ): vol.In(ENDPOINT_OPTIONS),
                    vol.Optional(
                        CONF_CONSUMPTION_RATE,
                        default=current.get(
                            CONF_CONSUMPTION_RATE, DEFAULT_CONSUMPTION_RATE
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_WASTEWATER_RATE,
                        default=current.get(
                            CONF_WASTEWATER_RATE, DEFAULT_WASTEWATER_RATE
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_WASTEWATER_RATIO,
                        default=current.get(
                            CONF_WASTEWATER_RATIO, DEFAULT_WASTEWATER_RATIO
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_ANNUAL_LINE_CHARGE,
                        default=current.get(
                            CONF_ANNUAL_LINE_CHARGE, DEFAULT_ANNUAL_LINE_CHARGE
                        ),
                    ): vol.Coerce(float),
                }
            ),
        )
