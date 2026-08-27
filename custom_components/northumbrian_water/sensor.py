"""Diagnostic sensors for the Northumbrian Water integration.

The consumption history itself lives in the statistics tables, not in entity
state, because the portal publishes readings well after the fact. These sensors
exist so the integration's health is visible at a glance and so automations can
react when fresh readings land.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import NorthumbrianWaterConfigEntry
from .const import CURRENCY_GBP, DOMAIN
from .coordinator import ImportResult, NorthumbrianWaterCoordinator


@dataclass(frozen=True, kw_only=True)
class NWLSensorDescription(SensorEntityDescription):
    """Describes a Northumbrian Water diagnostic sensor."""

    value_fn: Callable[[ImportResult], float | int | datetime | None]


SENSORS: tuple[NWLSensorDescription, ...] = (
    NWLSensorDescription(
        key="last_reading_time",
        translation_key="last_reading_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Off unless asked for: useful when something looks wrong, noise
        # otherwise. Enable it from the entity's settings.
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.latest_reading_start,
    ),
    NWLSensorDescription(
        key="last_full_day_usage",
        translation_key="last_full_day_usage",
        device_class=SensorDeviceClass.WATER,
        # Deliberately no state_class. This is a daily total that arrives days
        # late, not a measurement, and the consumption history is fed to the
        # Energy dashboard as external statistics instead. Giving it a state
        # class makes the dashboard offer it as a water source, where it can
        # only ever fail validation with "last_reset missing", and would have
        # HA record statistics that double-count the imported ones.
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=lambda data: data.latest_day_litres,
    ),
    NWLSensorDescription(
        key="last_full_day_cost",
        translation_key="last_full_day_cost",
        device_class=SensorDeviceClass.MONETARY,
        # No state_class, for the same reason as latest_day_usage: this is a
        # daily total that arrives days late, and the Energy dashboard is fed
        # the cost as an external statistic instead.
        native_unit_of_measurement=CURRENCY_GBP,
        suggested_display_precision=2,
        value_fn=lambda data: data.latest_day_cost,
    ),
    NWLSensorDescription(
        key="hours_in_last_import",
        translation_key="hours_in_last_import",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.hours_imported,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NorthumbrianWaterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the diagnostic sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        NorthumbrianWaterSensor(coordinator, description) for description in SENSORS
    )


class NorthumbrianWaterSensor(
    CoordinatorEntity[NorthumbrianWaterCoordinator], SensorEntity
):
    """A sensor reporting on the most recent import."""

    _attr_has_entity_name = True
    entity_description: NWLSensorDescription

    def __init__(
        self,
        coordinator: NorthumbrianWaterCoordinator,
        description: NWLSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.meter.meter_serial
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="Northumbrian Water",
            name=f"NWL Water Meter ({serial})",
            model="Smart water meter",
            serial_number=serial,
        )

    @property
    def native_value(self) -> float | int | datetime | None:
        """Return the value derived from the last import."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, str | int | float] | None:
        """Expose the statistic id so it is easy to find in the Energy dashboard."""
        if self.entity_description.key != "hours_in_last_import":
            return None
        data = self.coordinator.data
        if data is None:
            return None
        return {
            "statistic_id": data.statistic_id,
            "cost_statistic_id": data.cost_statistic_id or "",
            "days_with_data": data.days_with_data,
        }
