"""Zone configuration helpers for rain-vision-ha."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .const import CDC_ZONES, CONF_ENABLED_ZONES, CONF_PRODUCT_ID


def available_zones(product_id: int | None = None) -> list[int]:
    """Return available zones for the discovered product."""

    del product_id
    return list(range(1, CDC_ZONES + 1))


def zone_options(product_id: int | None = None) -> dict[str, str]:
    """Return multi-select options keyed as strings for config forms."""

    return {
        str(zone): f"Zone {zone}"
        for zone in available_zones(product_id)
    }


def normalize_enabled_zones(
    raw: Any,
    product_id: int | None = None,
    *,
    fallback_to_available: bool = True,
) -> list[int]:
    """Normalize enabled zones from form/config data."""

    available = available_zones(product_id)
    available_set = set(available)

    if raw is None:
        return available if fallback_to_available else []

    if isinstance(raw, (str, int)):
        raw_values: Iterable[Any] = [raw]
    elif isinstance(raw, Iterable):
        raw_values = raw
    else:
        return available if fallback_to_available else []

    zones: list[int] = []
    for value in raw_values:
        try:
            zone = int(value)
        except (TypeError, ValueError):
            continue
        if zone in available_set and zone not in zones:
            zones.append(zone)

    if zones:
        return zones
    return available if fallback_to_available else []


def enabled_zones_from_entry(entry: Any) -> list[int]:
    """Return enabled zones for an entry, preferring runtime options."""

    product_id = entry.data.get(CONF_PRODUCT_ID)
    return normalize_enabled_zones(
        entry.options.get(CONF_ENABLED_ZONES, entry.data.get(CONF_ENABLED_ZONES)),
        product_id,
    )
