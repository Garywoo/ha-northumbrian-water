"""The Northumbrian Water integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

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
)
from .coordinator import NorthumbrianWaterCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass(slots=True)
class NorthumbrianWaterData:
    """Runtime data stored on the config entry."""

    client: NorthumbrianWaterClient
    coordinator: NorthumbrianWaterCoordinator


type NorthumbrianWaterConfigEntry = ConfigEntry[NorthumbrianWaterData]


async def async_setup_entry(
    hass: HomeAssistant, entry: NorthumbrianWaterConfigEntry
) -> bool:
    """Set up Northumbrian Water from a config entry."""
    # A dedicated session keeps the portal's session cookies isolated from the
    # rest of Home Assistant's HTTP traffic. The cookie jar is passed explicitly
    # because the portal's usage endpoints authenticate against the server-side
    # session, so silently dropping cookies would break them.
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    client = NorthumbrianWaterClient(
        session,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
    )

    meter = MeterInfo(
        account_id=entry.data[CONF_ACCOUNT_ID],
        premise_id=entry.data[CONF_PREMISE_ID],
        person_id=entry.data[CONF_PERSON_ID],
        meter_serial=entry.data[CONF_METER_SERIAL],
        address=entry.data.get(CONF_ADDRESS),
    )

    try:
        # Logs in and selects this meter's account in the portal session, which
        # the usage endpoints need before they will return anything.
        await client.async_bind_account(meter)
    except NWLSessionError as err:
        # Checked first: it subclasses NWLAuthError but means the portal
        # dropped the session, which a retry fixes and a new password does not.
        raise ConfigEntryNotReady(str(err)) from err
    except NWLAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except NWLApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = NorthumbrianWaterCoordinator(
        hass,
        client,
        meter,
        entry.options.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS),
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = NorthumbrianWaterData(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # No update listener: the options flow subclasses OptionsFlowWithReload,
    # so Home Assistant reloads this entry itself when the options change.
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: NorthumbrianWaterConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
