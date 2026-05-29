"""Button entities for rain-vision-ha."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CDC_6_ZONE_NAME, DOMAIN
from .coordinator import RainVisionCdcCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities."""

    coordinator: RainVisionCdcCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RainVisionManualRefreshButton(coordinator, entry)])


class RainVisionManualRefreshButton(
    CoordinatorEntity[RainVisionCdcCoordinator],
    ButtonEntity,
):
    """Button that triggers an immediate coordinator refresh."""

    _attr_has_entity_name = True
    _attr_name = "Manual refresh"
    _attr_icon = "mdi:refresh"

    def __init__(
        self,
        coordinator: RainVisionCdcCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize button entity."""

        super().__init__(coordinator)
        self.entry = entry
        base_id = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base_id}_manual_refresh"
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
    def available(self) -> bool:
        """Keep the refresh button available even during BLE outages."""

        return True

    async def async_press(self) -> None:
        """Request an immediate device refresh."""

        await self.coordinator.async_request_refresh()
