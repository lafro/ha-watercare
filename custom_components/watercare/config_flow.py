"""Config flow for the Watercare integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import WatercareApi, WatercareAuthError, WatercareConnectionError
from .const import (
    CONF_FIXED_CHARGE,
    CONF_REFRESH_TOKEN,
    CONF_TARIFF_OVERRIDES,
    CONF_WASTEWATER_RATE,
    CONF_WASTEWATER_RATIO,
    CONF_WATER_RATE,
    DEFAULT_WASTEWATER_RATIO,
    DOMAIN,
    NZ_TIMEZONE,
)
from .models import AccountSummary
from .tariffs import (
    PUBLISHED_TARIFFS,
    financial_year,
    financial_year_label,
    schedule_from_options,
    tariff_from_user_input,
)

_LOGGER = logging.getLogger(__name__)

_EMAIL = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
_PASSWORD = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)

USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_USERNAME): _EMAIL,
        probatio.Required(CONF_PASSWORD): _PASSWORD,
    }
)
REAUTH_SCHEMA = probatio.Schema({probatio.Required(CONF_PASSWORD): _PASSWORD})


def _rate_selector(unit: str, step: float) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=10000,
            step=step,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


TARIFF_FIELDS: dict[str, NumberSelector] = {
    CONF_WATER_RATE: _rate_selector("NZD/kL", 0.001),
    CONF_WASTEWATER_RATE: _rate_selector("NZD/kL", 0.001),
    CONF_FIXED_CHARGE: _rate_selector("NZD/yr", 0.01),
}


def tariff_schema() -> probatio.Schema:
    """Return the schema for one financial year's prices."""
    return probatio.Schema(
        {probatio.Required(key): selector for key, selector in TARIFF_FIELDS.items()}
    )


async def async_validate_login(
    hass: HomeAssistant, email: str, password: str
) -> tuple[AccountSummary, str | None]:
    """Sign in and return the account and the refresh token."""
    api = WatercareApi(email, password, async_get_clientsession(hass))
    await api.async_sign_in()
    account = await api.async_get_account()
    return account, api.refresh_token


class WatercareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Watercare."""

    VERSION = 1
    MINOR_VERSION = 2

    async def _async_try_login(
        self, email: str, password: str, errors: dict[str, str]
    ) -> tuple[AccountSummary, str | None] | None:
        try:
            return await async_validate_login(self.hass, email, password)
        except WatercareAuthError:
            errors["base"] = "invalid_auth"
        except WatercareConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error while signing in to Watercare")
            errors["base"] = "unknown"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the Watercare sign-in."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_try_login(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD], errors
            )
            if result is not None:
                account, refresh_token = result
                await self.async_set_unique_id(account.account_number)
                self._abort_if_unique_id_configured()
                data: dict[str, Any] = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                if refresh_token:
                    data[CONF_REFRESH_TOKEN] = refresh_token
                return self.async_create_entry(
                    title="Watercare",
                    data=data,
                    options={CONF_WASTEWATER_RATIO: DEFAULT_WASTEWATER_RATIO},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after Watercare rejected the stored login."""
        del entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the current password."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        username = str(entry.data.get(CONF_USERNAME, ""))
        if user_input is not None:
            result = await self._async_try_login(
                username, user_input[CONF_PASSWORD], errors
            )
            if result is not None:
                account, refresh_token = result
                await self.async_set_unique_id(account.account_number)
                if entry.unique_id is not None:
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=_login_data(
                        username, user_input[CONF_PASSWORD], refresh_token
                    ),
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"username": username},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the sign-in for the same Watercare account."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            result = await self._async_try_login(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD], errors
            )
            if result is not None:
                account, refresh_token = result
                await self.async_set_unique_id(account.account_number)
                if entry.unique_id is not None:
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=_login_data(
                        user_input[CONF_USERNAME],
                        user_input[CONF_PASSWORD],
                        refresh_token,
                    ),
                )
        suggested = user_input or {CONF_USERNAME: entry.data.get(CONF_USERNAME)}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, suggested),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WatercareOptionsFlow:
        """Return the options flow."""
        del config_entry
        return WatercareOptionsFlow()


def _login_data(email: str, password: str, refresh_token: str | None) -> dict[str, Any]:
    data: dict[str, Any] = {CONF_USERNAME: email, CONF_PASSWORD: password}
    if refresh_token:
        data[CONF_REFRESH_TOKEN] = refresh_token
    return data


def store_year_prices(
    options: Mapping[str, Any], year: int, user_input: Mapping[str, Any]
) -> dict[str, Any]:
    """Return options with one financial year's prices set.

    Prices equal to the published ones are not stored, so a later release's
    corrections to the table still apply.
    """
    overrides: dict[str, Any] = dict(options.get(CONF_TARIFF_OVERRIDES) or {})
    tariff = tariff_from_user_input(user_input)
    if PUBLISHED_TARIFFS.get(year) == tariff:
        overrides.pop(str(year), None)
    else:
        overrides[str(year)] = tariff.as_options()
    new_options = {
        key: value for key, value in options.items() if key != CONF_TARIFF_OVERRIDES
    }
    if overrides:
        new_options[CONF_TARIFF_OVERRIDES] = overrides
    return new_options


class WatercareOptionsFlow(OptionsFlowWithReload):
    """Edit the wastewater ratio and this financial year's prices."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        year = financial_year(dt_util.now(NZ_TIMEZONE).date())
        options = self.config_entry.options
        if user_input is not None:
            new_options = store_year_prices(options, year, user_input)
            new_options[CONF_WASTEWATER_RATIO] = float(
                user_input[CONF_WASTEWATER_RATIO]
            )
            return self.async_create_entry(data=new_options)

        schedule = schedule_from_options(options)
        suggested: dict[str, Any] = {
            **schedule.best_known(year).as_options(),
            CONF_WASTEWATER_RATIO: options.get(
                CONF_WASTEWATER_RATIO, DEFAULT_WASTEWATER_RATIO
            ),
        }
        schema = probatio.Schema(
            {
                probatio.Required(CONF_WASTEWATER_RATIO): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=1, step=0.001, mode=NumberSelectorMode.BOX
                    )
                ),
                **{
                    probatio.Required(key): selector
                    for key, selector in TARIFF_FIELDS.items()
                },
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            description_placeholders={"financial_year": financial_year_label(year)},
        )
