"""rain-vision-ha BLE protocol helpers.

The byte offsets and commands in this file mirror the decompiled Android app for
CONNECT DC BATTERY devices running the one-by-one firmware introduced in 5.0.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime, timedelta
import logging
import time
from typing import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import (
    BATTERY_LEVEL_UUID,
    CDC_6_ZONE_TYPE_ID,
    CDC_SUPPORTED_FIRMWARE,
    CDC_ZONES,
    COMMAND_RESET_ICYCLE_POINTER,
    COMMAND_START_MANUAL_MODE,
    COMMAND_START_NORMAL_MODE,
    COMMAND_START_READY_MODE,
    COMMAND_STOP_CURRENT_CYCLE,
    COMMAND_UUID,
    FIRMWARE_UUID,
    HARDWARE_UUID,
    ICYCLE_UUID,
    MANUAL_UUID,
    PASSWORD_UUID,
    STATUS_UUID,
    TIME_UUID,
)
from .models import RainVisionAdvertisement, RainVisionData, RainVisionSchedule, RainVisionStatus

_LOGGER = logging.getLogger(__name__)

STATUS_FLAG_BITS = (
    "default_password",
    "generic_fw_error",
    "generic_hw_error",
    "ongoing_charging",
)


class RainVisionError(Exception):
    """Base Rain Vision error."""


class UnsupportedDeviceError(RainVisionError):
    """Raised when the connected device is not a CDC 6 firmware 5.0 device."""


def _uint_be(data: bytes | bytearray | memoryview) -> int:
    """Return integer for fields the app decodes with reverse()+little-endian."""

    return int.from_bytes(bytes(data), "big")


def _bits_lsb(value: int, width: int) -> list[int]:
    """Return least-significant-bit-first bits."""

    return [(value >> bit) & 1 for bit in range(width)]


def _hex(data: bytes | bytearray | memoryview) -> str:
    """Format bytes for debug logs."""

    return bytes(data).hex(" ")


def _is_missing_characteristic_error(err: Exception, uuid: str) -> bool:
    """Return true when BLE backend reports a missing characteristic UUID."""

    message = str(err).lower()
    return uuid.lower() in message and "not found" in message and "characteristic" in message


def decode_advertisement(service_info: bluetooth.BluetoothServiceInfoBleak) -> RainVisionAdvertisement:
    """Decode the manufacturer/service data exactly as the app does."""

    company = None
    type_id = None
    product_id = None

    for manufacturer_id, payload in service_info.manufacturer_data.items():
        data = bytes(payload)
        if len(data) >= 8:
            company = f"{manufacturer_id:04X}"
            type_id = (data[0] << 8) | data[1] if manufacturer_id != 0xFFFF else (data[2] << 8) | data[3]
            if manufacturer_id == 0xFFFF:
                product_id = int.from_bytes(data[4:8], "big")
            else:
                product_id = int.from_bytes(data[2:6], "big")
            if type_id not in {CDC_6_ZONE_TYPE_ID, None}:
                type_id = (data[2] << 8) | data[3]
                product_id = int.from_bytes(data[4:8], "big")
            break

    battery = None
    for uuid, payload in service_info.service_data.items():
        if uuid.lower() in {"0000180f-0000-1000-8000-00805f9b34fb", "180f"} and payload:
            battery = payload[-1]
            break

    advertisement = RainVisionAdvertisement(
        address=service_info.address,
        name=service_info.name,
        rssi=service_info.rssi,
        company=company,
        type_id=type_id,
        product_id=product_id,
        battery=battery,
    )
    _LOGGER.debug(
        "Decoded advertisement address=%s name=%s rssi=%s company=%s type_id=%s product_id=%s battery=%s",
        advertisement.address,
        advertisement.name,
        advertisement.rssi,
        advertisement.company,
        advertisement.type_id,
        advertisement.product_id,
        advertisement.battery,
    )
    return advertisement


def is_cdc_advertisement(service_info: bluetooth.BluetoothServiceInfoBleak) -> bool:
    """Return true when an advertisement is likely the supported CDC device."""

    adv = decode_advertisement(service_info)
    if adv.type_id == CDC_6_ZONE_TYPE_ID:
        return True
    name = (adv.name or "").upper()
    return name.startswith("CDC") or name.startswith("PLD")


def decode_status(data: bytes) -> RainVisionStatus:
    """Decode the irrigation STATUS characteristic."""

    payload = bytes(data)
    if len(payload) < 21:
        raise RainVisionError(f"Status payload too short: {len(payload)} bytes")

    status_bits = _bits_lsb(_uint_be(payload[0:2]), 16)
    flags = {
        name: status_bits[index]
        for index, name in enumerate(STATUS_FLAG_BITS)
        if index < len(status_bits)
    }

    active_bits = _bits_lsb(_uint_be(payload[2:6]), 32)
    zone_state = next((index + 1 for index, active in enumerate(active_bits) if active), 0)
    pump_active = False
    fert_active = False

    if sum(active_bits[:16]) > 1:
        pump_active = True
        zone_state = next((index + 1 for index, active in enumerate(active_bits[1:16], 1) if active), 0)

    return RainVisionStatus(
        flags=flags,
        zone_state=zone_state,
        pump_active=pump_active,
        fert_active=fert_active,
        rain_sensor_state=_uint_be(payload[6:7]),
        fsm_state=_uint_be(payload[7:8]),
        number_of_icycles=_uint_be(payload[8:10]),
        current_zone_lasting_time=_uint_be(payload[10:12]),
        number_of_acqua_sensors=_uint_be(payload[12:13]),
        zone_open_faults=_bits_lsb(_uint_be(payload[13:17]), 32),
        zone_shorted_faults=_bits_lsb(_uint_be(payload[17:21]), 32),
        active_zones=active_bits,
    )


def decode_icycle(data: bytes, zones: int = CDC_ZONES) -> RainVisionSchedule:
    """Decode a firmware 5.0 one-by-one iCycle record."""

    payload = bytes(data)
    if len(payload) < 10 + zones * 2:
        raise RainVisionError(f"iCycle payload too short: {len(payload)} bytes")

    info_bits = f"{payload[0]:08b}{payload[1]:08b}"[1:]
    cycle_id = int(info_bits[0:6], 2)
    program_name = chr(int(info_bits[7:11], 2) + 65)
    program_type_binary = info_bits[11:14]
    active = info_bits[14] == "1"

    schedule_type = {
        "000": "cycle",
        "001": "weekdays",
        "010": "even",
        "011": "even",
        "100": "even",
        "101": "calendar",
    }.get(program_type_binary, "unknown")

    start_hour = payload[2]
    start_minute = payload[3]
    start_time = f"{start_hour:02d}:{start_minute:02d}"

    year_mask = payload[4]
    month_mask = payload[5]
    day_mask = payload[6]

    weekday_disable_bits = _bits_lsb(payload[7], 8)
    weekdays = [index + 1 for index, disabled in enumerate(weekday_disable_bits[:7]) if disabled == 0]

    day_rep_mask = payload[8]
    hour_rep_mask = payload[9]
    if hour_rep_mask < 255:
        cycle_hours = hour_rep_mask + 1
        even_mode = None
    elif day_rep_mask >= 253:
        cycle_hours = None
        even_mode = day_rep_mask
    else:
        cycle_hours = (day_rep_mask + 1) * 24
        even_mode = None

    calendar = None
    if year_mask + month_mask + day_mask:
        with suppress(ValueError):
            calendar = datetime(2020 + year_mask - 1, month_mask, day_mask, start_hour, start_minute)

    zone_durations = [
        _uint_be(payload[10 + index * 2 : 12 + index * 2])
        for index in range(zones)
    ]
    zone_index = min(cycle_id // 4, zones - 1)
    active_zone_index = next(
        (index for index, duration in enumerate(zone_durations) if duration > 0),
        zone_index,
    )
    zone = active_zone_index + 1
    duration = zone_durations[active_zone_index]

    return RainVisionSchedule(
        cycle_id=cycle_id,
        program_name=program_name,
        zone=zone,
        active=active,
        start_time=start_time,
        schedule_type=schedule_type,
        cycle_hours=cycle_hours,
        even_mode=even_mode,
        calendar=calendar,
        weekdays=weekdays,
        duration=duration,
        zone_durations=zone_durations,
        raw=payload,
    )


def decode_manual_durations(data: bytes, zones: int = CDC_ZONES) -> list[int]:
    """Decode MANUAL characteristic into per-zone seconds."""

    payload = bytes(data)
    expected_len = zones * 2
    if len(payload) < expected_len:
        raise RainVisionError(f"Manual payload too short: {len(payload)} bytes")

    return [
        _uint_be(payload[index * 2 : index * 2 + 2])
        for index in range(zones)
    ]


def next_run(schedule: RainVisionSchedule, now: datetime) -> datetime | None:
    """Calculate the next run for a decoded schedule."""

    if not schedule.active or not any(duration > 0 for duration in schedule.zone_durations):
        return None

    if schedule.schedule_type == "unknown":
        return None

    if schedule.calendar is not None:
        candidate = schedule.calendar.replace(tzinfo=now.tzinfo)
        return candidate if candidate >= now else None

    try:
        hour, minute = (int(part) for part in schedule.start_time.split(":", 1))
    except ValueError:
        return None

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None

    candidates: list[datetime] = []
    start_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if schedule.schedule_type == "weekdays":
        for day_offset in range(14):
            candidate = start_today + timedelta(days=day_offset)
            app_weekday = 1 if candidate.weekday() == 6 else candidate.weekday() + 2
            if app_weekday in schedule.weekdays and candidate >= now:
                candidates.append(candidate)
                break
    elif schedule.even_mode is not None:
        for day_offset in range(32):
            candidate = start_today + timedelta(days=day_offset)
            if candidate < now:
                continue
            day = candidate.day
            if schedule.even_mode == 253 and day % 2 == 0:
                candidates.append(candidate)
                break
            if schedule.even_mode == 254 and day % 2 == 1:
                candidates.append(candidate)
                break
            if schedule.even_mode == 255 and day % 2 == 1 and day != 31:
                candidates.append(candidate)
                break
    elif schedule.cycle_hours:
        interval = timedelta(hours=schedule.cycle_hours)
        candidate = start_today
        while candidate < now:
            candidate += interval
        if candidate < now + timedelta(days=32):
            candidates.append(candidate)
    else:
        candidate = start_today if start_today >= now else start_today + timedelta(days=1)
        candidates.append(candidate)

    return min(candidates) if candidates else None


class RainVisionCdcClient:
    """Short-lived BLE client for a Rain Vision CDC controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        password: str,
        name: str | None = None,
        product_id: int | None = None,
        status_callback: Callable[[str, str | None], None] | None = None,
    ) -> None:
        """Initialize the client."""

        self.hass = hass
        self.address = address
        self.password = password
        self.name = name or address
        self.product_id = product_id
        self._status_callback = status_callback

    def set_status_callback(
        self,
        callback: Callable[[str, str | None], None] | None,
    ) -> None:
        """Set a callback used to emit connection lifecycle events."""

        self._status_callback = callback

    def _emit_status(self, state: str, message: str | None = None) -> None:
        """Emit a status event if a callback is configured."""

        if self._status_callback is not None:
            self._status_callback(state, message)

    def _service_info_from_product_id(self) -> bluetooth.BluetoothServiceInfoBleak | None:
        """Return the best current advertisement for the configured product ID."""

        if self.product_id is None:
            return None

        best: bluetooth.BluetoothServiceInfoBleak | None = None
        for service_info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            if not service_info.connectable or not is_cdc_advertisement(service_info):
                continue

            advertisement = decode_advertisement(service_info)
            if advertisement.type_id is not None and advertisement.type_id != CDC_6_ZONE_TYPE_ID:
                continue
            if advertisement.product_id != self.product_id:
                continue
            if best is None or (service_info.rssi or -999) > (best.rssi or -999):
                best = service_info

        return best

    async def _resolve_service_info(
        self,
        *,
        allow_active_scan: bool,
    ) -> bluetooth.BluetoothServiceInfoBleak | None:
        """Resolve the latest advertisement for this controller."""

        service_info = self._service_info_from_product_id()
        if service_info is None:
            service_info = bluetooth.async_last_service_info(
                self.hass,
                self.address,
                connectable=True,
            )

        if service_info is None and allow_active_scan:
            await bluetooth.async_request_active_scan(self.hass)
            service_info = self._service_info_from_product_id()
            if service_info is None:
                service_info = bluetooth.async_last_service_info(
                    self.hass,
                    self.address,
                    connectable=True,
                )

        if service_info is not None:
            self.address = service_info.address
            self.name = service_info.name or self.name
            self._emit_status("advertisement")
        elif allow_active_scan:
            self._emit_status("not_discoverable")

        return service_info

    async def _ble_device(self) -> BLEDevice:
        """Return a connectable BLE device from Home Assistant's Bluetooth manager."""

        await self._resolve_service_info(allow_active_scan=True)
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if ble_device is None:
            self._emit_status("not_discoverable")
            raise RainVisionError("Bluetooth device is not available or not connectable")
        return ble_device

    async def _connect(self) -> BleakClient:
        """Open a BLE connection."""

        ble_device = await self._ble_device()
        self._emit_status("connecting")
        _LOGGER.debug("Connecting to %s (%s)", self.address, self.name)
        client = await establish_connection(
            BleakClient,
            ble_device,
            self.name,
            timeout=20,
        )
        self._emit_status("connected")
        _LOGGER.debug("Connected to %s", self.address)
        return client

    async def _disconnect(self, client: BleakClient) -> None:
        """Disconnect without raising during cleanup."""

        if client.is_connected:
            with suppress(Exception):
                await client.disconnect()
                _LOGGER.debug("Disconnected from %s", self.address)
        self._emit_status("idle")

    async def _read(self, client: BleakClient, uuid: str, label: str) -> bytes:
        """Read a characteristic and log the response."""

        self._emit_status("reading")
        payload = bytes(await client.read_gatt_char(uuid))
        self._emit_status("connected")
        _LOGGER.debug("BLE read %s (%s) -> %s", label, uuid, _hex(payload))
        return payload

    async def _write(
        self,
        client: BleakClient,
        uuid: str,
        label: str,
        payload: bytes | bytearray | memoryview,
        *,
        redact: bool = False,
    ) -> None:
        """Write a characteristic and log the request."""

        self._emit_status("writing")
        logged_payload = "<redacted>" if redact else _hex(payload)
        _LOGGER.debug("BLE write %s (%s) <- %s", label, uuid, logged_payload)
        await client.write_gatt_char(uuid, bytes(payload), response=True)
        self._emit_status("connected")
        _LOGGER.debug("BLE write %s (%s) acknowledged", label, uuid)

    async def read_device_info(self) -> tuple[str | None, str | None]:
        """Read firmware and hardware version."""

        client = await self._connect()
        try:
            firmware = (await self._read(client, FIRMWARE_UUID, "firmware")).decode(errors="ignore").strip("\x00")
            hardware = (await self._read(client, HARDWARE_UUID, "hardware")).decode(errors="ignore").strip("\x00")
            _LOGGER.debug("Device info for %s firmware=%s hardware=%s", self.address, firmware, hardware)
            return firmware, hardware
        finally:
            await self._disconnect(client)

    async def validate_supported_device(self) -> None:
        """Validate that the connected device is CDC firmware 5.0 or newer."""

        firmware, _hardware = await self.read_device_info()
        if firmware is None:
            raise UnsupportedDeviceError("Unable to read firmware")
        try:
            major, minor = (int(part) for part in firmware.split(".", 1))
            req_major, req_minor = (int(part) for part in CDC_SUPPORTED_FIRMWARE.split(".", 1))
        except ValueError as err:
            raise UnsupportedDeviceError(f"Unexpected firmware version: {firmware}") from err
        if (major, minor) < (req_major, req_minor):
            raise UnsupportedDeviceError(f"Unsupported firmware version: {firmware}")

    async def read_password(self) -> str | None:
        """Read the device password when the platform allows it."""

        client = await self._connect()
        try:
            data = await self._read(client, PASSWORD_UUID, "password")
            return bytes(byte for byte in data if byte != 0).decode(errors="ignore")
        finally:
            await self._disconnect(client)

    async def set_password(self, password: str) -> None:
        """Set a six digit password."""

        if len(password) != 6 or not password.isdigit():
            raise ValueError("Password must be exactly 6 digits")

        client = await self._connect()
        try:
            await self._write(
                client,
                PASSWORD_UUID,
                "password",
                password.encode("ascii")[:6],
                redact=True,
            )
            self.password = password
        finally:
            await self._disconnect(client)

    async def async_update(self, manual_totals: list[int] | None = None) -> RainVisionData:
        """Read all data needed by Home Assistant."""

        service_info = await self._resolve_service_info(allow_active_scan=False)
        advertisement = decode_advertisement(service_info) if service_info else None

        client = await self._connect()
        try:
            _LOGGER.debug("Starting update for %s", self.address)
            await self._authenticate(client)
            await self._write_time(client)

            firmware = (await self._read(client, FIRMWARE_UUID, "firmware")).decode(errors="ignore").strip("\x00")
            hardware = (await self._read(client, HARDWARE_UUID, "hardware")).decode(errors="ignore").strip("\x00")
            status_payload = await self._read(client, STATUS_UUID, "status")
            status = decode_status(status_payload)
            _LOGGER.debug("Decoded status for %s: %s", self.address, status)

            requested_manual_totals = ((manual_totals or []) + [0] * CDC_ZONES)[:CDC_ZONES]
            device_manual_totals: list[int] | None = None
            with suppress(Exception):
                manual_payload = await self._read(client, MANUAL_UUID, "manual")
                device_manual_totals = decode_manual_durations(manual_payload)

            battery = advertisement.battery if advertisement else None
            with suppress(Exception):
                battery_payload = await self._read(client, BATTERY_LEVEL_UUID, "battery")
                if battery_payload:
                    battery = battery_payload[0]

            schedules: list[RainVisionSchedule] = []
            if status.number_of_icycles > 0:
                can_read_icycles = True
                try:
                    await self._write(
                        client,
                        COMMAND_UUID,
                        "command reset icycle pointer",
                        bytes([COMMAND_RESET_ICYCLE_POINTER]),
                    )
                except Exception as err:
                    if _is_missing_characteristic_error(err, COMMAND_UUID):
                        can_read_icycles = False
                        _LOGGER.warning(
                            "Skipping schedule read for %s: command characteristic %s is missing",
                            self.address,
                            COMMAND_UUID,
                        )
                    else:
                        raise

                if can_read_icycles:
                    await asyncio.sleep(0.2)
                    for index in range(status.number_of_icycles):
                        with suppress(Exception):
                            payload = await self._read(client, ICYCLE_UUID, f"icycle {index + 1}")
                            schedule = decode_icycle(payload)
                            schedules.append(schedule)
                            _LOGGER.debug("Decoded iCycle %s/%s: %s", index + 1, status.number_of_icycles, schedule)
                        await asyncio.sleep(0.01)
                    await asyncio.sleep(0.2)
                    with suppress(Exception):
                        await self._write(
                            client,
                            COMMAND_UUID,
                            "command reset icycle pointer",
                            bytes([COMMAND_RESET_ICYCLE_POINTER]),
                        )

            now = datetime.now().astimezone()
            upcoming = [run for schedule in schedules if (run := next_run(schedule, now)) is not None]
            effective_manual_totals = requested_manual_totals
            if device_manual_totals is not None and not any(requested_manual_totals):
                effective_manual_totals = device_manual_totals

            data = RainVisionData(
                name=self.name,
                address=self.address,
                product_id=advertisement.product_id if advertisement else None,
                firmware=firmware,
                hardware=hardware,
                battery=battery,
                rssi=advertisement.rssi if advertisement else None,
                status=status,
                schedules=schedules,
                next_irrigation=min(upcoming) if upcoming else None,
                manual_totals=effective_manual_totals,
            )
            _LOGGER.debug(
                "Finished update for %s battery=%s schedules=%s next_irrigation=%s",
                self.address,
                data.battery,
                len(data.schedules),
                data.next_irrigation,
            )
            return data
        finally:
            await self._disconnect(client)

    async def start_manual_irrigation(self, durations: Iterable[int]) -> None:
        """Start manual watering by writing MANUAL then command 5."""

        values = [max(0, min(28800, int(duration))) for duration in durations]
        values = (values + [0] * CDC_ZONES)[:CDC_ZONES]

        manual = bytearray(64)
        for index, duration in enumerate(values):
            offset = index * 2
            manual[offset : offset + 2] = duration.to_bytes(2, "big")

        _LOGGER.debug("Starting manual irrigation for %s durations=%s", self.address, values)
        client = await self._connect()
        try:
            await self._authenticate(client)
            await asyncio.sleep(0.5)
            await self._write(client, COMMAND_UUID, "command stop current cycle", bytes([COMMAND_STOP_CURRENT_CYCLE]))
            await asyncio.sleep(0.5)
            await self._write(client, MANUAL_UUID, "manual durations", manual)
            await asyncio.sleep(0.5)
            await self._write(client, COMMAND_UUID, "command start manual mode", bytes([COMMAND_START_MANUAL_MODE]))
        finally:
            await self._disconnect(client)

    async def stop_irrigation(self) -> None:
        """Stop manual mode and return to normal mode, matching the app."""

        _LOGGER.debug("Stopping irrigation for %s", self.address)
        client = await self._connect()
        try:
            await self._authenticate(client)
            await asyncio.sleep(0.5)
            await self._write(client, COMMAND_UUID, "command start ready mode", bytes([COMMAND_START_READY_MODE]))
            await asyncio.sleep(1.5)
            await self._write(client, COMMAND_UUID, "command start normal mode", bytes([COMMAND_START_NORMAL_MODE]))
        finally:
            await self._disconnect(client)

    async def _authenticate(self, client: BleakClient) -> None:
        """Write the password to unlock authenticated characteristics (e.g. COMMAND)."""

        if self.password:
            await self._write(client, PASSWORD_UUID, "password", self.password.encode("ascii")[:6], redact=True)

    async def _write_time(self, client: BleakClient) -> None:
        """Write the app's local-offset-adjusted timestamp."""

        offset = datetime.now().astimezone().utcoffset() or timedelta()
        timestamp = int(time.time() + offset.total_seconds() + 2)
        await self._write(client, TIME_UUID, "time", timestamp.to_bytes(4, "big"))
