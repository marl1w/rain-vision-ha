"""Coordinator for rain-vision-ha."""

from __future__ import annotations

from datetime import datetime, time, timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CDC_ZONES,
    DEFAULT_SCAN_INTERVAL,
    FAST_SCAN_INTERVAL,
    SCHEDULE_NEAR_WINDOW,
)
from .models import RainVisionData
from .protocol import RainVisionCdcClient, RainVisionError, set_icycle_active

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1



def _parse_window_time(time_str: str | None) -> time | None:
    """Parse HH:MM string into a time object. Returns None if invalid or empty."""

    if not time_str:
        return None
    try:
        parts = time_str.split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return time(h, m)
    except (ValueError, AttributeError):
        pass
    return None


def _schedule_driven_interval(now: datetime, next_irrigation: datetime) -> tuple[bool, float]:
    """Return whether fast polling is needed and suggested interval seconds.

    The suggested interval is chosen so the next refresh happens when the
    irrigation enters the near-schedule window.
    """

    delta_seconds = (next_irrigation - now).total_seconds()
    near_window_seconds = SCHEDULE_NEAR_WINDOW.total_seconds()
    default_seconds = DEFAULT_SCAN_INTERVAL.total_seconds()
    fast_seconds = FAST_SCAN_INTERVAL.total_seconds()

    if delta_seconds <= near_window_seconds:
        return True, fast_seconds

    until_near_window_seconds = delta_seconds - near_window_seconds
    suggested_seconds = min(default_seconds, max(fast_seconds, until_near_window_seconds))
    return False, suggested_seconds


class RainVisionCdcCoordinator(DataUpdateCoordinator[RainVisionData]):
    """Coordinate short-lived BLE refreshes."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: RainVisionCdcClient,
        entry_id: str | None = None,
    ) -> None:
        """Initialize the coordinator."""

        super().__init__(
            hass,
            _LOGGER,
            name="rain-vision-ha",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self.client.set_status_callback(self._handle_client_status)
        self.manual_totals = [0, 0, 0, 0, 0, 0]
        self.enabled_zones: set[int] = set(range(1, CDC_ZONES + 1))
        self._has_polled = False
        self._poll_in_progress = False
        self._last_poll_attempt: datetime | None = None
        self._last_poll_completed: datetime | None = None
        self._manual_start_requested = False
        self.connection_state = "idle"
        self.connection_error: str | None = None
        self._write_in_progress: bool = False
        self._last_poll_completed_iso: str | None = None
        self._last_write: datetime | None = None
        self._last_error: datetime | None = None
        self._poll_window_start: time | None = None
        self._poll_window_end: time | None = None
        storage_key = f"rain_vision_ha.{entry_id or client.address}"
        self._store: Store = Store(hass, _STORAGE_VERSION, storage_key)
        self._irrigation_paused: bool = False
        self._active_program_times: dict[str, set[str]] = {}

    async def async_setup(self) -> None:
        """Load persisted pause state from storage."""

        data = await self._store.async_load()
        if data is None:
            return
        self._irrigation_paused = data.get("paused", False)
        self._active_program_times = {
            program: set(times)
            for program, times in data.get("active_program_times", {}).items()
        }

    @property
    def irrigation_paused(self) -> bool:
        """Return True when all schedules have been disabled on the device."""

        return self._irrigation_paused

    async def async_pause_schedules(self) -> None:
        """Read fresh schedules from device, deactivate all active ones, write back.

        Stores only the cycle_ids that were active so resume can restore them
        without touching any other schedule configuration.
        """

        self._write_in_progress = True
        self.async_update_listeners()
        try:
            schedules = await self.client.read_icycles()
            if not schedules:
                _LOGGER.warning("No schedules found on device for %s", self.client.address)
                return
            active_program_times: dict[str, set[str]] = {}
            for s in schedules:
                if s.active and s.start_time != "00:00" and any(d > 0 for d in s.zone_durations):
                    active_program_times.setdefault(s.program_name, set()).add(s.start_time)
            self._active_program_times = active_program_times
            self._irrigation_paused = True
            await self._store.async_save({"paused": True, "active_program_times": {p: list(t) for p, t in active_program_times.items()}})
            modified = [
                set_icycle_active(s.raw, False)
                if (s.active and s.start_time != "00:00" and any(d > 0 for d in s.zone_durations))
                else s.raw
                for s in schedules
            ]
            await self.client.write_icycles(modified)
        except Exception:
            self._irrigation_paused = False
            raise
        finally:
            self._write_in_progress = False
            self._last_write = datetime.now().astimezone()
            self.async_update_listeners()
        await self.async_request_refresh()

    async def async_resume_schedules(self) -> None:
        """Read fresh schedules from device, re-enable stored cycle_ids, write back.

        Only the active flag is modified; zone durations and all other config
        come from the device as-is.
        """

        if not self._active_program_times:
            _LOGGER.warning("No stored active program times to restore for %s", self.client.address)
            self._irrigation_paused = False
            return
        self._write_in_progress = True
        self.async_update_listeners()
        try:
            schedules = await self.client.read_icycles()
            modified = [
                set_icycle_active(s.raw, True)
                if (s.program_name in self._active_program_times and s.start_time in self._active_program_times[s.program_name])
                else s.raw
                for s in schedules
            ]
            await self.client.write_icycles(modified)
        except Exception:
            raise
        finally:
            self._write_in_progress = False
            self._last_write = datetime.now().astimezone()
            self.async_update_listeners()
        self._irrigation_paused = False
        await self._store.async_save({"paused": False, "active_program_times": {p: list(t) for p, t in self._active_program_times.items()}})
        await self.async_request_refresh()

    async def _mirror_schedules(self, schedules: list) -> None:
        """Keep the active cycle_id set in sync after each successful poll."""

        active_program_times: dict[str, set[str]] = {}
        for s in schedules:
            if s.active and s.start_time != "00:00" and any(d > 0 for d in s.zone_durations):
                active_program_times.setdefault(s.program_name, set()).add(s.start_time)
        self._active_program_times = active_program_times
        await self._store.async_save({"paused": False, "active_program_times": {p: list(t) for p, t in active_program_times.items()}})

    def set_poll_window(self, start: str | None, end: str | None) -> None:
        """Configure the polling time window. Both empty/None means poll 24/7."""

        self._poll_window_start = _parse_window_time(start)
        self._poll_window_end = _parse_window_time(end)

    def _is_in_polling_window(self, now: datetime) -> bool:
        """Return True if now is within the configured polling window, or no window is set."""

        start = self._poll_window_start
        end = self._poll_window_end
        if start is None or end is None or start == end:
            return True
        current = now.time().replace(second=0, microsecond=0)
        if start < end:
            return start <= current < end
        # Overnight window (e.g. 22:00–06:00)
        return current >= start or current < end

    def _seconds_until_window_start(self, now: datetime) -> float:
        """Return seconds from now until the poll window opens."""

        start = self._poll_window_start
        if start is None:
            return DEFAULT_SCAN_INTERVAL.total_seconds()
        candidate = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return (candidate - now).total_seconds()

    def mask_disabled_zones(self, durations: list[int]) -> list[int]:
        """Zero-out durations for zones that are disabled."""

        normalized = (durations + [0] * CDC_ZONES)[:CDC_ZONES]
        return [
            seconds if (index + 1) in self.enabled_zones else 0
            for index, seconds in enumerate(normalized)
        ]

    async def _async_update_data(self) -> RainVisionData:
        """Fetch data from the device."""

        now = datetime.now().astimezone()
        self._has_polled = True
        self._poll_in_progress = True
        self._last_poll_attempt = now

        if not self._is_in_polling_window(now):
            currently_watering = self.data is not None and self.data.is_watering
            # Allow a first poll even outside the window so entities can initialize
            # after startup/options reload instead of staying unknown until window open.
            if not currently_watering and self.data is not None:
                self._poll_in_progress = False
                secs = self._seconds_until_window_start(now)
                self.update_interval = timedelta(seconds=secs)
                self._set_connection_state("outside_window")
                _LOGGER.debug(
                    "Skipping BLE poll for %s: outside polling window, resuming in %.0fs",
                    self.client.address,
                    secs,
                )
                return self.data
            if not currently_watering:
                _LOGGER.debug(
                    "Outside polling window for %s but allowing initial refresh (no cached data)",
                    self.client.address,
                )

        try:
            data = await self.client.async_update(self.manual_totals)
        except RainVisionError as err:
            self._set_connection_state("error", str(err))
            self._last_poll_completed = datetime.now().astimezone()
            self._poll_in_progress = False
            self._schedule_retry(self._last_poll_completed)
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # noqa: BLE failures are backend-specific.
            self._set_connection_state("error", f"Bluetooth update failed: {err}")
            _LOGGER.exception("Bluetooth update failed for %s", self.client.address)
            self._last_poll_completed = datetime.now().astimezone()
            self._poll_in_progress = False
            self._schedule_retry(self._last_poll_completed)
            if self.data is not None:
                _LOGGER.warning(
                    "Keeping last known data for %s after a transient Bluetooth failure",
                    self.client.address,
                )
                return self.data
            raise UpdateFailed(f"Bluetooth update failed: {err}") from err

        self._last_poll_completed = datetime.now().astimezone()
        self._last_poll_completed_iso = self._last_poll_completed.isoformat()
        self._poll_in_progress = False
        self.manual_totals = self.mask_disabled_zones(data.manual_totals)
        data.manual_totals = self.manual_totals
        if data.schedules:
            any_active = any(
                s.active and s.start_time != "00:00" and any(d > 0 for d in s.zone_durations)
                for s in data.schedules
            )
            self._irrigation_paused = not any_active
            if not self._irrigation_paused:
                await self._mirror_schedules(data.schedules)
            else:
                await self._store.async_save({"paused": True, "active_program_times": {p: list(t) for p, t in self._active_program_times.items()}})
        self._adjust_polling(now, data)
        return data

    async def async_start_manual_irrigation(self, durations: list[int]) -> None:
        """Start manual irrigation and refresh."""

        self.manual_totals = self.mask_disabled_zones(durations)
        self._write_in_progress = True
        self.async_update_listeners()
        try:
            await self.client.start_manual_irrigation(self.manual_totals)
        except RainVisionError as err:
            self._set_connection_state("error", str(err))
            raise
        except Exception as err:
            self._set_connection_state("error", f"Manual irrigation failed: {err}")
            raise
        finally:
            self._write_in_progress = False
            self._last_write = datetime.now().astimezone()
            self.async_update_listeners()
        self._manual_start_requested = True
        self.update_interval = FAST_SCAN_INTERVAL
        await self.async_request_refresh()

    async def async_stop_irrigation(self) -> None:
        """Stop irrigation and refresh."""

        self._write_in_progress = True
        self.async_update_listeners()
        try:
            await self.client.stop_irrigation()
        except RainVisionError as err:
            self._set_connection_state("error", str(err))
            raise
        except Exception as err:
            self._set_connection_state("error", f"Stop irrigation failed: {err}")
            raise
        finally:
            self._write_in_progress = False
            self._last_write = datetime.now().astimezone()
            self.async_update_listeners()
        self.manual_totals = [0, 0, 0, 0, 0, 0]
        self._manual_start_requested = False
        await self.async_request_refresh()

    @property
    def polling_mode(self) -> str:
        """Return current polling mode for diagnostics sensors."""

        if not self._has_polled:
            return "none"
        if self.connection_state == "outside_window":
            return "paused"
        if self.update_interval == FAST_SCAN_INTERVAL:
            return "fast"
        return "normal"

    @property
    def next_device_refresh(self) -> datetime | None:
        """Return the expected timestamp of the next scheduled device refresh.

        Returns None while a BLE poll is actively running so the sensor clearly
        shows the gap between the countdown hitting zero and the result arriving.
        """

        if self._poll_in_progress:
            return None
        completed = self._last_poll_completed
        if completed is None or self.update_interval is None:
            return None
        return completed + self.update_interval

    @property
    def connection_state_label(self) -> str:
        """Return one of four user-facing connection states."""

        if self.connection_state == "error":
            return "Error"
        if self._write_in_progress:
            return "Writing"
        if self._poll_in_progress:
            return "Polling"
        return "Waiting for next BLE poll"

    @property
    def connection_state_attributes(self) -> dict[str, str | None]:
        """Return a minimal set of connection diagnostics."""

        return {
            "last_poll": self._last_poll_completed_iso,
            "last_write": self._last_write.isoformat() if self._last_write else None,
            "last_error": self._last_error.isoformat() if self._last_error else None,
            "error": self.connection_error,
        }

    def _handle_client_status(self, state: str, message: str | None = None) -> None:
        """Handle BLE lifecycle events emitted by the protocol client."""

        self._set_connection_state(state, message)

    def _set_connection_state(self, state: str, message: str | None = None) -> None:
        """Update raw connection state; only error transitions are tracked."""

        if state == "error":
            self._last_error = datetime.now().astimezone()
            self.connection_error = message
        else:
            self.connection_error = None

        self.connection_state = state
        self.async_update_listeners()

    def _adjust_polling(self, now: datetime, data: RainVisionData | None = None) -> None:
        """Switch between regular and short-term faster polling."""

        if data is None:
            return

        if data.is_watering:
            # Keep a tight loop while irrigation is running.
            self._manual_start_requested = False
            self.update_interval = FAST_SCAN_INTERVAL
            return

        if self._manual_start_requested:
            self.update_interval = FAST_SCAN_INTERVAL
            return

        if data.next_irrigation:
            should_fast_poll, interval_seconds = _schedule_driven_interval(now, data.next_irrigation)
            if should_fast_poll:
                self.update_interval = FAST_SCAN_INTERVAL
                return

            self.update_interval = timedelta(seconds=interval_seconds)
            return

        self.update_interval = DEFAULT_SCAN_INTERVAL

    def _schedule_retry(self, now: datetime) -> None:
        """Recompute polling safely after a failed refresh."""

        self._manual_start_requested = False
        if self.data is not None:
            self._adjust_polling(now, self.data)
            return

        self.update_interval = DEFAULT_SCAN_INTERVAL
