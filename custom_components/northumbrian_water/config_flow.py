"""Config flow for the Northumbrian Water integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .api import (
    MeterInfo,
    NorthumbrianWaterClient,
    NWLApiError,
    NWLAuthError,
    NWLSessionError,
)
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_LOOKBACK_DAYS,
    CONF_METER_SERIAL,
    CONF_PERSON_ID,
    CONF_PREMISE_ID,
    DEFAULT_LOOKBACK_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class NorthumbrianWaterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the setup and reauthentication flows."""

    VERSION = 1

    def __init__(self) -> None:
        self._meters: list[MeterInfo] = []
        self._email: str = ""
        self._password: str = ""

    async def _async_discover(self, email: str, password: str) -> dict[str, str]:
        """Log in and list the meters. Returns form errors, empty when fine."""
        session = async_create_clientsession(self.hass)
        client = NorthumbrianWaterClient(session, email, password)
        try:
            await client.async_login()
            self._meters = await client.async_discover_meters()
        except NWLSessionError as err:
            # Checked before NWLAuthError, which it subclasses: the portal
            # dropped the session rather than refusing the credentials, so
            # telling the user to check their password would be wrong.
            _LOGGER.debug("Portal session failed during setup: %s", err)
            return {"base": "cannot_connect"}
        except NWLAuthError as err:
            _LOGGER.debug("Authentication failed: %s", err)
            return {"base": "invalid_auth"}
        except NWLApiError as err:
            _LOGGER.debug("Portal error during setup: %s", err)
            return {"base": "cannot_connect"}
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
            errors = await self._async_discover(self._email, self._password)
            if not errors:
                if len(self._meters) == 1:
                    return await self._async_create(self._meters[0])
                return await self.async_step_meter()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a meter when the login covers more than one."""
        if user_input is not None:
            serial = user_input[CONF_METER_SERIAL]
            meter = next(m for m in self._meters if m.meter_serial == serial)
            return await self._async_create(meter)

        options = {
            meter.meter_serial: (
                f"{meter.meter_serial} - {meter.address}"
                if meter.address
                else meter.meter_serial
            )
            for meter in self._meters
        }
        return self.async_show_form(
            step_id="meter",
            data_schema=vol.Schema({vol.Required(CONF_METER_SERIAL): vol.In(options)}),
        )

    async def _async_create(self, meter: MeterInfo) -> ConfigFlowResult:
        """Create (or update) the entry for one meter."""
        await self.async_set_unique_id(meter.meter_serial)
        data = {
            CONF_EMAIL: self._email,
            CONF_PASSWORD: self._password,
            CONF_ACCOUNT_ID: meter.account_id,
            CONF_PREMISE_ID: meter.premise_id,
            CONF_PERSON_ID: meter.person_id,
            CONF_METER_SERIAL: meter.meter_serial,
            CONF_ADDRESS: meter.address,
        }

        if self.source == "reauth":
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=self._title_for(meter), data=data)

    def _title_for(self, meter: MeterInfo) -> str:
        """Name the entry after the account it bills to.

        The meter serial is appended only when this login covers more than one
        meter: two meters on the same account would otherwise produce two
        entries with identical titles.
        """
        if not meter.account_id:
            return meter.address or f"Meter {meter.meter_serial}"
        title = f"Account Number {meter.account_id}"
        if len(self._meters or []) > 1:
            title = f"{title} ({meter.meter_serial})"
        return title

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the stored password stopped working."""
        self._email = entry_data.get(CONF_EMAIL, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect a new password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            errors = await self._async_discover(self._email, self._password)
            if not errors:
                entry = self._get_reauth_entry()
                stored_serial = entry.data[CONF_METER_SERIAL]
                meter = next(
                    (m for m in self._meters if m.meter_serial == stored_serial), None
                )
                if meter is None:
                    errors = {"base": "meter_missing"}
                else:
                    return await self._async_create(meter)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": self._email},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> NorthumbrianWaterOptionsFlow:
        """Return the options flow."""
        return NorthumbrianWaterOptionsFlow()


class NorthumbrianWaterOptionsFlow(OptionsFlowWithReload):
    """Let the lookback window be tuned after setup.

    OptionsFlowWithReload reloads the entry itself when the options change,
    which is how the new lookback window takes effect; the update listener
    that used to do it is deprecated for exactly this case.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_LOOKBACK_DAYS: int(user_input[CONF_LOOKBACK_DAYS])}
            )

        current = self.config_entry.options.get(
            CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOOKBACK_DAYS, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=2, max=60, step=1, mode=NumberSelectorMode.BOX
                        )
                    )
                }
            ),
        )
