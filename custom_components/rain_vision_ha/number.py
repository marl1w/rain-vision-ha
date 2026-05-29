"""Number entities for rain-vision-ha."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CDC_6_ZONE_NAME, CDC_ZONES, DOMAIN
from .coordinator import RainVisionCdcCoordinator
from .zones import enabled_zones_from_entry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities."""

    coordinator: RainVisionCdcCoordinator = hass.data[DOMAIN][entry.entry_id]
    enabled_zones = enabled_zones_from_entry(entry)
    async_add_entities(
        RainVisionZoneDurationNumber(coordinator, entry, zone)
        for zone in enabled_zones
    )


class RainVisionZoneDurationNumber(
    CoordinatorEntity[RainVisionCdcCoordinator],
    NumberEntity,
):
    """Per-zone manual duration in minutes."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-cog"
    _attr_native_min_value = 1
    _attr_native_max_value = 480
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: RainVisionCdcCoordinator,
        entry: ConfigEntry,
        zone: int,
    ) -> None:
        """Initialize duration entity."""

        super().__init__(coordinator)
        self.entry = entry
        self.zone = zone
        self._attr_name = f"Zone {zone} manual duration"
        self._attr_unique_id = f"{entry.unique_id}_zone_{zone}_manual_duration"
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

    @property
    def native_value(self) -> float:
        """Return configured duration in minutes."""

        seconds = 0
        if len(self.coordinator.manual_totals) >= self.zone:
            seconds = self.coordinator.manual_totals[self.zone - 1]
        minutes = seconds // 60
        return float(minutes if minutes > 0 else 1)

    async def async_set_native_value(self, value: float) -> None:
        """Update configured zone duration."""

        minutes = max(1, min(480, int(value)))
        if len(self.coordinator.manual_totals) < CDC_ZONES:
            self.coordinator.manual_totals = (self.coordinator.manual_totals + [0] * CDC_ZONES)[:CDC_ZONES]
        self.coordinator.manual_totals[self.zone - 1] = minutes * 60
        self.async_write_ha_state()
