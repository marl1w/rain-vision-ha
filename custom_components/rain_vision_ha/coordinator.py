"""Coordinator for rain-vision-ha."""

from __future__ import annotations

from datetime import datetime, timedelta
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
from .protocol import RainVisionCdcClient, RainVisionError

_LOGGER = logging.getLogger(__name__)

CONNECTION_STATE_LABELS: dict[str, str] = {
    "idle": "Waiting for next BLE poll",
    "advertisement": "BLE advertisement detected",
    "connecting": "Connecting to device",
    "connected": "Connected",
    "reading": "Reading from device",
    "writing": "Writing to device",
    "error": "Connection failed",
    "not_discoverable": "Device not discoverable",
}


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
        storage_key: str,
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
        self.weekly_zone_totals = [0, 0, 0, 0, 0, 0]
        self._weekly_bucket_start: str | None = None
        self._last_active_zone: int | None = None
        self._last_active_elapsed: int = 0
        self._store: Store[dict[str, object]] = Store(hass, 1, storage_key)
        self.enabled_zones: set[int] = set(range(1, CDC_ZONES + 1))
        self._has_polled = False
        self._poll_in_progress = False
        self._last_poll_attempt: datetime | None = None
        self._last_poll_completed: datetime | None = None
        self._manual_start_requested = False
        self.connection_state = "idle"
        self.connection_error: str | None = None
        self._last_advertisement: datetime | None = None
        self._last_connecting: datetime | None = None
        self._last_connected: datetime | None = None
        self._last_reading: datetime | None = None
        self._last_writing: datetime | None = None
        self._last_error: datetime | None = None
        self._last_not_discoverable: datetime | None = None

    async def async_initialize(self) -> None:
        """Load persisted weekly totals and align them with the current week."""

        payload = await self._store.async_load()
        current_week = self._week_start_key(datetime.now().astimezone())
        if isinstance(payload, dict) and payload.get("week_start") == current_week:
            zone_totals = payload.get("zone_totals")
            if isinstance(zone_totals, list):
                self.weekly_zone_totals = [
                    max(0, int(seconds))
                    for seconds in (zone_totals + [0] * CDC_ZONES)[:CDC_ZONES]
                ]
                self._weekly_bucket_start = current_week
                return

        self.weekly_zone_totals = [0] * CDC_ZONES
        self._weekly_bucket_start = current_week
        self._schedule_weekly_save()

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
        self._adjust_polling(now)
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
        self._poll_in_progress = False
        self.manual_totals = self.mask_disabled_zones(data.manual_totals)
        data.manual_totals = self.manual_totals
        self._update_weekly_totals(data, now)
        data.weekly_zone_totals = self.weekly_zone_totals.copy()
        self._adjust_polling(now, data)
        return data

    async def async_start_manual_irrigation(self, durations: list[int]) -> None:
        """Start manual irrigation and refresh."""

        self.manual_totals = self.mask_disabled_zones(durations)
        try:
            await self.client.start_manual_irrigation(self.manual_totals)
        except RainVisionError as err:
            self._set_connection_state("error", str(err))
            raise
        except Exception as err:
            self._set_connection_state("error", f"Manual irrigation failed: {err}")
            raise
        self._manual_start_requested = True
        self.update_interval = FAST_SCAN_INTERVAL
        await self.async_request_refresh()

    async def async_stop_irrigation(self) -> None:
        """Stop irrigation and refresh."""

        try:
            await self.client.stop_irrigation()
        except RainVisionError as err:
            self._set_connection_state("error", str(err))
            raise
        except Exception as err:
            self._set_connection_state("error", f"Stop irrigation failed: {err}")
            raise
        self.manual_totals = [0, 0, 0, 0, 0, 0]
        self._manual_start_requested = False
        await self.async_request_refresh()

    @property
    def polling_mode(self) -> str:
        """Return current polling mode for diagnostics sensors."""

        if not self._has_polled:
            return "none"
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
    def week_start(self) -> str | None:
        """Return current local week bucket start date (YYYY-MM-DD)."""

        return self._weekly_bucket_start

    @property
    def connection_state_label(self) -> str:
        """Return a friendly label for the current connection state."""

        return CONNECTION_STATE_LABELS.get(self.connection_state, self.connection_state)

    @property
    def connection_state_attributes(self) -> dict[str, str | None]:
        """Return connection lifecycle timestamps and error details."""

        return {
            "raw_state": self.connection_state,
            "last_advertisement": self._last_advertisement.isoformat() if self._last_advertisement else None,
            "last_connecting": self._last_connecting.isoformat() if self._last_connecting else None,
            "last_connected": self._last_connected.isoformat() if self._last_connected else None,
            "last_reading": self._last_reading.isoformat() if self._last_reading else None,
            "last_writing": self._last_writing.isoformat() if self._last_writing else None,
            "last_error": self._last_error.isoformat() if self._last_error else None,
            "last_not_discoverable": self._last_not_discoverable.isoformat() if self._last_not_discoverable else None,
            "error": self.connection_error,
        }

    def _handle_client_status(self, state: str, message: str | None = None) -> None:
        """Handle BLE lifecycle events emitted by the protocol client."""

        self._set_connection_state(state, message)

    def _set_connection_state(self, state: str, message: str | None = None) -> None:
        """Update connection state and remember key transition timestamps."""

        now = datetime.now().astimezone()
        if state == "advertisement":
            self._last_advertisement = now
        elif state == "connecting":
            self._last_connecting = now
        elif state == "connected":
            self._last_connected = now
        elif state == "reading":
            self._last_reading = now
        elif state == "writing":
            self._last_writing = now
        elif state == "error":
            self._last_error = now
            self.connection_error = message
        elif state == "not_discoverable":
            self._last_not_discoverable = now

        if state != "error":
            self.connection_error = None

        self.connection_state = state
        self.async_update_listeners()

    def _adjust_polling(self, now: datetime, data: RainVisionData | None = None) -> None:
        """Switch between regular and short-term faster polling."""

        if data is None:
            return

        if data.is_watering:
            # Keep a tight loop while irrigation is running, including manual starts detected during polling.
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

    def _week_start_key(self, now: datetime) -> str:
        """Return local week key (Monday date) for weekly accumulation."""

        week_start = now.date().toordinal() - now.weekday()
        return datetime.fromordinal(week_start).date().isoformat()

    def _serialize_weekly_state(self) -> dict[str, object]:
        """Return persisted weekly state payload."""

        return {
            "week_start": self._weekly_bucket_start,
            "zone_totals": self.weekly_zone_totals,
        }

    def _schedule_weekly_save(self) -> None:
        """Debounce weekly totals persistence to Home Assistant storage."""

        self._store.async_delay_save(self._serialize_weekly_state, 5)

    def _update_weekly_totals(self, data: RainVisionData, now: datetime) -> None:
        """Accumulate per-zone watering runtime in the current local week."""

        current_week = self._week_start_key(now)
        if self._weekly_bucket_start != current_week:
            self._weekly_bucket_start = current_week
            self.weekly_zone_totals = [0] * CDC_ZONES
            self._last_active_zone = None
            self._last_active_elapsed = 0
            self._schedule_weekly_save()

        active_zone = data.status.zone_state if data.status and data.status.zone_state > 0 else 0
        if active_zone <= 0:
            self._last_active_zone = None
            self._last_active_elapsed = 0
            return

        active_elapsed = max(0, data.zone_elapsed(active_zone))
        increment = 0
        if self._last_active_zone == active_zone:
            if active_elapsed >= self._last_active_elapsed:
                increment = active_elapsed - self._last_active_elapsed
            else:
                increment = active_elapsed
        else:
            # Include watering already in progress when HA reconnects mid-cycle.
            increment = active_elapsed

        if increment > 0 and active_zone <= len(self.weekly_zone_totals):
            self.weekly_zone_totals[active_zone - 1] += increment
            self._schedule_weekly_save()

        self._last_active_zone = active_zone
        self._last_active_elapsed = active_elapsed
