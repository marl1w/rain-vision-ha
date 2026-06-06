"""Background daily snapshot service for the Rain Vision irrigation advisor.

Data collection strategy:
  - At each next_irrigation time: capture the schedule that is about to run.
    This is the ground truth — whatever the schedule says at that exact moment
    is what the device will execute, regardless of later config changes.
  - At 23:58 local time: save the day's weather stats and merge with the
    irrigation data already captured above (or leave irrigation empty if no
    irrigation ran today).
  - On startup: backfill the past BACKFILL_DAYS days with weather-only data
    (we have no way to know what was scheduled on past days).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CDC_ZONES, DOMAIN
from .coordinator import RainVisionCdcCoordinator
from .zones import enabled_zones_from_entry

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

_ADVISOR_STORAGE_VERSION = 1
_ADVISOR_STORAGE_KEY = f"{DOMAIN}.advisor_config"
_CALENDAR_STORAGE_VERSION = 1
_CALENDAR_STORAGE_KEY = f"{DOMAIN}.calendar"
_BACKFILL_DAYS = 7
_RETENTION_DAYS = 30


async def async_setup_advisor(hass: HomeAssistant) -> None:
    """Register the background advisor service (idempotent — safe to call per entry)."""
    if hass.data[DOMAIN].get("_advisor_registered"):
        return
    hass.data[DOMAIN]["_advisor_registered"] = True

    service = RainVisionAdvisorService(hass)
    hass.data[DOMAIN]["_advisor_service"] = service

    # End-of-day weather snapshot at 23:58 local time
    async_track_time_change(hass, service.async_daily_weather_snapshot, hour=23, minute=58, second=0)
    # Reset in-memory daily totals at midnight so the sensor starts fresh each day
    async_track_time_change(hass, service.async_reset_today, hour=0, minute=0, second=0)

    # Backfill runs after HA has fully started so the recorder is ready
    if hass.is_running:
        hass.async_create_task(service.async_startup_backfill())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, service.async_startup_backfill)


class RainVisionAdvisorService:
    """Collects and persists daily weather + irrigation snapshots in the background."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._advisor_store = Store(hass, _ADVISOR_STORAGE_VERSION, _ADVISOR_STORAGE_KEY)
        self._calendar_store = Store(hass, _CALENDAR_STORAGE_VERSION, _CALENDAR_STORAGE_KEY)
        # entry_id → cancel fn for the currently scheduled irrigation callback
        self._irrigation_callbacks: dict[str, Callable[[], None]] = {}
        # entry_id → the next_irrigation datetime we already scheduled
        self._scheduled_irrigation: dict[str, datetime] = {}
        # In-memory today's irrigation totals — read by the daily irrigation sensor
        self._today_irrigation: dict[str, dict[str, int]] = {}

    # ── Per-coordinator registration ──────────────────────────────────────────

    def get_today_irrigation(self, entry_id: str) -> dict[str, int]:
        """Return the in-memory irrigation totals for today (called by the sensor)."""
        return self._today_irrigation.get(entry_id, {})

    async def async_reset_today(self, _now=None) -> None:
        """Reset in-memory daily totals at midnight and notify sensors."""
        self._today_irrigation = {}
        for entry_id in list(self._hass.data.get(DOMAIN, {}).keys()):
            async_dispatcher_send(
                self._hass, f"{DOMAIN}_irrigation_updated_{entry_id}"
            )

    def register_coordinator(
        self,
        entry_id: str,
        coordinator: RainVisionCdcCoordinator,
        entry,
    ) -> None:
        """Subscribe to coordinator updates so we can track next_irrigation changes."""

        def _on_update() -> None:
            self._hass.async_create_task(
                self._handle_coordinator_update(entry_id, coordinator, entry)
            )

        unsubscribe = coordinator.async_add_listener(_on_update)
        # Store the unsubscribe so we can cancel it if the entry is unloaded
        self._hass.data[DOMAIN].setdefault("_advisor_unsubs", {})[entry_id] = unsubscribe

    # ── Coordinator update handler ────────────────────────────────────────────

    async def _handle_coordinator_update(
        self,
        entry_id: str,
        coordinator: RainVisionCdcCoordinator,
        entry,
    ) -> None:
        """Check if next_irrigation changed and (re-)schedule the capture callback."""
        data = coordinator.data
        if not data or not data.next_irrigation:
            return

        next_irr: datetime = data.next_irrigation
        now = dt_util.now()

        # Skip if the irrigation time is already in the past
        if next_irr <= now:
            return

        # Skip if we already have a callback scheduled for this same time
        already = self._scheduled_irrigation.get(entry_id)
        if already is not None and abs((already - next_irr).total_seconds()) < 60:
            return

        # Cancel any previous pending callback for this entry
        if cancel := self._irrigation_callbacks.pop(entry_id, None):
            cancel()

        self._scheduled_irrigation[entry_id] = next_irr
        _LOGGER.debug(
            "RainVision: scheduling irrigation capture at %s for entry %s",
            next_irr.isoformat(),
            entry_id,
        )

        async def _at_irrigation_time(_now: datetime) -> None:
            self._irrigation_callbacks.pop(entry_id, None)
            self._scheduled_irrigation.pop(entry_id, None)
            try:
                await self._capture_irrigation(entry_id, coordinator, entry)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "RainVision irrigation capture error for %s: %s", entry_id, exc
                )

        cancel = async_track_point_in_time(self._hass, _at_irrigation_time, next_irr)
        self._irrigation_callbacks[entry_id] = cancel

    # ── Irrigation capture (at scheduled time) ────────────────────────────────

    async def _capture_irrigation(
        self,
        entry_id: str,
        coordinator: RainVisionCdcCoordinator,
        entry,
    ) -> None:
        """Record the active schedule for today at the moment irrigation fires.

        This is the authoritative source of 'what actually ran' — captured before
        the user has any chance to change the schedule after the fact.
        """
        today = date.today()
        date_str = today.isoformat()

        irrigation: dict[str, int] = {}
        enabled = list(enabled_zones_from_entry(entry) or range(1, CDC_ZONES + 1))
        if coordinator.data and coordinator.data.schedules:
            for s in coordinator.data.schedules:
                if not getattr(s, "active", False):
                    continue
                durations = getattr(s, "zone_durations", []) or []
                for zone in enabled:
                    idx = zone - 1
                    if idx < len(durations) and durations[idx] > 0:
                        zk = str(zone)
                        irrigation[zk] = irrigation.get(zk, 0) + round(durations[idx] / 60)

        calendar_data: dict = await self._calendar_store.async_load() or {}
        entry_calendar: dict = calendar_data.get(entry_id, {})
        existing: dict = entry_calendar.get(date_str, {})

        # Accumulate into any irrigation already captured today (multiple cycles per day)
        prev = existing.get("irrigation", {})
        for zk, minutes in irrigation.items():
            prev[zk] = prev.get(zk, 0) + minutes
        existing["irrigation"] = prev
        existing.setdefault("source", "background")
        entry_calendar[date_str] = existing
        calendar_data[entry_id] = entry_calendar

        await self._calendar_store.async_save(calendar_data)

        # Keep in-memory totals in sync so the sensor can read without hitting storage
        entry_mem = self._today_irrigation.setdefault(entry_id, {})
        for zk, minutes in irrigation.items():
            entry_mem[zk] = entry_mem.get(zk, 0) + minutes

        async_dispatcher_send(self._hass, f"{DOMAIN}_irrigation_updated_{entry_id}")
        _LOGGER.debug(
            "RainVision: captured irrigation for %s on %s: %s", entry_id, date_str, irrigation
        )

    # ── End-of-day weather snapshot ───────────────────────────────────────────

    async def async_daily_weather_snapshot(self, _now=None) -> None:
        """At 23:58 save today's weather stats, preserving any irrigation already captured."""
        _LOGGER.debug("RainVision advisor: saving daily weather snapshot")
        calendar_data: dict = await self._calendar_store.async_load() or {}
        advisor_configs: dict = await self._advisor_store.async_load() or {}
        today = date.today()
        date_str = today.isoformat()
        changed = False

        for entry_id, obj in list(self._hass.data.get(DOMAIN, {}).items()):
            if not isinstance(obj, RainVisionCdcCoordinator):
                continue
            entry = self._hass.config_entries.async_get_entry(entry_id)
            if entry is None:
                continue

            cfg = advisor_configs.get(entry_id, {})
            try:
                weather = await self._fetch_weather_stats(cfg, today)
                if weather:
                    entry_calendar: dict = calendar_data.get(entry_id, {})
                    existing: dict = entry_calendar.get(date_str, {})

                    entry_calendar[date_str] = {
                        # Preserve irrigation data captured at irrigation time (if any)
                        "irrigation": existing.get("irrigation", {}),
                        "weather": weather,
                        "source": existing.get("source", "background"),
                    }
                    cutoff = (today - timedelta(days=_RETENTION_DAYS)).isoformat()
                    calendar_data[entry_id] = {
                        k: v for k, v in entry_calendar.items() if k >= cutoff
                    }
                    changed = True
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "RainVision daily weather snapshot error for %s: %s", entry_id, exc
                )

        if changed:
            await self._calendar_store.async_save(calendar_data)

    # ── Startup backfill ──────────────────────────────────────────────────────

    async def async_startup_backfill(self, _event=None) -> None:
        """Fill missing snapshots for the past BACKFILL_DAYS days (weather only).

        We cannot know what schedules were active on past days, so irrigation
        is intentionally omitted — showing nothing is more honest than showing
        the current (possibly different) schedule.
        """
        _LOGGER.debug("RainVision advisor: starting historical backfill")
        calendar_data: dict = await self._calendar_store.async_load() or {}
        advisor_configs: dict = await self._advisor_store.async_load() or {}
        changed = False
        today = date.today()

        for entry_id, obj in list(self._hass.data.get(DOMAIN, {}).items()):
            if not isinstance(obj, RainVisionCdcCoordinator):
                continue
            entry = self._hass.config_entries.async_get_entry(entry_id)
            if entry is None:
                continue

            cfg = advisor_configs.get(entry_id, {})
            entry_calendar: dict = calendar_data.get(entry_id, {})

            for offset in range(_BACKFILL_DAYS, 0, -1):
                target = today - timedelta(days=offset)
                date_str = target.isoformat()
                if date_str in entry_calendar:
                    continue  # already stored — don't overwrite
                try:
                    weather = await self._fetch_weather_stats(cfg, target)
                    if weather:
                        entry_calendar[date_str] = {
                            "weather": weather,
                            "irrigation": {},  # unknown for past days
                            "source": "backfill",
                        }
                        changed = True
                        _LOGGER.debug(
                            "RainVision backfill: saved %s for %s", date_str, entry_id
                        )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "RainVision backfill error for %s on %s: %s", entry_id, target, exc
                    )

            calendar_data[entry_id] = entry_calendar

        if changed:
            await self._calendar_store.async_save(calendar_data)
        _LOGGER.debug("RainVision advisor: backfill complete")

    # ── Shared weather-fetching logic ─────────────────────────────────────────

    async def _fetch_weather_stats(self, cfg: dict, target_date: date) -> dict | None:
        """Return raw weather stats for one day from the recorder, or None if unavailable."""
        we: dict = cfg.get("weather_entities", {})

        def _ids(key: str) -> list[str]:
            val = we.get(key, [])
            if isinstance(val, str):
                return [val] if val else []
            return [v for v in (val or []) if v]

        temp_ids = _ids("temperature")
        if not temp_ids:
            return None

        start_dt = dt_util.as_local(datetime.combine(target_date, time.min))
        end_dt   = dt_util.as_local(datetime.combine(target_date, time.max))

        raw: dict[str, dict] = {}
        for key, ids in [
            ("temperature",     temp_ids),
            ("humidity",        _ids("humidity")),
            ("solar_radiation", _ids("solar_radiation")),
            ("wind_speed",      _ids("wind_speed")),
            ("rain_today",      _ids("rain_today")),
        ]:
            if ids:
                stats = await self._fetch_day_stats(ids, start_dt, end_dt)
                if stats:
                    raw[key] = stats

        if not raw.get("temperature"):
            return None

        temp  = raw["temperature"]
        hum   = raw.get("humidity", {})
        solar = raw.get("solar_radiation", {})
        wind  = raw.get("wind_speed", {})
        rain  = raw.get("rain_today", {})

        return {
            "tMean":    temp.get("mean"),
            "tMin":     temp.get("min"),
            "tMax":     temp.get("max"),
            "humidity": hum.get("mean"),
            "solar":    solar.get("mean"),
            "wind":     wind.get("mean"),
            "rain":     rain.get("max"),  # max = end-of-day cumulative total
        }

    async def _fetch_day_stats(
        self,
        entity_ids: list[str],
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict | None:
        """Aggregate {mean, min, max} across multiple entity IDs for a time range."""
        means, mins, maxs = [], [], []
        for eid in entity_ids:
            s = await self._entity_stats(eid, start_dt, end_dt)
            if s:
                if s.get("mean") is not None:
                    means.append(s["mean"])
                if s.get("min") is not None:
                    mins.append(s["min"])
                if s.get("max") is not None:
                    maxs.append(s["max"])
        if not (means or mins or maxs):
            return None
        return {
            "mean": sum(means) / len(means) if means else None,
            "min":  min(mins) if mins else None,
            "max":  max(maxs) if maxs else None,
        }

    async def _entity_stats(
        self,
        entity_id: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict | None:
        """Fetch min/mean/max for one entity: long-term statistics → state history fallback."""
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
                statistics_during_period,
            )

            recorder = get_instance(self._hass)
            if recorder is not None:
                rows = await recorder.async_add_executor_job(
                    statistics_during_period,
                    self._hass,
                    start_dt,
                    end_dt,
                    {entity_id},
                    "day",
                    None,
                    {"mean", "min", "max"},
                )
                for row_list in rows.values():
                    for row in row_list:
                        if any(row.get(k) is not None for k in ("mean", "min", "max")):
                            return {
                                "mean": row.get("mean"),
                                "min":  row.get("min"),
                                "max":  row.get("max"),
                            }
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Statistics API unavailable for %s: %s", entity_id, exc)

        # Fallback: compute from raw state history
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.history import (  # noqa: PLC0415
                state_changes_during_period,
            )

            recorder = get_instance(self._hass)
            if recorder is None:
                return None

            states_by_id: dict = await recorder.async_add_executor_job(
                state_changes_during_period,
                self._hass,
                start_dt,
                end_dt,
                entity_id,
                True,  # no_attributes — saves memory
            )
            nums: list[float] = []
            for s in states_by_id.get(entity_id, []):
                try:
                    nums.append(float(s.state))
                except (ValueError, TypeError):
                    pass
            if not nums:
                return None
            return {"mean": sum(nums) / len(nums), "min": min(nums), "max": max(nums)}
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("State history unavailable for %s: %s", entity_id, exc)

        return None
