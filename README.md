# rain-vision-ha

Custom Home Assistant integration for Rain Vision CONNECT DC BATTERY 6 zone devices
with firmware `5.0`.

This integration talks to the device over BLE using the same Rain characteristics and
command payloads used by the Android app:

- CDC 6-zone discovery/onboarding from Bluetooth advertisements
- Password setup and password editing through the integration options
- Battery, status, schedule count, next irrigation, and per-zone timing sensors
- Weekly irrigation totals (global + per-zone), persisted across restarts and reset every local week
- Manual irrigation service with one duration per zone
- Short-lived BLE connections only; the coordinator disconnects after each refresh or command

## Install with HACS

Add this repository as a custom repository in HACS, category `Integration`, then install
`rain-vision-ha` and restart Home Assistant.

## Services

`rain_vision_ha.start_manual_irrigation`

```yaml
target:
  device_id: your_device_id
data:
  zone_1: 300
  zone_2: 0
  zone_3: 120
  zone_4: 0
  zone_5: 0
  zone_6: 0
```

Durations are seconds, matching the Android app. `0` skips a zone.

`rain_vision_ha.stop_irrigation`

Stops manual mode and returns the controller to normal mode, matching the Android
app command sequence.

## Debug logging

To inspect BLE requests and responses, enable debug logging:

```yaml
logger:
  logs:
    custom_components.rain_vision_ha: debug
```
