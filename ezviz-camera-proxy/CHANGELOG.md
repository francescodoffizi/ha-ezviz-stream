# Changelog

All notable changes to the Ezviz Camera Proxy add-on will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
