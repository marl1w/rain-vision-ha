"""Switch entities for rain-vision-ha."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CDC_6_ZONE_NAME, CDC_ZONES, DOMAIN
from .coordinator import RainVisionCdcCoordinator
from .zones import enabled_zones_from_entry

DEFAULT_MANUAL_DURATION_SECONDS = 60
PENDING_START_TIMEOUT = timedelta(seconds=45)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""

    coordinator: RainVisionCdcCoordinator = hass.data[DOMAIN][entry.entry_id]
    enabled_zones = enabled_zones_from_entry(entry)
    async_add_entities(
        RainVisionZoneSwitch(coordinator, entry, zone)
        for zone in enabled_zones
    )


class RainVisionZoneSwitch(
    CoordinatorEntity[RainVisionCdcCoordinator],
    SwitchEntity,
):
    """Per-zone manual irrigation switch."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:sprinkler-variant"

    def __init__(
        self,
        coordinator: RainVisionCdcCoordinator,
        entry: ConfigEntry,
        zone: int,
    ) -> None:
        """Initialize switch entity."""

        super().__init__(coordinator)
        self.entry = entry
        self.zone = zone
        self._attr_name = f"Zone {zone} irrigation"
        base_id = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base_id}_zone_{zone}_irrigation"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title or CDC_6_ZONE_NAME,
            manufacturer="Rain",
            model=CDC_6_ZONE_NAME,
            sw_version=coordinator.data.firmware if coordinator.data else None,
            hw_version=coordinator.data.hardware if coordinator.data else None,
            serial_number=str(coordinator.data.product_id)
            if coordinator.data and coordinator.data.product_id
            else None,
        )
        self._pending_start_until: datetime | None = None

    def _pending_start_active(self) -> bool:
        """Return true while waiting for device state to reflect an accepted start."""

        if self._pending_start_until is None:
            return False
        return datetime.now().astimezone() <= self._pending_start_until

    def _clear_pending_start(self) -> None:
        """Clear pending start latch."""

        self._pending_start_until = None

    def _handle_coordinator_update(self) -> None:
        """Clear pending latch when the device state catches up or timeout elapses."""

        data = self.coordinator.data
        if data is not None and data.status is not None and data.status.zone_state == self.zone:
            self._clear_pending_start()
        elif self._pending_start_until is not None and not self._pending_start_active():
            self._clear_pending_start()
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool:
        """Return whether this zone is currently running."""

        data = self.coordinator.data
        if self._pending_start_active():
            return True
        if data is None or data.status is None:
            return False
        return data.status.zone_state == self.zone

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Return helpful zone attributes."""

        configured_seconds = 0
        if len(self.coordinator.manual_totals) >= self.zone:
            configured_seconds = self.coordinator.manual_totals[self.zone - 1]
        return {
            "configured_duration_seconds": configured_seconds,
            "configured_duration_minutes": configured_seconds // 60,
        }

    async def async_turn_on(self, **kwargs) -> None:
        """Start manual irrigation for this zone only."""

        configured_seconds = 0
        if len(self.coordinator.manual_totals) >= self.zone:
            configured_seconds = self.coordinator.manual_totals[self.zone - 1]
        duration = configured_seconds or DEFAULT_MANUAL_DURATION_SECONDS

        durations = [0] * CDC_ZONES
        durations[self.zone - 1] = duration
        self._pending_start_until = datetime.now().astimezone() + PENDING_START_TIMEOUT
        self.async_write_ha_state()
        try:
            await self.coordinator.async_start_manual_irrigation(durations)
        except Exception:
            self._clear_pending_start()
            self.async_write_ha_state()
            raise

    async def async_turn_off(self, **kwargs) -> None:
        """Stop irrigation."""

        self._clear_pending_start()
        await self.coordinator.async_stop_irrigation()
