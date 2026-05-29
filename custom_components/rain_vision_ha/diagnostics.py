"""Diagnostics for rain-vision-ha."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD, DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics with the password redacted."""

    coordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": {
            **{key: value for key, value in entry.data.items() if key != CONF_PASSWORD},
            CONF_PASSWORD: "**REDACTED**",
        },
        "data": coordinator.data.as_diagnostics() if coordinator.data else None,
    }
