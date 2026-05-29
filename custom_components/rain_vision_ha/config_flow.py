"""Config flow for rain-vision-ha."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_NAME
from homeassistant.helpers import config_validation as cv
from homeassistant.core import callback
from homeassistant.helpers.selector import BooleanSelector, TimeSelector
import voluptuous as vol

from .const import (
    CDC_6_ZONE_NAME,
    CDC_6_ZONE_TYPE_ID,
    CONF_ADDRESS,
    CONF_ENABLED_ZONES,
    CONF_PASSWORD,
    CONF_POLL_WINDOW_ENABLED,
    CONF_POLL_WINDOW_END,
    CONF_POLL_WINDOW_START,
    CONF_PRODUCT_ID,
    DEFAULT_PASSWORD,
    DOMAIN,
)
from .protocol import (
    RainVisionCdcClient,
    RainVisionError,
    UnsupportedDeviceError,
    decode_advertisement,
    is_cdc_advertisement,
)
from .zones import normalize_enabled_zones, zone_options

CONF_DEVICE = "device"


def _entry_unique_id(product_id: int | None, address: str) -> str:
    """Return a stable config entry unique ID."""

    if product_id is not None:
        return f"rv-{product_id}"
    return address


def _device_label(service_info: bluetooth.BluetoothServiceInfoBleak) -> str:
    """Return a short device label for selection."""

    advertisement = decode_advertisement(service_info)
    name = service_info.name or CDC_6_ZONE_NAME
    details: list[str] = []
    if advertisement.product_id is not None:
        details.append(f"ID {advertisement.product_id}")
    if service_info.rssi is not None:
        details.append(f"RSSI {service_info.rssi}")
    if details:
        return f"{name} ({', '.join(details)})"
    return name


def _valid_password(password: str) -> bool:
    """Return true if the password matches the app's BLE pin rules."""

    return len(password) == 6 and password.isdigit()


def _valid_time_str(time_str: str) -> bool:
    """Return true if time_str is empty or a valid HH:MM value."""

    if not time_str:
        return True
    try:
        parts = time_str.split(":")
        if len(parts) != 2:
            return False
        h, m = int(parts[0]), int(parts[1])
        return 0 <= h <= 23 and 0 <= m <= 59
    except (ValueError, AttributeError):
        return False


class RainVisionCdcConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle rain-vision-ha config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""

        self._discovery_info: bluetooth.BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, bluetooth.BluetoothServiceInfoBleak] = {}
        self._address: str | None = None
        self._product_id: int | None = None
        self._title: str = CDC_6_ZONE_NAME

    async def _async_discovered_devices(
        self,
    ) -> dict[str, bluetooth.BluetoothServiceInfoBleak]:
        """Return currently discovered supported devices keyed by address."""

        await bluetooth.async_request_active_scan(self.hass)

        devices: dict[str, bluetooth.BluetoothServiceInfoBleak] = {}
        for service_info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            if not service_info.connectable or not is_cdc_advertisement(service_info):
                continue

            advertisement = decode_advertisement(service_info)
            if advertisement.type_id is not None and advertisement.type_id != CDC_6_ZONE_TYPE_ID:
                continue

            devices[service_info.address] = service_info

        self._discovered_devices = dict(
            sorted(
                devices.items(),
                key=lambda item: (
                    -(item[1].rssi if item[1].rssi is not None else -999),
                    (item[1].name or CDC_6_ZONE_NAME),
                    item[0],
                ),
            )
        )
        return self._discovered_devices

    def _async_abort_if_device_configured(self, product_id: int | None, address: str) -> None:
        """Abort when the same controller is already configured."""

        if product_id is not None:
            self._async_abort_entries_match({CONF_PRODUCT_ID: product_id})
        self._async_abort_entries_match({CONF_ADDRESS: address})

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> config_entries.ConfigFlowResult:
        """Handle Bluetooth discovery."""

        if not discovery_info.connectable:
            return self.async_abort(reason="not_connectable")
        if not is_cdc_advertisement(discovery_info):
            return self.async_abort(reason="unsupported_device")

        advertisement = decode_advertisement(discovery_info)
        if advertisement.type_id is not None and advertisement.type_id != CDC_6_ZONE_TYPE_ID:
            return self.async_abort(reason="unsupported_device")

        self._discovery_info = discovery_info
        self._address = discovery_info.address
        self._product_id = advertisement.product_id
        self._title = discovery_info.name or CDC_6_ZONE_NAME
        await self.async_set_unique_id(_entry_unique_id(advertisement.product_id, discovery_info.address))
        self._async_abort_if_device_configured(advertisement.product_id, discovery_info.address)
        self._abort_if_unique_id_configured()

        self.context["title_placeholders"] = {"name": self._title}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm a Bluetooth-discovered device."""

        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            enabled_zones = normalize_enabled_zones(
                user_input.get(CONF_ENABLED_ZONES),
                self._product_id,
                fallback_to_available=False,
            )
            if not _valid_password(password):
                errors[CONF_PASSWORD] = "invalid_password"
            elif not enabled_zones:
                errors[CONF_ENABLED_ZONES] = "invalid_zones"
            else:
                assert self._address is not None
                result = await self._validate(self._address, password, self._product_id)
                if result is None:
                    return self.async_create_entry(
                        title=self._title,
                        data={
                            CONF_ADDRESS: self._address,
                            CONF_PASSWORD: password,
                            CONF_PRODUCT_ID: self._product_id,
                            CONF_ENABLED_ZONES: enabled_zones,
                        },
                    )
                errors["base"] = result

        zone_select_options = zone_options(self._product_id)
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
                    vol.Required(CONF_ENABLED_ZONES, default=list(zone_select_options)): cv.multi_select(
                        zone_select_options
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"name": self._title},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle manual setup."""

        if not self._discovered_devices:
            devices = await self._async_discovered_devices()
            if not devices:
                return self.async_abort(reason="no_devices_found")

        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_DEVICE]
            password = user_input[CONF_PASSWORD]
            discovery_info = self._discovered_devices.get(address)
            if discovery_info is None:
                await self._async_discovered_devices()
                errors["base"] = "device_not_found"
            else:
                advertisement = decode_advertisement(discovery_info)
                enabled_zones = normalize_enabled_zones(
                    user_input.get(CONF_ENABLED_ZONES),
                    advertisement.product_id,
                    fallback_to_available=False,
                )
                title = user_input.get(CONF_NAME) or discovery_info.name or CDC_6_ZONE_NAME
                if not _valid_password(password):
                    errors[CONF_PASSWORD] = "invalid_password"
                elif not enabled_zones:
                    errors[CONF_ENABLED_ZONES] = "invalid_zones"
                else:
                    await self.async_set_unique_id(_entry_unique_id(advertisement.product_id, address))
                    self._async_abort_if_device_configured(advertisement.product_id, address)
                    self._abort_if_unique_id_configured()
                    result = await self._validate(address, password, advertisement.product_id, title)
                    if result is None:
                        return self.async_create_entry(
                            title=title,
                            data={
                                CONF_ADDRESS: address,
                                CONF_PASSWORD: password,
                                CONF_PRODUCT_ID: advertisement.product_id,
                                CONF_ENABLED_ZONES: enabled_zones,
                            },
                        )
                    errors["base"] = result

        device_options = {
            address: _device_label(service_info)
            for address, service_info in self._discovered_devices.items()
        }
        default_device = next(iter(device_options))
        default_product_id = self._discovered_devices[default_device]
        default_zone_options = zone_options(decode_advertisement(default_product_id).product_id)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE, default=default_device): vol.In(device_options),
                    vol.Required(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
                    vol.Required(CONF_ENABLED_ZONES, default=list(default_zone_options)): cv.multi_select(
                        default_zone_options
                    ),
                    vol.Optional(CONF_NAME, default=CDC_6_ZONE_NAME): str,
                }
            ),
            errors=errors,
        )

    async def _validate(
        self,
        address: str,
        password: str,
        product_id: int | None = None,
        title: str | None = None,
    ) -> str | None:
        """Validate connectivity and firmware."""

        client = RainVisionCdcClient(self.hass, address, password, title or self._title, product_id)
        try:
            await client.validate_supported_device()
        except UnsupportedDeviceError:
            return "unsupported_device"
        except RainVisionError:
            return "cannot_connect"
        except Exception:
            return "cannot_connect"
        return None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow."""

        return RainVisionCdcOptionsFlow()


class RainVisionCdcOptionsFlow(config_entries.OptionsFlow):
    """Handle options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit password, enabled zones, and polling window."""

        errors: dict[str, str] = {}
        current_password = self.config_entry.options.get(
            CONF_PASSWORD,
            self.config_entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
        )
        product_id = self.config_entry.data.get(CONF_PRODUCT_ID)
        current_enabled = normalize_enabled_zones(
            self.config_entry.options.get(CONF_ENABLED_ZONES, self.config_entry.data.get(CONF_ENABLED_ZONES)),
            product_id,
        )
        current_window_start = self.config_entry.options.get(CONF_POLL_WINDOW_START, "")
        current_window_end = self.config_entry.options.get(CONF_POLL_WINDOW_END, "")
        current_window_enabled = self.config_entry.options.get(
            CONF_POLL_WINDOW_ENABLED,
            bool(current_window_start and current_window_end),
        )
        zone_select_options = zone_options(product_id)

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            enabled_zones = normalize_enabled_zones(
                user_input.get(CONF_ENABLED_ZONES),
                product_id,
                fallback_to_available=False,
            )
            window_enabled = user_input.get(CONF_POLL_WINDOW_ENABLED, False)
            if window_enabled:
                # TimeSelector returns "HH:MM:SS" — keep only "HH:MM"
                window_start = user_input.get(CONF_POLL_WINDOW_START, "")[:5]
                window_end = user_input.get(CONF_POLL_WINDOW_END, "")[:5]
            else:
                window_start = ""
                window_end = ""

            if not _valid_password(password):
                errors[CONF_PASSWORD] = "invalid_password"
            elif not enabled_zones:
                errors[CONF_ENABLED_ZONES] = "invalid_zones"
            else:
                new_options = {
                    CONF_PASSWORD: password,
                    CONF_ENABLED_ZONES: enabled_zones,
                    CONF_POLL_WINDOW_ENABLED: window_enabled,
                    CONF_POLL_WINDOW_START: window_start,
                    CONF_POLL_WINDOW_END: window_end,
                }
                if password != current_password:
                    client = RainVisionCdcClient(
                        self.hass,
                        self.config_entry.data[CONF_ADDRESS],
                        current_password,
                        self.config_entry.title,
                        product_id,
                    )
                    try:
                        await client.set_password(password)
                    except Exception:
                        errors["base"] = "cannot_connect"
                    else:
                        return self.async_create_entry(title="", data=new_options)
                else:
                    return self.async_create_entry(title="", data=new_options)

        default_start = (current_window_start + ":00") if current_window_start else "06:00:00"
        default_end = (current_window_end + ":00") if current_window_end else "22:00:00"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD, default=current_password): str,
                    vol.Required(
                        CONF_ENABLED_ZONES,
                        default=[str(zone) for zone in current_enabled],
                    ): cv.multi_select(zone_select_options),
                    vol.Optional(CONF_POLL_WINDOW_ENABLED, default=current_window_enabled): BooleanSelector(),
                    vol.Optional(CONF_POLL_WINDOW_START, default=default_start): TimeSelector(),
                    vol.Optional(CONF_POLL_WINDOW_END, default=default_end): TimeSelector(),
                }
            ),
            errors=errors,
        )
