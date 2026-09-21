"""Config flow: Exchange account, calendars (subentries), options, reauth, reconfigure."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_client
from .client import AUTH_BASIC, AUTH_NTLM, normalize_url
from .const import (
    AUTH_AUTO,
    CONF_AUTH_METHOD,
    CONF_DAYS_AHEAD,
    CONF_EXCLUDE_CANCELLED,
    CONF_EXCLUDE_DECLINED,
    CONF_INCLUDE_DESCRIPTION,
    CONF_MAILBOX,
    CONF_SCAN_INTERVAL,
    DEFAULT_OPTIONS,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    SUBENTRY_CALENDAR,
)
from .models import (
    EwsAuthError,
    EwsConnectionError,
    EwsError,
    EwsResponseError,
    EwsSslError,
    FolderInfo,
)

_LOGGER = logging.getLogger(__name__)

AUTH_METHODS = [AUTH_AUTO, AUTH_NTLM, AUTH_BASIC]
MAILBOX_ERRORS = {
    "ErrorNonExistentMailbox": "mailbox_not_found",
    "ErrorInvalidSmtpAddress": "mailbox_not_found",
    "ErrorAccessDenied": "access_denied",
    "ErrorFolderNotFound": "access_denied",
    "ErrorItemNotFound": "access_denied",
}


def _password_selector() -> TextSelector:
    return TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _auth_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=AUTH_METHODS,
            translation_key=CONF_AUTH_METHOD,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


async def _connect(
    hass: HomeAssistant, data: dict[str, Any], mailbox: str | None = None
) -> tuple[dict[str, str], dict[str, str], FolderInfo | None]:
    """Resolve `auto` auth, read the calendar folder.

    Returns (errors, resolved data, folder). Never tries a second auth method after a
    rejected password: failed logins count towards the account lockout policy.
    """
    data = {**data, CONF_URL: normalize_url(data[CONF_URL])}
    requested = data.get(CONF_AUTH_METHOD, AUTH_AUTO)
    client = create_client({**data, CONF_AUTH_METHOD: AUTH_NTLM if requested == AUTH_AUTO else requested})
    try:
        if requested == AUTH_AUTO:
            offered = await client.detect_auth_methods()
            if not offered:
                return {"base": "no_auth_method"}, data, None
            data[CONF_AUTH_METHOD] = offered[0]
            await client.close()
            client = create_client(data)
        folder = await client.get_calendar_folder(mailbox)
    except EwsAuthError:
        return {"base": "invalid_auth"}, data, None
    except EwsSslError:
        return {"base": "ssl_error"}, data, None
    except EwsResponseError as err:
        if mailbox and err.code in MAILBOX_ERRORS:
            return {CONF_MAILBOX: MAILBOX_ERRORS[err.code]}, data, None
        _LOGGER.warning("EWS error: %s", err)
        return {"base": "ews_error"}, data, None
    except EwsConnectionError as err:
        _LOGGER.warning("Cannot connect to %s: %s", data[CONF_URL], err)
        return {"base": "cannot_connect"}, data, None
    except EwsError:
        _LOGGER.exception("Unexpected EWS error")
        return {"base": "unknown"}, data, None
    finally:
        await client.close()
    return {}, data, folder


class EwsCalendarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add an Exchange account."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = normalize_url(user_input[CONF_URL])
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(f"{url}|{username}".lower())
            self._abort_if_unique_id_configured()
            errors, data, folder = await _connect(self.hass, {**user_input, CONF_USERNAME: username})
            if not errors and folder is not None:
                return self.async_create_entry(
                    title=username,
                    data=data,
                    options=dict(DEFAULT_OPTIONS),
                    subentries=[
                        {
                            "subentry_type": SUBENTRY_CALENDAR,
                            "data": {CONF_MAILBOX: ""},
                            "title": username,
                            "unique_id": "",
                        }
                    ],
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_URL): str,
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): _password_selector(),
                vol.Required(CONF_AUTH_METHOD, default=AUTH_AUTO): _auth_selector(),
                vol.Required(CONF_VERIFY_SSL, default=True): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            description_placeholders={"ews_url_example": "https://mail.example.com/EWS/Exchange.asmx"},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, data, _ = await _connect(
                self.hass, {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            )
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _password_selector()}),
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change server URL, auth method, TLS verification or password."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, data, _ = await _connect(self.hass, {**entry.data, **user_input})
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        schema = vol.Schema(
            {
                vol.Required(CONF_URL): str,
                vol.Required(CONF_PASSWORD): _password_selector(),
                vol.Required(CONF_AUTH_METHOD): _auth_selector(),
                vol.Required(CONF_VERIFY_SSL): BooleanSelector(),
            }
        )
        suggested = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or suggested),
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EwsOptionsFlow:
        return EwsOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_CALENDAR: SharedCalendarFlow}


class EwsOptionsFlow(OptionsFlow):
    """Polling and filtering options for all calendars of the account."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **user_input,
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_DAYS_AHEAD: int(user_input[CONF_DAYS_AHEAD]),
                }
            )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=1440,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Required(CONF_DAYS_AHEAD): NumberSelector(
                    NumberSelectorConfig(min=1, max=365, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_INCLUDE_DESCRIPTION): BooleanSelector(),
                vol.Required(CONF_EXCLUDE_DECLINED): BooleanSelector(),
                vol.Required(CONF_EXCLUDE_CANCELLED): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, {**DEFAULT_OPTIONS, **self.config_entry.options}
            ),
        )


class SharedCalendarFlow(ConfigSubentryFlow):
    """Add another mailbox's calendar the account has access to."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        entry = self._get_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            mailbox = user_input[CONF_MAILBOX].strip()
            if any(sub.unique_id == mailbox.lower() for sub in entry.subentries.values()):
                return self.async_abort(reason="already_configured")
            errors, _, folder = await _connect(self.hass, dict(entry.data), mailbox)
            if not errors and folder is not None:
                return self.async_create_entry(
                    title=(user_input.get(CONF_NAME) or "").strip() or mailbox,
                    data={CONF_MAILBOX: mailbox},
                    unique_id=mailbox.lower(),
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_MAILBOX): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
                vol.Optional(CONF_NAME): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )
