"""
Ezviz Cloud API Client Wrapper
Wraps pyezvizapi (v1.0.x) for use in the HA Add-on.
Handles authentication, token caching, device status and snapshots.

NOTE: The HP2 camera returns non-standard data in the CLOUD section of the
pagelist API, causing pyezvizapi's get_device_infos() to crash with
'str' object has no attribute 'get'.  This wrapper works around that by
calling the pagelist API directly and parsing the response safely.
"""

import io
import json
import logging
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


class EzvizClientError(Exception):
    """Base exception for EzvizClient errors."""


class EzvizAuthError(EzvizClientError):
    """Authentication failed."""


class EzvizDeviceError(EzvizClientError):
    """Device operation failed."""


class EzvizClient:
    """
    Wrapper around pyezvizapi that handles:
    - Login / token caching
    - Auto-reconnect on session expiry
    - Device status, snapshot and alarm retrieval

    Compatible with pyezvizapi >= 1.0.0.
    Works around HP2-specific pagelist format issues.
    """

    TOKEN_EXPIRY_HOURS = 23

    def __init__(
        self,
        username: str,
        password: str,
        region: str = "apiieu.ezvizlife.com",
        camera_serial: str = "",
        camera_password: str = "",
        encryption_key: str = "",
        open_domain: str = "https://ieuopen.ezvizlife.com",
        app_key: str = "",
        app_secret: str = "",
        access_token: str = "",
        token_cache_path: str = "/data/open_token.json",
        session_cache_path: str = "/data/cloud_session.json",
    ):
        self.username = username
        self.password = password
        self.region = region
        self.camera_serial = camera_serial
        self.camera_password = camera_password
        self.encryption_key = encryption_key
        self.open_domain = (open_domain or "https://ieuopen.ezvizlife.com").rstrip("/")
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token
        self.token_cache_path = Path(token_cache_path)
        self.session_cache_path = Path(session_cache_path)

        self._client = None  # pyezvizapi.EzvizClient instance
        self._lock = threading.Lock()
        self._last_login: datetime | None = None
        self._open_token_expire_time: float = 0
        self._cached_session: dict | None = None

        # Load existing cached Open Platform token & Cloud session from disk
        self._load_cached_token()
        self._load_cached_session()

        # Cached data from last successful pagelist fetch
        self._cached_device_data: dict = {}
        self._cached_status: dict = {}
        self._logged_structure = False

    def _load_cached_session(self):
        """Load saved Ezviz Cloud login session (sessionId, rfSessionId) to avoid new logins."""
        try:
            if self.session_cache_path.exists():
                data = json.loads(self.session_cache_path.read_text())
                if data.get("session_id") and data.get("rf_session_id"):
                    self._cached_session = data
                    logger.info("Loaded Ezviz Cloud session from persistent cache (%s)", self.session_cache_path)
        except Exception as e:
            logger.warning("Could not read session cache from %s: %s", self.session_cache_path, e)

    def _save_cached_session(self, token_dict: dict):
        """Save Ezviz Cloud login session to disk."""
        try:
            self.session_cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "session_id": token_dict.get("session_id"),
                "rf_session_id": token_dict.get("rf_session_id"),
                "username": token_dict.get("username"),
                "api_url": token_dict.get("api_url", self.region),
                "savedAt": time.time(),
            }
            self.session_cache_path.write_text(json.dumps(payload))
            logger.info("Saved Ezviz Cloud session to persistent cache: %s", self.session_cache_path)
        except Exception as e:
            logger.warning("Could not save session cache to %s: %s", self.session_cache_path, e)

    def _load_cached_token(self):
        """Load Open Platform AccessToken and expiry from disk cache."""
        try:
            if self.token_cache_path.exists():
                data = json.loads(self.token_cache_path.read_text())
                cached_token = data.get("accessToken")
                expire_time = float(data.get("expireTime", 0))
                # Only use if at least 2 hours left before expiration
                if cached_token and expire_time > time.time() + 7200:
                    self.access_token = cached_token
                    self._open_token_expire_time = expire_time
                    remaining_hours = (expire_time - time.time()) / 3600.0
                    logger.info("Loaded valid Open Platform AccessToken from disk cache (expires in %.1f hours)", remaining_hours)
        except Exception as e:
            logger.warning("Could not read token cache from %s: %s", self.token_cache_path, e)

    def _save_cached_token(self, token: str, expire_time: float):
        """Save Open Platform AccessToken and expiry to disk cache."""
        try:
            self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "accessToken": token,
                "expireTime": expire_time,
                "savedAt": time.time(),
            }
            self.token_cache_path.write_text(json.dumps(payload))
            logger.info("Saved Open Platform AccessToken to persistent cache: %s", self.token_cache_path)
        except Exception as e:
            logger.warning("Could not save token cache to %s: %s", self.token_cache_path, e)

    def get_open_access_token(self) -> str:
        """Get a valid Open Platform AccessToken, refreshing via AppKey/AppSecret only if expired."""
        if self.access_token and time.time() < (self._open_token_expire_time - 7200):
            return self.access_token

        if not self.app_key or not self.app_secret:
            return self.access_token

        try:
            url = f"{self.open_domain}/api/lapp/token/get"
            logger.info("Requesting fresh Open Platform AccessToken from %s...", url)
            resp = requests.post(
                url,
                data={"appKey": self.app_key, "appSecret": self.app_secret},
                timeout=10,
                verify=False
            )
            data = resp.json()
            if data.get("code") == "200" and data.get("data", {}).get("accessToken"):
                self.access_token = data["data"]["accessToken"]
                expire_ms = data["data"].get("expireTime", 0)
                if expire_ms:
                    self._open_token_expire_time = expire_ms / 1000.0
                else:
                    self._open_token_expire_time = time.time() + (7 * 24 * 3600)
                self._save_cached_token(self.access_token, self._open_token_expire_time)
                logger.info("Open Platform AccessToken refreshed and cached successfully")
                return self.access_token
            else:
                logger.error("Failed to refresh Open Platform token: %s", data)
        except Exception as e:
            logger.error("Error refreshing Open Platform token: %s", e)

        return self.access_token

    def get_hls_stream_url(self, start_time: str | None = None, stop_time: str | None = None) -> str | None:
        """Get HLS (.m3u8) stream URL for live or playback of SD records."""
        token = self.get_open_access_token()
        if not token:
            logger.warning("No Open Platform AccessToken available for HLS stream")
            return None

        url = f"{self.open_domain}/api/lapp/v2/live/address/get"
        payload = {
            "accessToken": token,
            "deviceSerial": self.camera_serial,
            "channelNo": 1,
            "protocol": 2, # HLS
        }
        if start_time and stop_time:
            payload["type"] = 2 # Playback
            payload["startTime"] = start_time
            payload["stopTime"] = stop_time

        try:
            logger.info("Requesting HLS stream URL from Ezviz Open Platform (type=%s, start=%s, stop=%s)...",
                        payload.get("type", 1), start_time, stop_time)
            resp = requests.post(url, data=payload, timeout=12, verify=False)
            data = resp.json()
            if data.get("code") == "200" and data.get("data", {}).get("url"):
                stream_url = data["data"]["url"]
                logger.info("Successfully obtained HLS stream URL: %s", stream_url)
                return stream_url
            else:
                logger.error("Ezviz Open Platform returned error for HLS stream: %s", data)
                # If token expired, invalidate and retry once
                if data.get("code") in ["10002", 10002]:
                    self._open_token_expire_time = 0
                    if self.app_key and self.app_secret:
                        logger.info("Retrying with new Open Platform token...")
                        payload["accessToken"] = self.get_open_access_token()
                        retry_resp = requests.post(url, data=payload, timeout=12, verify=False)
                        retry_data = retry_resp.json()
                        if retry_data.get("code") == "200" and retry_data.get("data", {}).get("url"):
                            return retry_data["data"]["url"]
        except Exception as e:
            logger.error("Error requesting HLS stream address: %s", e)

        return None

    def get_event_record_info(self, alarm_time_str: str) -> dict | None:
        """
        Query /api/lapp/video/by/time to find the autonomous video recording
        (CloudPlay recType=1 or MicroSD recType=2) matching the alarm timestamp.
        Note: Ezviz /api/lapp/video/by/time encodes the camera's local wall-clock
        time as UTC epoch milliseconds.
        """
        token = self.get_open_access_token()
        if not token or not alarm_time_str:
            return None

        try:
            ev_dt_wall = datetime.strptime(alarm_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            ev_ms = int(ev_dt_wall.timestamp() * 1000)
        except Exception as e:
            logger.warning("get_event_record_info: invalid alarm_time_str '%s': %s", alarm_time_str, e)
            return None

        url = f"{self.open_domain}/api/lapp/video/by/time"
        search_start = ev_ms - 90000
        search_end = ev_ms + 180000

        # Try CloudPlay (recType=1) first, then Local MicroSD (recType=2)
        for rec_type in (1, 2):
            payload = {
                "accessToken": token,
                "deviceSerial": self.camera_serial,
                "channelNo": 1,
                "startTime": search_start,
                "endTime": search_end,
                "recType": rec_type,
            }
            try:
                resp = requests.post(url, data=payload, timeout=10, verify=False)
                data = resp.json()
                if data.get("code") in ["10002", 10002] and self.app_key and self.app_secret:
                    self._open_token_expire_time = 0
                    token = self.get_open_access_token()
                    payload["accessToken"] = token
                    resp = requests.post(url, data=payload, timeout=10, verify=False)
                    data = resp.json()

                if data.get("code") != "200":
                    continue

                records = data.get("data") or []
                if not isinstance(records, list) or not records:
                    continue

                best_rec = None
                best_diff = 999999999
                for item in records:
                    if not isinstance(item, dict):
                        continue
                    st_ms = int(item.get("startTime") or 0)
                    et_ms = int(item.get("endTime") or 0)
                    if not st_ms or not et_ms or et_ms <= st_ms:
                        continue
                    # Match if alarm time is within [startTime - 25s, endTime + 25s] or startTime is within [-30s, +45s]
                    diff = abs(st_ms - ev_ms)
                    in_window = (st_ms - 25000) <= ev_ms <= (et_ms + 25000) or (-30000 <= (st_ms - ev_ms) <= 45000)
                    if in_window and diff < best_diff:
                        best_diff = diff
                        best_rec = item

                if best_rec:
                    st_ms = int(best_rec["startTime"])
                    et_ms = int(best_rec["endTime"])
                    start_str = datetime.fromtimestamp(st_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    stop_str = datetime.fromtimestamp(et_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    duration_s = max(5, min(60, int(round((et_ms - st_ms) / 1000.0))))
                    info = {
                        "rec_type": rec_type,
                        "addr_type": 3 if rec_type == 1 else 2,
                        "start_ms": st_ms,
                        "end_ms": et_ms,
                        "start_time": start_str,
                        "stop_time": stop_str,
                        "duration_s": duration_s,
                        "file_id": best_rec.get("fileId"),
                    }
                    logger.info(
                        "Found autonomous event recording (recType=%d) for alarm %s: %s -> %s (%ds)",
                        rec_type, alarm_time_str, start_str, stop_str, duration_s
                    )
                    return info
            except Exception as e:
                logger.warning("Error querying /api/lapp/video/by/time (recType=%d): %s", rec_type, e)

        return None

    def get_playback_stream_url(
        self,
        start_time: str,
        stop_time: str,
        addr_type: int = 3,
        protocol: int = 3,
    ) -> str | None:
        """
        Get OpenPlayback (/v3/openpb/) stream URL for a recorded event clip.
        - addr_type: 3 = CloudPlay playback, 2 = MicroSD playback
        - protocol:  3 = RTMP (clean EOF on clip end), 4 = HTTP-FLV
        Note: protocol=2 (HLS) on Ezviz OpenAPI returns /v3/openlive/ (live feed)
        instead of /v3/openpb/ (playback), so RTMP (3) or FLV (4) must be used.
        """
        token = self.get_open_access_token()
        if not token:
            logger.warning("No Open Platform AccessToken available for playback stream")
            return None

        url = f"{self.open_domain}/api/lapp/v2/live/address/get"
        payload = {
            "accessToken": token,
            "deviceSerial": self.camera_serial,
            "channelNo": 1,
            "protocol": protocol,
            "type": addr_type,
            "startTime": start_time,
            "stopTime": stop_time,
        }

        try:
            logger.info(
                "Requesting playback stream URL (type=%d, proto=%d, start=%s, stop=%s)...",
                addr_type, protocol, start_time, stop_time
            )
            resp = requests.post(url, data=payload, timeout=12, verify=False)
            data = resp.json()
            if data.get("code") in ["10002", 10002] and self.app_key and self.app_secret:
                self._open_token_expire_time = 0
                payload["accessToken"] = self.get_open_access_token()
                resp = requests.post(url, data=payload, timeout=12, verify=False)
                data = resp.json()

            if data.get("code") == "200" and (data.get("data") or {}).get("url"):
                stream_url = data["data"]["url"]
                logger.info("Successfully obtained playback stream URL: %s", stream_url)
                return stream_url
            else:
                logger.warning("Ezviz Open Platform returned error for playback stream: %s", data)
        except Exception as e:
            logger.error("Error requesting playback stream address: %s", e)

        return None



    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """Authenticate with the Ezviz Cloud API."""
        with self._lock:
            return self._login_locked()

    def _login_locked(self) -> bool:
        """Login (must be called while holding _lock)."""
        try:
            from pyezvizapi import EzvizClient as _EzvizClient
        except ImportError as e:
            raise EzvizClientError(
                "pyezvizapi is not installed. Check requirements.txt."
            ) from e

        logger.info("Authenticating with Ezviz Cloud (%s)...", self.region)
        try:
            # 1. Try to reuse or refresh cached session if available
            if self._cached_session and self._cached_session.get("session_id"):
                try:
                    logger.info("Attempting session resumption with cached token...")
                    client = _EzvizClient(
                        account=self.username,
                        password=self.password,
                        url=self.region,
                        token=self._cached_session,
                    )
                    tok = client.login()
                    if tok and tok.get("session_id"):
                        self._client = client
                        self._last_login = datetime.now(timezone.utc)
                        self._save_cached_session(tok)
                        logger.info("Ezviz session restored and refreshed successfully (no new login device created)")
                        return True
                except Exception as re:
                    logger.warning("Session resumption failed (%s), performing clean login...", re)
                    self._cached_session = None

            # 2. Full login with credentials
            client = _EzvizClient(
                account=self.username,
                password=self.password,
                url=self.region,
            )
            tok = client.login()
            self._client = client
            self._last_login = datetime.now(timezone.utc)
            if tok and isinstance(tok, dict):
                self._save_cached_session(tok)
            logger.info("Ezviz authentication successful")
            return True

        except Exception as e:
            error_msg = str(e)
            logger.error("Ezviz authentication failed: %s", error_msg)
            if "verification" in error_msg.lower() or "captcha" in error_msg.lower():
                raise EzvizAuthError(
                    "Two-factor authentication or CAPTCHA required. "
                    "Please log in via the Ezviz app once to clear it."
                ) from e
            raise EzvizAuthError(f"Login failed: {error_msg}") from e

    def _ensure_authenticated(self):
        """Ensure we have a valid session. Must hold _lock."""
        if self._client is None or (
            self._last_login
            and datetime.now(timezone.utc) - self._last_login
            > timedelta(hours=self.TOKEN_EXPIRY_HOURS)
        ):
            logger.info("Session expired or not initialized, re-authenticating...")
            self._login_locked()

    # ------------------------------------------------------------------
    # Safe pagelist fetch (works around HP2 'str' has no attr 'get')
    # ------------------------------------------------------------------

    def _safe_get_page_list(self) -> dict:
        """
        Fetch the pagelist via pyezvizapi's internal API and return the raw dict.
        This is the same as client._get_page_list() but we catch errors.
        """
        self._ensure_authenticated()
        try:
            data = self._client._get_page_list()
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error("_get_page_list failed: %s", e)
            return {}

    def _safe_get_device_data(self, serial: str) -> dict:
        """
        Build device data dict for the given serial from the raw pagelist.
        This is a safe reimplementation of get_device_infos() that handles
        the HP2's non-standard CLOUD section (strings instead of dicts).
        """
        pages = self._safe_get_page_list()
        if not pages:
            if self._cached_device_data:
                logger.warning("Pagelist fetch failed, using cached device data")
                return self._cached_device_data
            return {}

        # Find the device in deviceInfos
        device_info = None
        for dev in pages.get("deviceInfos", []) or []:
            if isinstance(dev, dict) and dev.get("deviceSerial") == serial:
                device_info = dev
                break

        if device_info is None:
            logger.warning("Device %s not found in pagelist deviceInfos", serial)
            # List available devices for debugging
            available = []
            for dev in pages.get("deviceInfos", []) or []:
                if isinstance(dev, dict):
                    available.append(f"{dev.get('deviceSerial')} ({dev.get('name', '?')})")
            if available:
                logger.info("Available devices: %s", ", ".join(available))
            return {}

        # Safely extract sections — each section might be keyed by serial or resource ID
        def safe_get(section_name: str, key: str) -> dict:
            section = pages.get(section_name)
            if not isinstance(section, dict):
                return {}
            val = section.get(key)
            return val if isinstance(val, dict) else {}

        result = {
            "deviceInfos": device_info,
            "STATUS": safe_get("STATUS", serial),
            "CONNECTION": safe_get("CONNECTION", serial),
            "P2P": safe_get("P2P", serial),
            "KMS": safe_get("KMS", serial),
            "QOS": safe_get("QOS", serial),
            "NODISTURB": safe_get("NODISTURB", serial),
            "FEATURE": safe_get("FEATURE", serial),
            "UPGRADE": safe_get("UPGRADE", serial),
            "FEATURE_INFO": safe_get("FEATURE_INFO", serial),
            "SWITCH": safe_get("SWITCH", serial),
            "WIFI": safe_get("WIFI", serial),
            "TIME_PLAN": safe_get("TIME_PLAN", serial),
        }

        # Parse supportExt if it's a JSON string
        support_ext = device_info.get("supportExt")
        if isinstance(support_ext, str) and support_ext:
            try:
                result["deviceInfos"]["supportExt"] = json.loads(support_ext)
            except (ValueError, TypeError):
                pass

        # Parse optionals if it's a JSON string (common in STATUS)
        optionals = result["STATUS"].get("optionals")
        if isinstance(optionals, str) and optionals:
            try:
                result["STATUS"]["optionals"] = json.loads(optionals)
            except (ValueError, TypeError):
                pass
        elif isinstance(optionals, dict):
            # Recursively parse any string values that are JSON
            for k, v in list(optionals.items()):
                if isinstance(v, str):
                    try:
                        optionals[k] = json.loads(v)
                    except (ValueError, TypeError):
                        pass

        self._cached_device_data = result

        # Log device data structure on first successful fetch for debugging
        if not hasattr(self, '_logged_structure'):
            self._logged_structure = True
            logger.info("=== Pagelist top-level keys: %s ===", list(pages.keys()))
            logger.info("=== deviceInfos fields for %s: %s ===",
                        serial, list(device_info.keys()) if isinstance(device_info, dict) else "N/A")
            # Log all string values in deviceInfos that might be URLs
            for k, v in device_info.items():
                if isinstance(v, str) and (v.startswith("http") or "pic" in k.lower() or "url" in k.lower() or "image" in k.lower()):
                    logger.info("  deviceInfos[%s] = %s", k, v[:200])
            # Log STATUS keys
            logger.info("=== STATUS fields: %s ===",
                        list(result["STATUS"].keys()) if isinstance(result["STATUS"], dict) else "N/A")
            # Log any URL fields in STATUS
            if isinstance(result["STATUS"], dict):
                for k, v in result["STATUS"].items():
                    if isinstance(v, str) and (v.startswith("http") or "pic" in k.lower() or "url" in k.lower()):
                        logger.info("  STATUS[%s] = %s", k, v[:200])

        return result

    # ------------------------------------------------------------------
    # Device status
    # ------------------------------------------------------------------

    def get_device_status(self) -> dict:
        """Return a dict with device status information."""
        with self._lock:
            try:
                device = self._safe_get_device_data(self.camera_serial)
                if not device:
                    raise EzvizDeviceError(
                        f"Failed to fetch device data for {self.camera_serial} (network timeout or not in account)"
                    )

                dev_info = device.get("deviceInfos", {})
                status = device.get("STATUS", {})
                connection = device.get("CONNECTION", {})
                optionals = status.get("optionals", {})
                if not isinstance(optionals, dict):
                    optionals = {}

                # Parse SWITCH list into a dict
                switch_data = device.get("SWITCH")
                switches = {}
                if isinstance(switch_data, list):
                    for item in switch_data:
                        if isinstance(item, dict):
                            t = item.get("type")
                            en = item.get("enable")
                            if t is not None:
                                switches[int(t)] = bool(en)
                elif isinstance(switch_data, dict):
                    switches = switch_data

                # Alarm info
                last_alarm_pic = ""
                last_alarm_time = ""
                last_alarm_type = ""
                try:
                    alarm_resp = self._client.get_alarminfo(
                        serial=self.camera_serial, limit=1
                    )
                    alarm_list = (
                        alarm_resp.get("alarmList")
                        or alarm_resp.get("page", {}).get("alarmList")
                        or []
                    )
                    if isinstance(alarm_list, list) and alarm_list:
                        latest = alarm_list[0]
                        last_alarm_pic = latest.get("alarmPicUrl", "")
                        last_alarm_time = latest.get("alarmStartTimeStr", "")
                        last_alarm_type = latest.get("sampleName") or latest.get(
                            "alarmType", ""
                        )
                except Exception as e:
                    logger.debug("Alarm info fetch failed: %s", e)

                result = {
                    "serial": self.camera_serial,
                    "name": dev_info.get("name", "HP2"),
                    "online": dev_info.get("status") == 1,
                    "status_code": dev_info.get("status"),
                    "battery_level": optionals.get("powerRemaining"),
                    "local_ip": connection.get("localIp") or dev_info.get("localIp"),
                    "wan_ip": connection.get("netIp"),
                    "version": dev_info.get("version", ""),
                    "device_category": dev_info.get("deviceCategory"),
                    "device_sub_category": dev_info.get("deviceSubCategory"),
                    "alarm_notify": bool(status.get("globalStatus")),
                    "alarm_sound_mod": status.get("alarmSoundMode"),
                    "encrypted": bool(status.get("isEncrypt")),
                    "local_rtsp_port": connection.get("localRtspPort", "0"),
                    "last_alarm_time": last_alarm_time,
                    "last_alarm_pic": last_alarm_pic,
                    "last_alarm_type": last_alarm_type,
                    "motion_trigger": bool(status.get("pirStatus")),
                    "pir_status": status.get("pirStatus"),
                    "is_sleeping": bool(switches.get(21, False)),
                    "mac_address": dev_info.get("mac"),
                    "supported_channels": dev_info.get("channelNumber"),
                    "battery_work_mode": optionals.get("batteryCameraWorkMode"),
                    "upgrade_available": device.get("UPGRADE", {}).get("isNeedUpgrade")
                    == 3,
                }
                self._cached_status = result
                return result

            except EzvizDeviceError:
                raise
            except Exception as e:
                logger.error("get_device_status failed: %s\n%s", e, traceback.format_exc())
                self._client = None
                raise EzvizDeviceError(f"Status fetch failed: {e}") from e

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def get_snapshot(self) -> bytes | None:
        """
        Fetch the latest snapshot image.
        Strategies (tried in order):
        0. picUrl from deviceInfos in pagelist (device cover image)
        1. last_alarm_pic from alarminfo API
        2. capture_picture API (wakes camera)
        3. device_messages_list for recent event images
        4. cached alarm pic from last status call
        """
        with self._lock:
            try:
                self._ensure_authenticated()

                # Strategy 0: Device cover image (picUrl in deviceInfos)
                device_pic = self._get_device_pic_url()
                if device_pic:
                    logger.info("Strategy 0: trying device picUrl: %s", device_pic[:120])
                    img = self._download_image(device_pic)
                    if img:
                        logger.info("Snapshot from device picUrl: %d bytes", len(img))
                        return img
                    else:
                        logger.info("Strategy 0: device picUrl download failed or empty")
                else:
                    logger.info("Strategy 0: no picUrl in device data")

                # Strategy 1: Get last alarm picture from alarminfo API
                pic_url = self._get_latest_alarm_pic()
                if pic_url:
                    logger.info("Strategy 1: trying alarm pic: %s", pic_url[:120])
                    img = self._download_image(pic_url)
                    if img:
                        logger.info("Snapshot from alarm pic: %d bytes", len(img))
                        return img
                    else:
                        logger.info("Strategy 1: alarm pic download failed or empty")
                else:
                    logger.info("Strategy 1: no alarm pic URL available")

                # Strategy 2: Try capture_picture API
                try:
                    logger.info("Strategy 2: trying capture_picture API...")
                    result = self._client.capture_picture(
                        serial=self.camera_serial, channel=1
                    )
                    logger.info("Strategy 2: capture_picture returned: %s",
                                str(result)[:300] if result else "None")
                    if isinstance(result, dict):
                        # Extract picUrl from various response formats:
                        # Format 1: {"captureInfo": {"picUrl": "..."}}
                        # Format 2: {"picUrl": "..."}
                        # Format 3: {"data": {"picUrl": "..."}}
                        cap_url = ""
                        capture_info = result.get("captureInfo")
                        if isinstance(capture_info, dict):
                            cap_url = capture_info.get("picUrl", "")
                        if not cap_url:
                            cap_url = result.get("picUrl", "")
                        if not cap_url:
                            data = result.get("data")
                            if isinstance(data, dict):
                                cap_url = data.get("picUrl", "")

                        if cap_url and cap_url.startswith("http"):
                            logger.info("Strategy 2: downloading from %s", cap_url[:120])
                            img = self._download_image(cap_url)
                            if img:
                                logger.info("Snapshot from capture: %d bytes", len(img))
                                return img
                            else:
                                logger.info("Strategy 2: download failed or image too small")
                        else:
                            logger.info("Strategy 2: no valid picUrl in response")
                except Exception as e:
                    logger.info("Strategy 2: capture_picture failed: %s", e)

                # Strategy 3: Device messages list
                try:
                    logger.info("Strategy 3: trying device_messages_list...")
                    messages = []
                    # Retry logic: when an event just happened, the image might take a few seconds to appear
                    for attempt in range(3):
                        msgs = self._client.get_device_messages_list(
                            serials=self.camera_serial, limit=5
                        )
                        messages = msgs.get("message") or msgs.get("messages") or []
                        
                        if isinstance(messages, list) and messages:
                            latest_msg = messages[0] if isinstance(messages[0], dict) else {}
                            msg_pic = latest_msg.get("picUrl") or latest_msg.get("alarmPicUrl") or ""
                            
                            if not msg_pic and attempt < 2:
                                logger.info("Strategy 3: newest message lacks picture, waiting 2.5s (attempt %d/3)", attempt + 1)
                                time.sleep(2.5)
                                continue
                        break
                        
                    if isinstance(messages, list):
                        logger.info("Strategy 3: found %d messages", len(messages))
                        for i, msg in enumerate(messages):
                            if not isinstance(msg, dict):
                                continue
                            msg_pic = (
                                msg.get("picUrl")
                                or msg.get("alarmPicUrl")
                                or ""
                            )
                            if msg_pic:
                                logger.info("Strategy 3: message[%d] pic: %s", i, msg_pic[:120])
                                img = self._download_image(msg_pic)
                                if img:
                                    logger.info("Snapshot from message: %d bytes", len(img))
                                    return img
                        logger.info("Strategy 3: no usable picture in messages")
                    else:
                        logger.info("Strategy 3: messages list empty or not a list")
                except Exception as e:
                    logger.info("Strategy 3: messages list failed: %s", e)

                # Strategy 4: Use cached alarm pic from last status
                cached_pic = self._cached_status.get("last_alarm_pic", "")
                if cached_pic:
                    logger.info("Strategy 4: trying cached alarm pic: %s", cached_pic[:120])
                    img = self._download_image(cached_pic)
                    if img:
                        logger.info("Snapshot from cached status: %d bytes", len(img))
                        return img
                    else:
                        logger.info("Strategy 4: cached pic download failed")
                else:
                    logger.info("Strategy 4: no cached alarm pic")

                logger.warning("All 5 snapshot strategies failed — no image available")
                return None

            except EzvizDeviceError:
                raise
            except Exception as e:
                logger.error("get_snapshot failed: %s", e)
                self._client = None
                raise EzvizDeviceError(f"Snapshot fetch failed: {e}") from e

    def _get_device_pic_url(self) -> str:
        """Get the device's cover/status picture URL from cached pagelist data."""
        dev_info = self._cached_device_data.get("deviceInfos", {})
        if not isinstance(dev_info, dict):
            return ""

        # Try various known picture URL fields
        for key in ("picUrl", "devicePicUrl", "statusPicUrl", "coverUrl"):
            url = dev_info.get(key, "")
            if url and isinstance(url, str) and url.startswith("http"):
                return url

        # Check in STATUS section
        status = self._cached_device_data.get("STATUS", {})
        if isinstance(status, dict):
            for key in ("picUrl", "devicePicUrl"):
                url = status.get(key, "")
                if url and isinstance(url, str) and url.startswith("http"):
                    return url

        return ""

    def _get_latest_alarm_pic(self) -> str:
        """Get the URL of the latest alarm picture."""
        try:
            alarm_resp = self._client.get_alarminfo(
                serial=self.camera_serial, limit=1
            )
            alarm_list = (
                alarm_resp.get("alarmList")
                or alarm_resp.get("page", {}).get("alarmList")
                or []
            )
            if isinstance(alarm_list, list) and alarm_list:
                pic = alarm_list[0].get("alarmPicUrl", "")
                if pic and pic.startswith("http"):
                    return pic
        except Exception as e:
            logger.debug("get_alarminfo failed: %s", e)
        return ""

    def _download_image(self, url: str) -> bytes | None:
        """Download an image from a URL, return bytes or None."""
        if not url or not url.startswith("http"):
            return None
        try:
            resp = requests.get(url, timeout=15)
            content_type = resp.headers.get("Content-Type", "unknown")
            content_len = len(resp.content)
            if resp.status_code == 200 and content_len > 100:
                # Check if the response looks like a real image
                is_jpeg = resp.content[:2] == b'\xff\xd8'
                is_png = resp.content[:4] == b'\x89PNG'
                logger.info("Image download: %d bytes, type=%s, jpeg=%s, png=%s",
                            content_len, content_type, is_jpeg, is_png)
                if is_jpeg or is_png or "image" in content_type.lower():
                    # Check if it might be an encrypted HIK picture in a JPEG container
                    # or if the header is present and we have a key
                    if b"hikencodepicture" in resp.content:
                        logger.info("Encrypted HIK header detected in image")
                        if self.encryption_key:
                            try:
                                from pyezvizapi.utils import decrypt_image
                                decoded = decrypt_image(resp.content, self.encryption_key)
                                if decoded.startswith(b"\xff\xd8"):
                                    logger.info("Successfully decrypted image (%d bytes)", len(decoded))
                                    return decoded
                                else:
                                    logger.warning("Decrypted data is not a JPEG. Header: %s", decoded[:20].hex())
                            except Exception as de:
                                logger.error("Image decryption failed: %s", de)
                        else:
                            logger.warning("Image is encrypted but no encryption_key provided in config")
                    return resp.content
                else:
                    # Might be encrypted or HTML error — log first bytes
                    if b"hikencodepicture" in resp.content:
                        logger.info("Encrypted HIK header detected (without image content-type)")
                        if self.encryption_key:
                            try:
                                from pyezvizapi.utils import decrypt_image
                                decoded = decrypt_image(resp.content, self.encryption_key)
                                if decoded.startswith(b"\xff\xd8"):
                                    logger.info("Successfully decrypted image from raw stream (%d bytes)", len(decoded))
                                    return decoded
                            except Exception as de:
                                logger.error("Image decryption failed (raw): %s", de)
                        else:
                             logger.warning("Raw stream is encrypted but no encryption_key provided")
                    
                    logger.warning("Downloaded data is not a recognized image format. "
                                   "First 50 bytes: %s, Content-Type: %s",
                                   resp.content[:50], content_type)
                    # Return it anyway — it might still work
                    return resp.content
            else:
                logger.info("Image download: status=%d, size=%d bytes (too small or error)",
                            resp.status_code, content_len)
        except Exception as e:
            logger.info("Image download failed from %s: %s", url[:80], e)
        return None

    # ------------------------------------------------------------------
    # Alarm / Events
    # ------------------------------------------------------------------

    def get_alarm_list(self, max_count: int = 10, s_type: str | None = None) -> list[dict]:
        """Return a list of recent alarm events. 
        s_type "92" is ALL_ALARMS, "101"/"102" is for calling/doorbell messages. 
        """
        with self._lock:
            try:
                self._ensure_authenticated()

                # Try unified messages first
                try:
                    messages = []
                    # Retry logic for newly generated alerts without pictures
                    for attempt in range(3):
                        msgs = self._client.get_device_messages_list(
                            serials=self.camera_serial,
                            limit=min(max_count, 50),
                            s_type=s_type
                        )
                        messages = msgs.get("message") or msgs.get("messages") or []
                        
                        if isinstance(messages, list) and messages:
                            latest_msg = messages[0] if isinstance(messages[0], dict) else {}
                            msg_pic = latest_msg.get("picUrl") or latest_msg.get("alarmPicUrl") or ""
                            
                            if not msg_pic and attempt < 2:
                                logger.debug("get_alarm_list: newest message lacks picture, waiting 2.5s (attempt %d/3)", attempt + 1)
                                time.sleep(2.5)
                                continue
                        break

                    result = []
                    if isinstance(messages, list):
                        for msg in messages:
                            if not isinstance(msg, dict):
                                continue
                            result.append(
                                {
                                    "alarm_id": msg.get("msgId", ""),
                                    "alarm_type": msg.get("title")
                                    or msg.get("sampleName")
                                    or msg.get("alarmType", ""),
                                    "alarm_time": msg.get("timeStr")
                                    or msg.get("msgTimeStr")
                                    or msg.get("alarmStartTimeStr")
                                    or (datetime.fromtimestamp(int(msg["time"])/1000).strftime("%Y-%m-%d %H:%M:%S") if msg.get("time") else "")
                                    or (datetime.fromtimestamp(int(msg["msgTime"])/1000).strftime("%Y-%m-%d %H:%M:%S") if msg.get("msgTime") else "")
                                    or (datetime.fromtimestamp(int(msg["alarmStartTime"])/1000).strftime("%Y-%m-%d %H:%M:%S") if msg.get("alarmStartTime") else ""),
                                    "alarm_pic_url": msg.get("pic")
                                    or msg.get("picUrl")
                                    or msg.get("alarmPicUrl", ""),
                                    "device_serial": msg.get("deviceSerial", ""),
                                }
                            )
                    if result:
                        return result
                except Exception as e:
                    logger.debug("get_device_messages_list failed: %s", e)

                # Fallback: alarminfo API
                try:
                    alarm_resp = self._client.get_alarminfo(
                        serial=self.camera_serial, limit=max_count
                    )
                    alarm_list = (
                        alarm_resp.get("alarmList")
                        or alarm_resp.get("page", {}).get("alarmList")
                        or []
                    )
                    result = []
                    if isinstance(alarm_list, list):
                        for alarm in alarm_list:
                            if not isinstance(alarm, dict):
                                continue
                            result.append(
                                {
                                    "alarm_id": alarm.get("alarmId", ""),
                                    "alarm_type": alarm.get("sampleName")
                                    or alarm.get("alarmType", ""),
                                    "alarm_time": alarm.get("alarmStartTimeStr")
                                    or (datetime.fromtimestamp(int(alarm["alarmStartTime"])/1000).strftime("%Y-%m-%d %H:%M:%S") if alarm.get("alarmStartTime") else ""),
                                    "alarm_pic_url": alarm.get("alarmPicUrl", ""),
                                    "device_serial": self.camera_serial,
                                }
                            )
                    return result
                except Exception as e:
                    logger.debug("get_alarminfo fallback failed: %s", e)

                return []

            except Exception as e:
                logger.error("get_alarm_list failed: %s", e)
                return []

    # ------------------------------------------------------------------
    # Device list
    # ------------------------------------------------------------------

    def get_all_devices(self) -> list[dict]:
        """Return a list of all devices on the account."""
        with self._lock:
            try:
                self._ensure_authenticated()
                pages = self._safe_get_page_list()
                result = []
                for dev in pages.get("deviceInfos", []) or []:
                    if not isinstance(dev, dict):
                        continue
                    serial = dev.get("deviceSerial", "")
                    status_section = pages.get("STATUS", {})
                    dev_status = (
                        status_section.get(serial, {})
                        if isinstance(status_section, dict)
                        else {}
                    )
                    optionals = dev_status.get("optionals", {})
                    if isinstance(optionals, str):
                        try:
                            optionals = json.loads(optionals)
                        except (ValueError, TypeError):
                            optionals = {}

                    result.append(
                        {
                            "serial": serial,
                            "name": dev.get("name", serial),
                            "online": dev.get("status") == 1,
                            "model": dev.get("deviceSubCategory", ""),
                            "battery_level": optionals.get("powerRemaining")
                            if isinstance(optionals, dict)
                            else None,
                        }
                    )
                return result

            except Exception as e:
                logger.error("get_all_devices failed: %s", e)
                self._client = None
                raise EzvizDeviceError(f"Device list failed: {e}") from e

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        return self._client is not None

    def invalidate_session(self) -> None:
        with self._lock:
            if self._client:
                try:
                    self._client.close_session()
                except Exception:
                    pass
            self._client = None
            logger.info("Session invalidated, will re-authenticate on next call")
