"""Diagnostics support for the Northumbrian Water integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import NorthumbrianWaterConfigEntry
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_METER_SERIAL,
    CONF_PERSON_ID,
    CONF_PREMISE_ID,
)

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_ACCOUNT_ID,
    CONF_PREMISE_ID,
    CONF_PERSON_ID,
    CONF_METER_SERIAL,
    CONF_ADDRESS,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NorthumbrianWaterConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "lookback_days": coordinator.lookback_days,
        "last_import": {
            "hours_imported": data.hours_imported if data else None,
            "days_with_data": data.days_with_data if data else None,
            "latest_reading_start": (
                data.latest_reading_start.isoformat()
                if data and data.latest_reading_start
                else None
            ),
            "latest_day_litres": data.latest_day_litres if data else None,
        },
    }
