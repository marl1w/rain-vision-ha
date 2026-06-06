# rain-vision-ha

Custom Home Assistant integration for Rain Vision **CONNECT DC BATTERY 6 zone** devices
with firmware `5.0`.

Communicates over BLE using the same Rain characteristics and command payloads as the
Android app. Connections are short-lived; the coordinator disconnects after each refresh
or command.

## Features

### Setup

- Automatic Bluetooth discovery and one-click onboarding
- Manual device selection from nearby BLE advertisements, sorted by signal strength
- Password validation against the device during setup
- Zone selection: enable or disable individual zones at setup or later via options
- Options flow: change password (written to device), toggle zones, set polling window

### Sensors

| Sensor | Notes |
|---|---|
| Battery | Percentage |
| Status | `idle`, `watering`, `manual`, etc. — includes FSM state, zone state, pump, rain sensor |
| Active zone | Currently running zone number |
| Schedule program count | Number of active programs; attributes list active and disabled cycles |
| Next irrigation | Timestamp of next scheduled cycle |
| Zone N remaining time | Per-zone countdown in seconds while watering |
| Device connection status | Current BLE lifecycle state with last poll/write/error timestamps |
| Polling mode | `none`, `normal`, `fast`, or `paused` |
| Next device refresh | Timestamp of next scheduled BLE poll |
| Firmware | Disabled by default |
| RSSI | Signal strength in dBm — disabled by default |

### Switches

| Switch | Behaviour |
|---|---|
| Zone N irrigation | Turns on manual irrigation for that zone using the configured duration; turns off with stop |
| Scheduled irrigation | Toggle off to pause all active schedules on the device; toggle on to restore them |

Pause state is persisted across restarts so the switch survives a Home Assistant reboot.

### Numbers

| Number | Range |
|---|---|
| Zone N manual duration | 1 – 480 minutes — used by the zone switch and `start_manual_irrigation` service |

### Buttons

| Button | Behaviour |
|---|---|
| Manual refresh | Triggers an immediate BLE poll outside of the normal polling schedule |

### Smart polling

- **Normal** — 30-minute interval during idle periods
- **Fast** — 30-second interval while irrigation is running, just after a manual start,
  or within 2 minutes of the next scheduled cycle
- **Schedule-driven** — when idle, the interval is automatically shortened so the next
  poll lands just as the near-schedule window opens
- **Paused** — outside a configured polling window; automatically resumes at window open

## Install with HACS

Add this repository as a custom repository in HACS, category `Integration`, then install
`rain-vision-ha` and restart Home Assistant.

## Options

Open the integration options to:

- **Password** — update the 6-digit PIN (written to the device on save)
- **Enabled zones** — select which zones to expose as entities
- **Polling window** — restrict BLE polling to a time range (e.g. `06:00`–`22:00`)

## Services

### `rain_vision_ha.start_manual_irrigation`

Start manual irrigation with one duration (in seconds) per zone. `0` skips a zone.
Maximum duration is 28800 s (8 hours) per zone.

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

### `rain_vision_ha.stop_irrigation`

Stops manual mode and returns the controller to normal mode.

```yaml
target:
  device_id: your_device_id
```

Both services accept `device_id` as a string or list. Omitting `device_id` targets all
configured Rain Vision devices.

## Debug logging

```yaml
logger:
  logs:
    custom_components.rain_vision_ha: debug
```

This logs all BLE requests, responses, and polling decisions.
