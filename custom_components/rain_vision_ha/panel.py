"""Sidebar panel and WebSocket API for the Rain Vision irrigation advisor."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import voluptuous as vol
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CDC_ZONES, DOMAIN
from .coordinator import RainVisionCdcCoordinator
from .zones import enabled_zones_from_entry

_LOGGER = logging.getLogger(__name__)

_ADVISOR_STORAGE_VERSION = 1
_ADVISOR_STORAGE_KEY = f"{DOMAIN}.advisor_config"

_CALENDAR_STORAGE_VERSION = 1
_CALENDAR_STORAGE_KEY = f"{DOMAIN}.calendar"
_CALENDAR_RETENTION_DAYS = 30


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Register the sidebar panel and WebSocket commands (idempotent)."""

    if hass.data[DOMAIN].get("_panel_registered"):
        return
    hass.data[DOMAIN]["_panel_registered"] = True

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                f"/{DOMAIN}/panel.js",
                str(Path(__file__).parent / "www" / "panel.js"),
                False,
            )
        ]
    )

    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=f"{DOMAIN.replace('_', '-')}-panel",
        sidebar_title="Rain Vision",
        sidebar_icon="mdi:sprinkler-variant",
        frontend_url_path=DOMAIN,
        module_url=f"/{DOMAIN}/panel.js",
        embed_iframe=False,
        require_admin=False,
    )

    websocket_api.async_register_command(hass, _ws_get_devices)
    websocket_api.async_register_command(hass, _ws_get_advisor_config)
    websocket_api.async_register_command(hass, _ws_save_advisor_config)
    websocket_api.async_register_command(hass, _ws_get_calendar)
    websocket_api.async_register_command(hass, _ws_save_day_snapshot)
    websocket_api.async_register_command(hass, _ws_get_forecast)


def _advisor_store(hass: HomeAssistant) -> Store:
    return hass.data[DOMAIN].setdefault(
        "_advisor_store",
        Store(hass, _ADVISOR_STORAGE_VERSION, _ADVISOR_STORAGE_KEY),
    )


def _calendar_store(hass: HomeAssistant) -> Store:
    return hass.data[DOMAIN].setdefault(
        "_calendar_store",
        Store(hass, _CALENDAR_STORAGE_VERSION, _CALENDAR_STORAGE_KEY),
    )


def _device_payload(coordinator: RainVisionCdcCoordinator, entry) -> dict:
    """Serialise one coordinator into a JSON-safe dict for the frontend."""

    data = coordinator.data
    schedules = []
    if data and data.schedules:
        for s in data.schedules:
            has_zones = any(d > 0 for d in s.zone_durations)
            has_start = bool(s.start_time)
            if not (has_zones and has_start):
                continue
            schedules.append(
                {
                    "cycle_id": s.cycle_id,
                    "program_name": s.program_name,
                    "zone": s.zone,
                    "active": s.active,
                    "start_time": s.start_time,
                    "schedule_type": s.schedule_type,
                    "weekdays": s.weekdays,
                    "duration": s.duration,
                    "zone_durations": s.zone_durations,
                }
            )

    enabled = list(enabled_zones_from_entry(entry) or range(1, CDC_ZONES + 1))

    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "enabled_zones": enabled,
        "status": {
            "battery": data.battery if data else None,
            "status_name": data.status_name if data else "unknown",
            "next_irrigation": (
                data.next_irrigation.isoformat() if (data and data.next_irrigation) else None
            ),
            "firmware": data.firmware if data else None,
            "is_watering": data.is_watering if data else False,
            "connection_state": coordinator.connection_state,
            "polling_mode": coordinator.polling_mode,
        },
        "schedules": schedules,
        "manual_durations": coordinator.manual_totals,
    }


# ── Existing WebSocket commands ───────────────────────────────────────────────

@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_devices"})
@websocket_api.async_response
async def _ws_get_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return all configured Rain Vision devices with their current status."""

    devices = []
    for entry_id, obj in hass.data.get(DOMAIN, {}).items():
        if not isinstance(obj, RainVisionCdcCoordinator):
            continue
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            continue
        devices.append(_device_payload(obj, entry))

    connection.send_result(msg["id"], {"devices": devices})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/get_advisor_config",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.async_response
async def _ws_get_advisor_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return the stored advisor configuration for one device."""

    store = _advisor_store(hass)
    all_configs: dict = await store.async_load() or {}
    cfg = all_configs.get(msg["entry_id"], {})
    connection.send_result(
        msg["id"],
        {
            "zones": cfg.get("zones", {}),
            "weather_entities": cfg.get("weather_entities", {}),
            "forecast_entity": cfg.get("forecast_entity", None),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/save_advisor_config",
        vol.Required("entry_id"): str,
        vol.Required("config"): dict,
    }
)
@websocket_api.async_response
async def _ws_save_advisor_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Persist advisor configuration for one device."""

    store = _advisor_store(hass)
    all_configs: dict = await store.async_load() or {}
    all_configs[msg["entry_id"]] = msg["config"]
    await store.async_save(all_configs)
    connection.send_result(msg["id"], {"success": True})


# ── Calendar WebSocket commands ───────────────────────────────────────────────

@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/get_calendar",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.async_response
async def _ws_get_calendar(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return stored daily snapshots for one device."""

    store = _calendar_store(hass)
    all_data: dict = await store.async_load() or {}
    connection.send_result(msg["id"], {"days": all_data.get(msg["entry_id"], {})})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/save_day_snapshot",
        vol.Required("entry_id"): str,
        vol.Required("date"): str,
        vol.Required("snapshot"): dict,
    }
)
@websocket_api.async_response
async def _ws_save_day_snapshot(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Persist a daily weather + irrigation snapshot for one device."""

    store = _calendar_store(hass)
    all_data: dict = await store.async_load() or {}
    entry_data: dict = all_data.get(msg["entry_id"], {})
    entry_data[msg["date"]] = msg["snapshot"]

    # Prune entries older than the retention window
    cutoff = (date.today() - timedelta(days=_CALENDAR_RETENTION_DAYS)).isoformat()
    entry_data = {k: v for k, v in entry_data.items() if k >= cutoff}

    all_data[msg["entry_id"]] = entry_data
    await store.async_save(all_data)
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/get_forecast",
        vol.Required("entity_id"): str,
    }
)
@websocket_api.async_response
async def _ws_get_forecast(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Fetch daily forecast from a weather.* entity via the get_forecasts service."""

    try:
        result = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"type": "daily"},
            target={"entity_id": msg["entity_id"]},
            blocking=True,
            return_response=True,
        )
        forecasts = (result or {}).get(msg["entity_id"], {}).get("forecast", [])
        connection.send_result(msg["id"], {"forecast": forecasts})
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Failed to fetch forecast from %s: %s", msg["entity_id"], exc)
        connection.send_error(msg["id"], "forecast_error", str(exc))
