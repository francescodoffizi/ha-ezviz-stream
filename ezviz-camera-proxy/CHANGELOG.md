# Changelog

All notable changes to the Ezviz Camera Proxy add-on will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.7] — 2026-04-13

### Added

- **Battery Optimization**: Increased background status polling interval to 300s (5 minutes) when `SNAPSHOT_INTERVAL` is 0, relying on real-time push for events.
- **Deep Debug**: Enhanced `/api/debug/alarms` to search multiple message subtypes (`101`, `102`, `2701`) for doorbell events.

---

## [1.3.6] — 2026-04-12

### Added

- **Debug endpoint**: Added `/api/debug/alarms` to allow manual verification of cloud event history. This helps in diagnosing why push events might be missing (e.g. cloud misconfiguration).

---

## [1.3.5] — 2026-04-12

### Fixed

- **Event Processing**: Fixed a regression in the real-time event handler and improved event ID extraction to ensure all push messages are processed correctly.

---

## [1.3.4] — 2026-04-12

### Fixed

- **Cleaner Entity Names**: Simplified sensor names in MQTT Discovery to avoid redundant serial numbers. This should result in cleaner entity IDs in Home Assistant like `binary_sensor.ezviz_camera_bh9350432_motion`.

---

## [1.3.3] — 2026-04-12

### Fixed

- **Flattened MQTT Attributes**: Event data in binary sensors is now flattened, allowing direct access to the `image` URL via `state_attr(..., 'image')` in Home Assistant automations.

---

## [1.3.2] — 2026-04-12

### Added

- **Global Alarm Sensor**: Added `binary_sensor.ezviz_{serial}_alarm` that triggers for ANY detected event (motion, doorbell, person, etc.). This ensures no event is missed while debugging specific codes.
- **Enhanced Debug Logging**: The raw payload of every push event is now logged to help identify unknown doorbell codes.

### Fixed

- **Expanded Doorbell Detection**: Added more potential doorbell event codes (`10000`, `10054`, `10055`, etc.) to the detection logic.

---

## [1.3.1] — 2026-04-12

### Added

- **MQTT Attributes**: Binary sensors for Motion and Doorbell now include the full Ezviz JSON event data as attributes. This makes it easy to extract the `image` URL for Home Assistant notifications.

---

## [1.3.0] — 2026-04-12

### Fixed

- **Ezviz Cloud MQTT Stability**: Fixed a scoping bug that caused the connection to the Ezviz event stream to be unstable and reset every minute.
- **Local MQTT Discovery**: Verified and confirmed that auto-discovery messages are correctly sent to Home Assistant.

---

## [1.2.9] — 2026-04-12

### Added

- **Manual MQTT Configuration**: Added options to manually specify MQTT host, port, username, and password in the add-on configuration. This serves as a definitive fallback if auto-detection fails.

### Fixed

- **Bashio Service Detection**: Switched to a more robust `bashio::service` syntax to fix "command not found" errors during startup.

---

## [1.2.8] — 2026-04-12

### Fixed

- **Boot Crash**: Fixed "unbound variable" error in `run.sh` that caused the add-on to crash during startup.
- **Bashio Compatibility**: Updated MQTT service detection to use `bashio::services.available` for better compatibility with current Home Assistant versions.

---

## [1.2.7] — 2026-04-12

### Fixed

- **Event List recovery**: Fixed the issue where the events table on the dashboard was empty when the snapshot interval was set to 0. Events are now polled every 60 seconds from the cloud, and real-time push events are instantly added to the list.

---

## [1.2.6] — 2026-04-12

### Added

- **MQTT Diagnostics**: Added verbose startup logs to identify why Home Assistant MQTT services are not being detected automatically.
- **MQTT Fallback**: Added a fallback detection for `core-mosquitto` broker in common Home Assistant setups.

---

## [1.2.5] — 2026-04-12

### Added

- **Home Assistant MQTT Discovery**: Automatically registers "Motion" and "Doorbell" sensors in Home Assistant. They will now appear as devices/entities without manual YAML configuration.
- **Specific Event Topics**: Now publishing to `homeassistant/camera/ezviz/{serial}/doorbell` and `homeassistant/camera/ezviz/{serial}/motion` as requested.

### Fixed

- **Event Mapping**: Improved logic to distinguish between person/motion events and doorbell presses based on Ezviz alert codes.

---

## [1.2.4] — 2026-04-12

### Added

- **Auto-Refresh Dashboard**: The UI now automatically refreshes the snapshot and battery level whenever a new event is detected or a manual refresh occurs.
- **MQTT Auto-Discovery**: Fixed MQTT service detection in Home Assistant Supervisor; the add-on now correctly identifies the local broker for alarm publishing.

### Fixed

- **Event Reliability**: Moved event-driven snapshot triggering to run even if MQTT is not configured locally.

---

## [1.2.3] — 2026-04-12

### Added

- **Enhanced Event Logging**: Added verbose logging for Ezviz Cloud push messages to debug doorbell and motion detection issues in real-time.

---

## [1.2.2] — 2026-04-12

### Fixed

- **Aggressive Auth Backoff**: Increased the authentication retry cooling period to 60 seconds globally and to 5 minutes for the background worker. This is specifically designed to help recover from Ezviz Error 1069 ("terminal limit reached") by giving the account time to clear stale sessions.

---

## [1.2.1] — 2026-04-12

### Fixed

- **UI Refresh Loop**: Fixed a bug where setting `snapshot_interval` to `0` caused the dashboard to refresh every second.
- **Auth Flood Protection**: Added a 30-second "cooling period" after any authentication failure. This prevents the proxy from hammering Ezviz servers and hitting the "too many terminals" (error 1069) limit when sessions expire or credentials issues occur.

---

## [1.2.0] — 2026-04-12

### Added

- **Event-Driven Snapshots (Battery Saver)**: The add-on now allows `snapshot_interval` to be set to `0` (or any large number up to completely disabled). When a real-time push event (motion or doorbell) arrives via Ezviz MQTT, an immediate, lightweight threaded snapshot fetch is forced instantly, perfectly synchronized with the event. This drastically saves battery on the HP2 camera, as the proxy no longer forcefully wakes the device up on an arbitrary polling clock.

---

## [1.1.4] — 2026-04-12

### Added

- **Real-Time Push Notifications**: Migrated the internal architecture from 60-second API polling to zero-latency MQTT push. The proxy now maintains a persistent connection to Ezviz Cloud's push servers. When motion or a doorbell ring occurs, the event is immediately intercepted and forwarded to the local Home Assistant MQTT broker, eliminating the polling delay entirely!

---

## [1.1.3] — 2026-04-12
- Support Content-Length in MJPEG streams

---

## [1.1.2] — 2026-04-12

### Fixed

- **MJPEG Stream Timeout**: Decreased the MJPEG `frame_delay` in `/api/stream` to 1.0 seconds (1 FPS constant). Previously the delay was bound to the `SNAPSHOT_INTERVAL` (e.g. 30 seconds), causing strict clients like Home Assistant Generic Camera to drop the connection due to timeout.

---

## [1.1.1] — 2026-04-12

### Fixed

- **API Ports:** Exposed container port `8099` to the Home Assistant host network. Previously, endpoints like `/api/snapshot` and `/api/stream` were only accessible internally via HA Ingress and not exposed externally for integrations like Generic Camera.

---

## [1.1.0] — 2026-04-12

### Added

- **MQTT Event Forwarding**: Fully implemented MQTT logic in `server.py` to publish new events from Ezviz Cloud directly to the local Home Assistant MQTT Broker (on topic `ezviz/<CAMERA_SERIAL>/alarm`).
- Required the `mqtt: want` service in the add-on configuration, allowing Home Assistant to securely feed the broker connection parameters to the add-on dynamically.

### Changed

- Updated core dependency `pyezvizapi` to version `>=1.0.4.5` for better stability and latest endpoint capabilities.
- Added a smart retry and wait mechanism inside the event fetcher. If a newly discovered alarm on the cloud lacks an image (which happens frequently due to battery camera upload delays), the proxy will wait a few seconds and try fetching it again to improve the image success rate on Home Assistant templates.

---

## [1.0.1] — 2026-04-02

### Fixed

- **Critical:** Bypass `pyezvizapi`'s `get_device_infos()` which crashes with
  `'str' object has no attribute 'get'` on HP2 cameras. The HP2 returns string
  values instead of dicts in the CLOUD section of the pagelist response.
  New implementation calls `_get_page_list()` directly and safely parses all
  sections, fully bypassing the broken code path.
- Fixed `datetime.utcnow()` deprecation warnings (replaced with
  `datetime.now(timezone.utc)`).
- Improved snapshot worker with consecutive error tracking and backoff.
- Default `snapshot_interval` increased from 30s to 60s (HP2 is battery-powered).

---

## [1.0.0] — 2026-04-02

### Added

- Initial release of the Ezviz Camera Proxy add-on
- Ezviz Cloud API integration via `pyezvizapi`
- Periodic snapshot polling with configurable interval (5–300 seconds)
- Token caching to `/data/ezviz_token.json` for reduced login calls
- HTTP endpoints:
  - `GET /api/snapshot` — Latest cached snapshot as JPEG
  - `POST /api/snapshot/refresh` — On-demand cloud snapshot fetch
  - `GET /api/status` — Camera status (online, battery, WiFi signal, firmware)
  - `GET /api/events` — Recent alarm events list
  - `GET /api/stream` — Simulated MJPEG stream from cached snapshots
  - `GET /api/devices` — All devices on the Ezviz account
  - `GET /api/health` — Add-on health check
- Home Assistant Ingress support with sidebar panel (`mdi:doorbell-video`)
- Built-in dark-themed Web UI dashboard:
  - Real-time snapshot display with auto-refresh
  - Camera status panel (battery level, WiFi, online/offline)
  - Recent events/alarms list
  - Manual refresh button
  - MJPEG stream link
  - HA Generic Camera URL helper
- MQTT event publishing for doorbell and motion detection (optional)
- Auto-reconnect on session expiry
- Placeholder JPEG image for "no snapshot yet" state
- Support for architectures: `amd64`, `aarch64`, `armv7`, `armhf`, `i386`
- English translations for all config options
- Comprehensive documentation (DOCS.md)

### Notes

- The Ezviz HP2 is battery-powered and enters deep sleep between events.
  Each snapshot fetch briefly wakes the device. Adjust `snapshot_interval`
  to balance responsiveness and battery life.
- RTSP and LAN Live View are not supported by the HP2 hardware/firmware.
  This add-on uses the Ezviz Cloud API as the only available access path.
