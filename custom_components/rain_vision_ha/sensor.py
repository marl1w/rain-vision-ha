"""Sensors for rain-vision-ha."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
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


def _is_active_schedule(s: Any) -> bool:
    return s.active and s.start_time != "00:00" and any(d > 0 for d in s.zone_durations)


def _is_meaningful_schedule(s: Any) -> bool:
    """Return True when a schedule has real configuration (not an empty placeholder slot)."""
    return s.start_time != "00:00" or any(d > 0 for d in s.zone_durations)


def _schedule_attrs(data: RainVisionData) -> dict[str, Any]:
    active_schedules = [s for s in data.schedules if _is_active_schedule(s)]
    disabled_schedules = [
        s for s in data.schedules
        if _is_meaningful_schedule(s) and not _is_active_schedule(s)
    ]

    def _group_by_program(schedules: list) -> dict[str, list[str]]:
        by_program: dict[str, list[str]] = {}
        for s in schedules:
            by_program.setdefault(s.program_name, []).append(s.start_time)
        return {p: sorted(times) for p, times in sorted(by_program.items())}

    return {
        "active_cycle_count": len(active_schedules),
        "active_programs": _group_by_program(active_schedules),
        "disabled_cycle_count": len(disabled_schedules),
        "disabled_programs": _group_by_program(disabled_schedules),
        "total_icycle_count": len(data.schedules),
        "decoded_programs": sorted({s.program_name for s in data.schedules}),
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
            {s.program_name for s in data.schedules if _is_active_schedule(s)}
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
        entities.append(RainVisionZoneRemainingSensor(coordinator, entry, zone))

    entities.append(RainVisionDailyIrrigationSensor(coordinator, entry, enabled_zones))
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


class RainVisionZoneRemainingSensor(RainVisionBaseSensor):
    """Per-zone remaining irrigation time sensor."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-outline"

    def __init__(
        self,
        coordinator: RainVisionCdcCoordinator,
        entry: ConfigEntry,
        zone: int,
    ) -> None:
        """Initialize the zone remaining sensor."""

        super().__init__(coordinator, entry)
        self.zone = zone
        self._attr_name = f"Zone {zone} remaining time"
        self._attr_unique_id = f"{entry.unique_id}_zone_{zone}_remaining_time"

    @property
    def native_value(self) -> int | None:
        """Return remaining seconds for this zone."""

        if self.coordinator.data is None:
            return None
        return self.coordinator.data.zone_remaining(self.zone)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return zone attributes."""

        return {
            "zone": self.zone,
            "cdc_type_id": CDC_6_ZONE_TYPE_ID,
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
        """Return polling mode: none, normal, fast, or paused."""

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


class RainVisionDailyIrrigationSensor(CoordinatorEntity[RainVisionCdcCoordinator], SensorEntity):
    """Single sensor tracking today's total irrigation with per-zone attributes.

    State: total minutes irrigated today across all zones.
    Attributes: per-zone breakdown (zone_1, zone_2, …).

    Updates via dispatcher when the advisor captures an irrigation event at
    schedule time, and at midnight via a separate reset callback.
    """

    _attr_has_entity_name = True
    _attr_name = "Today's Irrigation"
    _attr_icon = "mdi:water"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: RainVisionCdcCoordinator,
        entry: ConfigEntry,
        enabled_zones: list[int],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._enabled_zones = enabled_zones
        self._attr_unique_id = f"{entry.unique_id}_daily_irrigation"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Update whenever the advisor captures an irrigation event or resets at midnight
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_irrigation_updated_{self._entry.entry_id}",
                self.async_write_ha_state,
            )
        )

    @property
    def native_value(self) -> int:
        irrigation = self._get_irrigation()
        return sum(irrigation.values()) if irrigation else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        irrigation = self._get_irrigation()
        return {
            f"zone_{zk}": irrigation.get(zk, 0)
            for zk in [str(z) for z in self._enabled_zones]
        }

    def _get_irrigation(self) -> dict[str, int]:
        service = self.hass.data.get(DOMAIN, {}).get("_advisor_service")
        if service is None:
            return {}
        return service.get_today_irrigation(self._entry.entry_id)
