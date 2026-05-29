"""rain-vision-ha integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.components import bluetooth
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import voluptuous as vol

from .const import CDC_ZONES, CONF_ADDRESS, CONF_PASSWORD, CONF_PRODUCT_ID, DOMAIN
from .coordinator import RainVisionCdcCoordinator
from .protocol import RainVisionCdcClient, decode_advertisement
from .zones import enabled_zones_from_entry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: tuple[Platform, ...] = (
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BUTTON,
)

SERVICE_START_MANUAL = "start_manual_irrigation"
SERVICE_STOP = "stop_irrigation"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up rain-vision-ha from a config entry without blocking startup."""

    address = entry.data[CONF_ADDRESS]
    product_id = entry.data.get(CONF_PRODUCT_ID)
    if product_id is None:
        service_info = bluetooth.async_last_service_info(
            hass,
            address,
            connectable=True,
        )
        if service_info is not None:
            advertisement = decode_advertisement(service_info)
            product_id = advertisement.product_id
            if product_id is not None:
                address = service_info.address
                hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_ADDRESS: address,
                        CONF_PRODUCT_ID: product_id,
                    },
                    unique_id=f"rv-{product_id}",
                )
    elif entry.unique_id != f"rv-{product_id}":
        hass.config_entries.async_update_entry(entry, unique_id=f"rv-{product_id}")

    client = RainVisionCdcClient(
        hass,
        address,
        entry.options.get(CONF_PASSWORD, entry.data[CONF_PASSWORD]),
        entry.title,
        product_id,
    )
    coordinator = RainVisionCdcCoordinator(
        hass,
        client,
        storage_key=f"{DOMAIN}_{entry.entry_id}_weekly_totals",
    )
    await coordinator.async_initialize()
    configured_zones = enabled_zones_from_entry(entry)
    coordinator.enabled_zones = set(configured_zones or range(1, CDC_ZONES + 1))
    coordinator.manual_totals = coordinator.mask_disabled_zones(coordinator.manual_totals)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _async_register_services(hass)
    hass.async_create_task(_async_deferred_first_refresh(coordinator))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after options update."""

    await hass.config_entries.async_reload(entry.entry_id)


async def _async_deferred_first_refresh(coordinator: RainVisionCdcCoordinator) -> None:
    """Refresh in the background so HA startup is never blocked by BLE."""

    _LOGGER.debug("Starting deferred first refresh for %s", coordinator.client.address)
    try:
        await coordinator.async_refresh()
    except Exception:
        _LOGGER.debug(
            "Deferred first refresh failed for %s; regular polling will retry",
            coordinator.client.address,
            exc_info=True,
        )
    else:
        _LOGGER.debug("Deferred first refresh completed for %s", coordinator.client.address)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register services once."""

    if hass.services.has_service(DOMAIN, SERVICE_START_MANUAL):
        return

    zone_schema = {
        vol.Required(f"zone_{zone}", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=28800))
        for zone in range(1, CDC_ZONES + 1)
    }

    async def _coordinators_from_call(call: ServiceCall) -> list[RainVisionCdcCoordinator]:
        device_ids = call.data.get("device_id")
        if isinstance(device_ids, str):
            device_ids = [device_ids]
        if not device_ids:
            return list(hass.data.get(DOMAIN, {}).values())

        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
        coordinators: list[RainVisionCdcCoordinator] = []
        for device_id in device_ids:
            device_entry = device_registry.async_get(device_id)
            if device_entry is None:
                continue
            for config_entry_id in device_entry.config_entries:
                coordinator = hass.data.get(DOMAIN, {}).get(config_entry_id)
                if coordinator and coordinator not in coordinators:
                    coordinators.append(coordinator)
            for entity_entry in er.async_entries_for_device(entity_registry, device_id):
                coordinator = hass.data.get(DOMAIN, {}).get(entity_entry.config_entry_id)
                if coordinator and coordinator not in coordinators:
                    coordinators.append(coordinator)
        return coordinators

    async def start_manual(call: ServiceCall) -> None:
        durations = [call.data[f"zone_{zone}"] for zone in range(1, CDC_ZONES + 1)]
        for coordinator in await _coordinators_from_call(call):
            await coordinator.async_start_manual_irrigation(durations)

    async def stop(call: ServiceCall) -> None:
        for coordinator in await _coordinators_from_call(call):
            await coordinator.async_stop_irrigation()

    hass.services.async_register(
        DOMAIN,
        SERVICE_START_MANUAL,
        start_manual,
        schema=vol.Schema({vol.Optional("device_id"): vol.Any(str, [str]), **zone_schema}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP,
        stop,
        schema=vol.Schema({vol.Optional("device_id"): vol.Any(str, [str])}),
    )
