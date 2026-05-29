"""Sensors for rain-vision-ha."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CDC_6_ZONE_NAME, CDC_6_ZONE_TYPE_ID, DOMAIN
from .coordinator import RainVisionCdcCoordinator
from .models import RainVisionData
from .zones import enabled_zones_from_entry


@dataclass(frozen=True, kw_only=True)
class RainVisionSensorDescription(SensorEntityDescription):
    """Rain Vision sensor description."""

    value_fn: Callable[[RainVisionData], Any]
    attr_fn: Callable[[RainVisionData], dict[str, Any]] | None = None


def _status_attrs(data: RainVisionData) -> dict[str, Any]:
    if data.status is None:
        return {}
    return {
        "fsm_state": data.status.fsm_state,
        "zone_state": data.status.zone_state,
        "pump_active": data.status.pump_active,
        "rain_sensor_state": data.status.rain_sensor_state,
        "flags": data.status.flags,
    }


def _format_duration(seconds: int) -> str:
    """Format seconds into a compact human-readable duration."""

    if seconds <= 0:
        return "Off"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _format_zone_durations(zone_durations: list[int]) -> dict[str, str]:
    """Return zone durations keyed by zone label."""

    return {
        f"zone_{index}": _format_duration(duration)
        for index, duration in enumerate(zone_durations, start=1)
    }


def _schedule_attrs(data: RainVisionData) -> dict[str, Any]:
    active_schedules = [
        schedule
        for schedule in data.schedules
        if schedule.active and any(duration > 0 for duration in schedule.zone_durations)
    ]

    grouped_schedules: dict[str, dict[str, Any]] = {}
    for schedule in active_schedules:
        if schedule.program_name not in grouped_schedules:
            grouped_schedules[schedule.program_name] = {
                "program": schedule.program_name,
                "start_times": [],
                "zone_durations": _format_zone_durations(schedule.zone_durations),
            }
        grouped_schedules[schedule.program_name]["start_times"].append(schedule.start_time)

    return {
        "active_schedule_count": len({schedule.program_name for schedule in active_schedules}),
        "active_cycle_count": len(active_schedules),
        "schedules": json.dumps(list(grouped_schedules.values()), indent=2)
    }


SENSOR_DESCRIPTIONS: tuple[RainVisionSensorDescription, ...] = (
    RainVisionSensorDescription(
        key="battery",
        translation_key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.battery,
    ),
    RainVisionSensorDescription(
        key="status",
        translation_key="status",
        name="Status",
        icon="mdi:sprinkler",
        value_fn=lambda data: data.status_name,
        attr_fn=_status_attrs,
    ),
    RainVisionSensorDescription(
        key="active_zone",
        translation_key="active_zone",
        name="Active zone",
        icon="mdi:sprinkler-variant",
        value_fn=lambda data: data.status.zone_state if data.status else None,
    ),
    RainVisionSensorDescription(
        key="schedule_count",
        translation_key="schedule_count",
        name="Schedule program count",
        icon="mdi:calendar-clock",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(
            {
                schedule.program_name
                for schedule in data.schedules
                if schedule.active and any(duration > 0 for duration in schedule.zone_durations)
            }
        ),
        attr_fn=_schedule_attrs,
    ),
    RainVisionSensorDescription(
        key="next_irrigation",
        translation_key="next_irrigation",
        name="Next irrigation",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.next_irrigation,
    ),
    RainVisionSensorDescription(
        key="firmware",
        translation_key="firmware",
        name="Firmware",
        icon="mdi:chip",
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.firmware,
    ),
    RainVisionSensorDescription(
        key="rssi",
        translation_key="rssi",
        name="RSSI",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.rssi,
    ),
    RainVisionSensorDescription(
        key="weekly_irrigation_total",
        name="Weekly irrigation total",
        icon="mdi:water-sync",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.weekly_total // 60,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors."""

    coordinator: RainVisionCdcCoordinator = hass.data[DOMAIN][entry.entry_id]
    enabled_zones = enabled_zones_from_entry(entry)
    entities: list[SensorEntity] = [
        RainVisionSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    ]
    entities.append(RainVisionConnectionStateSensor(coordinator, entry))
    entities.append(RainVisionPollingModeSensor(coordinator, entry))
    entities.append(RainVisionNextDeviceRefreshSensor(coordinator, entry))

    for zone in enabled_zones:
        entities.append(RainVisionZoneTimeSensor(coordinator, entry, zone, "remaining"))
        entities.append(RainVisionZoneTimeSensor(coordinator, entry, zone, "weekly_total"))

    async_add_entities(entities)


class RainVisionBaseSensor(CoordinatorEntity[RainVisionCdcCoordinator], SensorEntity):
    """Base Rain Vision sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RainVisionCdcCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""

        super().__init__(coordinator)
        self.entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title or CDC_6_ZONE_NAME,
            manufacturer="Rain",
            model=CDC_6_ZONE_NAME,
            sw_version=coordinator.data.firmware if coordinator.data else None,
            hw_version=coordinator.data.hardware if coordinator.data else None,
            serial_number=str(coordinator.data.product_id) if coordinator.data and coordinator.data.product_id else None,
        )


class RainVisionSensor(RainVisionBaseSensor):
    """Generic coordinator-backed Rain Vision sensor."""

    entity_description: RainVisionSensorDescription

    def __init__(
        self,
        coordinator: RainVisionCdcCoordinator,
        entry: ConfigEntry,
        description: RainVisionSensorDescription,
    ) -> None:
        """Initialize the sensor."""

        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"

    @property
    def native_value(self) -> str | int | float | datetime | None:
        """Return the state."""

        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return attributes."""

        if self.coordinator.data is None or self.entity_description.attr_fn is None:
            return None
        return self.entity_description.attr_fn(self.coordinator.data)


class RainVisionZoneTimeSensor(RainVisionBaseSensor):
    """Per-zone time sensor."""

    def __init__(
        self,
        coordinator: RainVisionCdcCoordinator,
        entry: ConfigEntry,
        zone: int,
        kind: str,
    ) -> None:
        """Initialize the zone sensor."""

        super().__init__(coordinator, entry)
        self.zone = zone
        self.kind = kind
        if kind == "remaining":
            self._attr_name = f"Zone {zone} remaining time"
            self._attr_unique_id = f"{entry.unique_id}_zone_{zone}_remaining_time"
            self._attr_icon = "mdi:timer-outline"
            self._attr_native_unit_of_measurement = UnitOfTime.SECONDS
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            self._attr_name = f"Zone {zone} weekly irrigation total"
            self._attr_unique_id = f"{entry.unique_id}_zone_{zone}_weekly_total"
            self._attr_icon = "mdi:timer-check-outline"
            self._attr_native_unit_of_measurement = UnitOfTime.MINUTES
            self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.DURATION

    @property
    def native_value(self) -> int | None:
        """Return remaining seconds or weekly total minutes."""

        if self.coordinator.data is None:
            return None
        if self.kind == "remaining":
            return self.coordinator.data.zone_remaining(self.zone)
        return self.coordinator.data.zone_weekly_total(self.zone) // 60

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return zone attributes."""

        return {
            "zone": self.zone,
            "cdc_type_id": CDC_6_ZONE_TYPE_ID,
            "week_start": self.coordinator.week_start,
        }


class RainVisionConnectionStateSensor(RainVisionBaseSensor):
    """Sensor exposing BLE connection lifecycle state and timestamps."""

    def __init__(self, coordinator: RainVisionCdcCoordinator, entry: ConfigEntry) -> None:
        """Initialize the connection state sensor."""

        super().__init__(coordinator, entry)
        self._attr_name = "Device connection status"
        self._attr_unique_id = f"{entry.unique_id}_connection_state"
        self._attr_icon = "mdi:bluetooth-connect"

    @property
    def native_value(self) -> str:
        """Return current connection state."""

        return self.coordinator.connection_state_label

    @property
    def available(self) -> bool:
        """Keep the status sensor available even if the latest BLE poll failed."""

        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return connection lifecycle details."""

        return self.coordinator.connection_state_attributes


class RainVisionPollingModeSensor(RainVisionBaseSensor):
    """Sensor exposing current coordinator polling mode."""

    def __init__(self, coordinator: RainVisionCdcCoordinator, entry: ConfigEntry) -> None:
        """Initialize the polling mode sensor."""

        super().__init__(coordinator, entry)
        self._attr_name = "Polling mode"
        self._attr_unique_id = f"{entry.unique_id}_polling_mode"
        self._attr_icon = "mdi:timer-cog-outline"

    @property
    def native_value(self) -> str:
        """Return polling mode: none, normal, or fast."""

        return self.coordinator.polling_mode

    @property
    def available(self) -> bool:
        """Keep the polling mode sensor available even if the latest BLE poll failed."""

        return True


class RainVisionNextDeviceRefreshSensor(RainVisionBaseSensor):
    """Sensor exposing expected timestamp of next coordinator device refresh."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: RainVisionCdcCoordinator, entry: ConfigEntry) -> None:
        """Initialize the next device refresh sensor."""

        super().__init__(coordinator, entry)
        self._attr_name = "Next device refresh"
        self._attr_unique_id = f"{entry.unique_id}_next_device_refresh"
        self._attr_icon = "mdi:clock-outline"

    @property
    def native_value(self) -> datetime | None:
        """Return when the next BLE device refresh is expected."""

        return self.coordinator.next_device_refresh

    @property
    def available(self) -> bool:
        """Keep the next refresh sensor available even if the latest BLE poll failed."""

        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return timing diagnostics for next poll calculations."""

        completed = self.coordinator._last_poll_completed
        return {
            "polling_mode": self.coordinator.polling_mode,
            "interval_seconds": int(self.coordinator.update_interval.total_seconds()),
            "poll_in_progress": self.coordinator._poll_in_progress,
            "last_poll_completed": completed.isoformat() if completed else None,
        }
