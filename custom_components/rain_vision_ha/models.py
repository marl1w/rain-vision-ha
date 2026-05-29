"""Data models for rain-vision-ha."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class RainVisionAdvertisement:
    """Decoded Rain Vision advertisement."""

    address: str
    name: str | None
    rssi: int | None
    company: str | None
    type_id: int | None
    product_id: int | None
    battery: int | None


@dataclass(slots=True)
class RainVisionStatus:
    """Decoded CDC status characteristic."""

    flags: dict[str, int] = field(default_factory=dict)
    zone_state: int = 0
    pump_active: bool = False
    fert_active: bool = False
    rain_sensor_state: int = 0
    fsm_state: int = 0
    number_of_icycles: int = 0
    current_zone_lasting_time: int = 0
    number_of_acqua_sensors: int = 0
    zone_open_faults: list[int] = field(default_factory=list)
    zone_shorted_faults: list[int] = field(default_factory=list)
    active_zones: list[int] = field(default_factory=list)


@dataclass(slots=True)
class RainVisionSchedule:
    """Decoded firmware 5.0 iCycle record."""

    cycle_id: int
    program_name: str
    zone: int
    active: bool
    start_time: str
    schedule_type: str
    cycle_hours: int | None
    even_mode: int | None
    calendar: datetime | None
    weekdays: list[int]
    duration: int
    zone_durations: list[int]
    raw: bytes


@dataclass(slots=True)
class RainVisionData:
    """Coordinator data."""

    name: str
    address: str
    product_id: int | None = None
    firmware: str | None = None
    hardware: str | None = None
    battery: int | None = None
    rssi: int | None = None
    status: RainVisionStatus | None = None
    schedules: list[RainVisionSchedule] = field(default_factory=list)
    next_irrigation: datetime | None = None
    update_error: str | None = None
    manual_totals: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0, 0])
    weekly_zone_totals: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0, 0])

    @property
    def active_schedule_count(self) -> int:
        """Return active iCycle count."""

        return sum(
            1
            for schedule in self.schedules
            if schedule.active and any(duration > 0 for duration in schedule.zone_durations)
        )

    @property
    def status_name(self) -> str:
        """Return a friendly status name."""

        if self.status is None:
            return "unknown"
        if self.status.fsm_state == 1:
            return "manual_watering"
        if self.status.fsm_state == 2 and self.status.zone_state > 0:
            return "scheduled_watering"
        if self.status.fsm_state == 2:
            return "idle"
        if self.status.fsm_state == 0:
            return "ready"
        return f"state_{self.status.fsm_state}"

    @property
    def is_watering(self) -> bool:
        """Return true if any zone/pump is active."""

        if self.status is None:
            return False
        return (
            self.status.zone_state > 0
            or self.status.pump_active
            or any(self.status.active_zones[:6])
        )

    def zone_remaining(self, zone: int) -> int:
        """Return remaining/current time for a zone."""

        if self.status is None or self.status.zone_state != zone:
            return 0
        return self.status.current_zone_lasting_time

    def zone_configured_total(self, zone: int) -> int:
        """Return configured total seconds for a zone from manual or schedule data."""

        try:
            manual_total = self.manual_totals[zone - 1]
        except IndexError:
            manual_total = 0

        scheduled_totals = [
            schedule.zone_durations[zone - 1]
            for schedule in self.schedules
            if schedule.active
            and len(schedule.zone_durations) >= zone
            and schedule.zone_durations[zone - 1] > 0
        ]

        return manual_total if manual_total > 0 else (max(scheduled_totals) if scheduled_totals else 0)

    def zone_elapsed(self, zone: int) -> int:
        """Return elapsed watering seconds for the currently active zone."""

        if self.status is None or self.status.zone_state != zone:
            return 0

        configured_total = self.zone_configured_total(zone)
        if configured_total <= 0:
            return 0

        remaining = max(0, self.status.current_zone_lasting_time)
        if remaining <= configured_total:
            return configured_total - remaining
        return configured_total

    def zone_weekly_total(self, zone: int) -> int:
        """Return accumulated weekly watering seconds for the zone."""

        try:
            return max(0, self.weekly_zone_totals[zone - 1])
        except IndexError:
            return 0

    @property
    def weekly_total(self) -> int:
        """Return accumulated weekly watering seconds across all zones."""

        return sum(max(0, seconds) for seconds in self.weekly_zone_totals)

    def as_diagnostics(self) -> dict[str, Any]:
        """Return a simple diagnostic payload."""

        return {
            "address": self.address,
            "product_id": self.product_id,
            "firmware": self.firmware,
            "hardware": self.hardware,
            "battery": self.battery,
            "rssi": self.rssi,
            "status": asdict(self.status) if self.status else None,
            "schedule_count": len(self.schedules),
            "next_irrigation": self.next_irrigation.isoformat() if self.next_irrigation else None,
            "update_error": self.update_error,
            "weekly_zone_totals": self.weekly_zone_totals,
            "weekly_total": self.weekly_total,
        }
