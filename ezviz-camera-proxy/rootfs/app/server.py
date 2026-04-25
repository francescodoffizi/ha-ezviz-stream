"""
Ezviz Camera Proxy — Flask Web Server
======================================
Provides:
  /                   — Dashboard (HTML)
  /api/snapshot       — Latest snapshot JPEG
  /api/snapshot/refresh — Trigger new snapshot from cloud
  /api/status         — Camera status JSON
  /api/events         — Recent alarm events JSON
  /api/stream         — MJPEG stream (simulated from periodic snapshots)

Compatible with Home Assistant Ingress.
All routes use a configurable prefix (INGRESS_ENTRY env var).
"""

import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    redirect,
    url_for,
)
import paho.mqtt.publish as publish
import paho.mqtt.client as mqtt

from ezviz_client import EzvizClient, EzvizClientError, EzvizAuthError, EzvizDeviceError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment (set by run.sh from /data/options.json)
# ---------------------------------------------------------------------------
EZVIZ_USERNAME = os.environ.get("EZVIZ_USERNAME", "")
EZVIZ_PASSWORD = os.environ.get("EZVIZ_PASSWORD", "")
EZVIZ_REGION = os.environ.get("EZVIZ_REGION", "apiieu.ezvizlife.com")
CAMERA_SERIAL = os.environ.get("CAMERA_SERIAL", "")
CAMERA_PASSWORD = os.environ.get("CAMERA_PASSWORD", "")
EZVIZ_ENCRYPTION_KEY = os.environ.get("EZVIZ_ENCRYPTION_KEY", "")
SNAPSHOT_INTERVAL = int(os.environ.get("SNAPSHOT_INTERVAL", "30"))
ENABLE_MQTT_EVENTS = os.environ.get("ENABLE_MQTT_EVENTS", "true").lower() == "true"

# Ingress support: HA sets INGRESS_ENTRY e.g. "/api/hassio_ingress/abcdef"
INGRESS_ENTRY = os.environ.get("INGRESS_ENTRY", "/").rstrip("/")

DATA_PATH = Path(os.environ.get("DATA_PATH", "/data"))
SNAPSHOT_PATH = DATA_PATH / "snapshots"
SNAPSHOT_PATH.mkdir(parents=True, exist_ok=True)

CURRENT_SNAPSHOT_FILE = SNAPSHOT_PATH / "current.jpg"
EVENT_SNAPSHOT_PATH = SNAPSHOT_PATH / "events"
EVENT_SNAPSHOT_PATH.mkdir(parents=True, exist_ok=True)


class EventStore:
    """Manages a list of recent events with deduplication and local image caching."""
    def __init__(self, max_size=20):
        self.max_size = max_size
        self.events = []
        self.lock = threading.Lock()

    def add_events(self, new_events: list[dict], download_images=True):
        """Merge new events into the store, deduplicating by alarm_id and timestamp/type."""
        with self.lock:
            # Map of ID to index and (time, type) to index
            existing_ids = {e["alarm_id"]: i for i, e in enumerate(self.events)}
            existing_keys = {(e["alarm_time"], str(e["alarm_type"])): i for i, e in enumerate(self.events)}
            
            for event in new_events:
                raw_id = event.get("alarm_id")
                if not raw_id:
                    continue
                
                ev_id = str(raw_id)
                # Normalize alarm_type ensuring it's a string name
                raw_type = event.get("alarm_type") or event.get("alarm_name") or "Event"
                
                # Map common numeric codes to readable names for better deduplication
                type_map = {
                    "10000": "Doorbell", "10001": "Doorbell", "10002": "Doorbell", "10006": "Doorbell", 
                    "10022": "Doorbell", "10054": "Doorbell", "10055": "Doorbell",
                    "1": "Motion", "11514": "Motion", "11502": "Motion"
                }
                ev_type = type_map.get(str(raw_type), str(raw_type))
                
                ev_time = event.get("alarm_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                normalized = {
                    "alarm_id": ev_id,
                    "alarm_type": ev_type,
                    "alarm_time": ev_time,
                    "alarm_pic_url": event.get("alarm_pic_url") or event.get("pic_url") or "",
                    "is_push": event.get("is_push", False),
                    "local_pic": event.get("local_pic", False)
                }

                # Deduplication logic: check ID first
                match_idx = existing_ids.get(ev_id)
                
                if match_idx is None:
                    # Check if another event has same time and type (already existed)
                    match_idx = existing_keys.get((ev_time, ev_type))
                
                if match_idx is None:
                    # IMPROVED DEDUPLICATION: Check if there's an event within 5s for the same camera
                    # regardless of type (accrued many duplicates like 'Face Detection' + 'Human Detection')
                    try:
                        dt = datetime.strptime(ev_time, "%Y-%m-%d %H:%M:%S")
                        for e_idx, e in enumerate(self.events):
                            if e.get("device_serial", "") == event.get("device_serial", ""):
                                try:
                                    e_dt = datetime.strptime(e["alarm_time"], "%Y-%m-%d %H:%M:%S")
                                    if abs((dt - e_dt).total_seconds()) <= 5:
                                        match_idx = e_idx
                                        break
                                except: continue
                    except: pass
                    
                if match_idx is not None:
                    # Update existing record
                    old_event = self.events[match_idx]
                    
                    # MERGE LOGIC:
                    # 1. Always prefer the original time if the new one is significantly different 
                    #    (unless the new one is specifically marked as more reliable, which we don't know).
                    #    Crucially, prevent "polling batch times" (multiple events getting same 'now()' time)
                    #    from overwriting distinct historical times.
                    if old_event["alarm_time"] != ev_time:
                        # If the new time was a "now" fallback in server.py (which we can't perfectly know here, 
                        # but we can guess if it differs from old_event), keep the old one.
                        # Rule: if old event has a time and new one is just "poll time", keep old.
                        if old_event["alarm_time"] and not event.get("alarm_time"):
                             normalized["alarm_time"] = old_event["alarm_time"]
                    
                    # 2. Prefer specific type names over generic ones or codes
                    generic_codes = ["10120", "12663", "event", "alarm"]
                    is_generic = lambda t: str(t).lower() in generic_codes or str(t).isdigit()
                    
                    current_type = ev_type
                    old_type = old_event["alarm_type"]
                    
                    better_types = ["face", "person", "doorbell", "call", "appeared", "human"]
                    current_is_better = any(bt in current_type.lower() for bt in better_types)
                    old_is_better = any(bt in old_type.lower() for bt in better_types)

                    if old_is_better and not current_is_better:
                        normalized["alarm_type"] = old_type
                    elif not old_is_better and current_is_better:
                        # Keep current_type
                        pass
                    elif is_generic(current_type) and not is_generic(old_type):
                        normalized["alarm_type"] = old_type
                    
                    # 3. Merge picture info: prefer non-empty URLs, prefer True local_pic
                    if not normalized["alarm_pic_url"] and old_event.get("alarm_pic_url"):
                        normalized["alarm_pic_url"] = old_event["alarm_pic_url"]
                    if not normalized["local_pic"] and old_event.get("local_pic"):
                        normalized["local_pic"] = old_event.get("local_pic", False)
                    
                    # Keep the original ID if we matched by (time, type)
                    if ev_id != old_event["alarm_id"]:
                        logger.debug("Merging duplicate event %s with existing %s (matched by time/type)", 
                                    ev_id, old_event["alarm_id"])
                        normalized["alarm_id"] = old_event["alarm_id"]
                    
                    self.events[match_idx].update(normalized)
                else:
                    self.events.append(normalized)
                    # Update maps for next iterations in the same batch
                    idx = len(self.events) - 1
                    existing_ids[ev_id] = idx
                    existing_keys[(ev_time, ev_type)] = idx

            # Sort by time descending
            self.events.sort(key=lambda x: x["alarm_time"], reverse=True)
            self.events = self.events[:self.max_size]
            
            # Prune old images from disk
            self._prune_disk()
            
        if download_images:
            # Download images in background for events that don't have local_pic
            threading.Thread(target=self._process_images, daemon=True, name="event-image-processor").start()

    def _prune_disk(self):
        """Delete images from EVENT_SNAPSHOT_PATH that are no longer in self.events."""
        try:
            current_ids = {e["alarm_id"] for e in self.events if e.get("local_pic")}
            for f in EVENT_SNAPSHOT_PATH.glob("event_*.jpg"):
                # Extract ID from event_<id>.jpg
                try:
                    f_id = f.name[6:-4]
                    if f_id not in current_ids:
                        f.unlink()
                        logger.debug("Pruned old event image: %s", f.name)
                except: continue
        except Exception as e:
            logger.error("Disk pruning failed: %s", e)

    def _process_images(self):
        """Download images for events that only have cloud URLs."""
        try:
            client = get_client()
            with self.lock:
                to_process = [e for e in self.events if e.get("alarm_pic_url") and not e.get("local_pic")]
            
            for ev in to_process:
                url = ev["alarm_pic_url"]
                if not url or not url.startswith("http"):
                    continue
                
                logger.info("Downloading event image for %s...", ev["alarm_id"])
                img_bytes = client._download_image(url)
                if img_bytes:
                    filename = f"event_{ev['alarm_id']}.jpg"
                    filepath = EVENT_SNAPSHOT_PATH / filename
                    filepath.write_bytes(img_bytes)
                    
                    with self.lock:
                        # Update event in list
                        for e in self.events:
                            if e["alarm_id"] == ev["alarm_id"]:
                                e["local_pic"] = True
                                break
                    logger.info("Saved event image: %s", filename)
                time.sleep(1) # Throttle
        except Exception as e:
            logger.error("Event image processor failed: %s", e)

    def get_all(self):
        with self.lock:
            return list(self.events)

    def get_image_list(self):
        """Return list of local image paths for the history playback loop."""
        with self.lock:
            paths = []
            for e in self.events:
                if e.get("local_pic"):
                    filename = f"event_{e['alarm_id']}.jpg"
                    path = EVENT_SNAPSHOT_PATH / filename
                    if path.exists():
                        paths.append(path)
            logger.info("History image list requested: found %d local images on disk", len(paths))
            return paths

# ---------------------------------------------------------------------------
# Flask App — use APPLICATION_ROOT for ingress prefix
# ---------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates")
app.config["APPLICATION_ROOT"] = INGRESS_ENTRY or "/"
app.config["PREFERRED_URL_SCHEME"] = "http"

# ---------------------------------------------------------------------------
# Ezviz client (singleton)
# ---------------------------------------------------------------------------
_client: EzvizClient | None = None
_client_lock = threading.Lock()
_last_status: dict = {}
_event_store = EventStore(max_size=30)
_snapshot_error: str = ""
_status_error: str = ""
_last_snapshot_time: datetime | None = None
ezviz_mqtt = None
_seen_events: set | None = None
_last_event_trigger_time: float = 0
_last_auth_fail_time: float = 0


def get_client() -> EzvizClient:
    """Return a single globally-cached EzvizClient instance."""
    global _client, _last_auth_fail_time
    
    # If we failed auth recently, don't even try for 60 seconds to avoid "terminal limit"
    if time.time() - _last_auth_fail_time < 60:
        logger.warning("Auth cooling period active (60s). Skipping terminal-heavy login attempt.")
        if _client:
             return _client
        raise EzvizAuthError("Auth cooling period active")

    with _client_lock:
        if _client is None:
            _client = EzvizClient(
                username=EZVIZ_USERNAME,
                password=EZVIZ_PASSWORD,
                region=EZVIZ_REGION,
                camera_serial=CAMERA_SERIAL,
                camera_password=CAMERA_PASSWORD,
                encryption_key=EZVIZ_ENCRYPTION_KEY
            )
        return _client

def handle_auth_error(e):
    """Update auth failure timing to trigger cooling period."""
    global _last_auth_fail_time
    _last_auth_fail_time = time.time()
    logger.error("Authentication failed, cooling down: %s", e)


# ---------------------------------------------------------------------------
# Background snapshot poller & Real-time MQTT setup
# ---------------------------------------------------------------------------

def _fetch_snapshot_on_event():
    """Trigger an immediate snapshot asynchronously when motion/doorbell is pushed."""
    global _last_snapshot_time, _snapshot_error
    try:
        logger.info("⚡ Event detected! Forcing immediate snapshot fetch...")
        client = get_client()
        img_bytes = client.get_snapshot()
        if img_bytes:
            with open(CURRENT_SNAPSHOT_FILE, "wb") as f:
                f.write(img_bytes)
            _last_snapshot_time = datetime.now(timezone.utc)
            _snapshot_error = ""
            logger.debug("Event-driven snapshot saved (%d bytes)", len(img_bytes))
        else:
            logger.warning("Event-driven snapshot returned empty data")
    except Exception as e:
        logger.error("Failed to fetch snapshot on event: %s", e)

def _on_ezviz_push_message(msg):
    """Callback triggered instantly by Ezviz Cloud when motion/doorbell occurs."""
    if not ENABLE_MQTT_EVENTS:
        return
    
    logger.info("⚡ Real-time Push Message received: %s", json.dumps(msg))
    
    ext = msg.get("ext", {})
    # Try multiple ways to get an event ID to avoid duplicates
    ev_id = msg.get("id") or ext.get("msgId") or msg.get("extras", {}).get("ticket")
    
    # Capture the exact time from the push message if available
    push_time = ext.get("time")
    if not push_time:
        push_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not ev_id:
        # Fallback to a hash of alert text and time if ID is missing
        ev_id = f"push_{msg.get('alert', 'Event')}_{push_time}".replace(" ", "_").replace(":", "-")
    
    global _seen_events
    if _seen_events is None:
        _seen_events = set()
    
    if ev_id not in _seen_events:
        _seen_events.add(ev_id)
        logger.info("⚡ New event %s detected (type: %s). Processing...", ev_id, ext.get("alert_type_code"))

        # Trigger snapshot fetch IMMEDIATELY (independent of MQTT publish)
        global _last_event_trigger_time
        now = time.time()
        if now - _last_event_trigger_time > 15:
            _last_event_trigger_time = now
            threading.Thread(target=_fetch_snapshot_on_event, daemon=True, name="event-snapshot-worker").start()
        else:
            logger.debug("⚡ Event debounce active (last snapshot was %.1fs ago)", now - _last_event_trigger_time)

        # Publish to local HA MQTT if possible
        mqtt_host = os.environ.get("MQTT_HOST")
        if not mqtt_host:
            logger.info("⚠️ MQTT_HOST not set, skipping local MQTT publish")
            return

        mqtt_port = int(os.environ.get("MQTT_PORT", 1883))
        mqtt_user = os.environ.get("MQTT_USER", "")
        mqtt_pass = os.environ.get("MQTT_PASSWORD", "")
        auth = {'username': mqtt_user, 'password': mqtt_pass} if mqtt_user else None
        
        # Determine event type
        alert_code = int(ext.get("alert_type_code", 0))
        # Doorbell codes: 10000, 10006, 10022 etc.
        is_doorbell = alert_code in [10000, 10001, 10002, 10006, 10022, 10054, 10055]
        event_type = "doorbell" if is_doorbell else "motion"
        
        logger.info("⚡ Push Event: code=%s, type=%s, msg=%s", alert_code, event_type, msg.get("alert", "N/A"))
        
        main_topic = f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{event_type}/state"
        global_topic = f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_alarm/state"
        user_topic = f"homeassistant/camera/ezviz/{CAMERA_SERIAL}/{event_type}"
        
        try:
            # Publish 'ON' and Attributes to all relevant topics
            for t in [main_topic, user_topic, global_topic]:
                publish.single(t, "ON", hostname=mqtt_host, port=mqtt_port, auth=auth)
            
            # Attributes topic - Flatten 'ext' into top-level
            attr_data = msg.copy()
            if "ext" in attr_data and isinstance(attr_data["ext"], dict):
                attr_data.update(attr_data.pop("ext"))
            
            attr_topic = main_topic.replace("/state", "/attributes")
            publish.single(attr_topic, json.dumps(attr_data), hostname=mqtt_host, port=mqtt_port, auth=auth)
            
            global_attr_topic = global_topic.replace("/state", "/attributes")
            publish.single(global_attr_topic, json.dumps(attr_data), hostname=mqtt_host, port=mqtt_port, auth=auth)
            
            logger.info("⚡ Real-time Push: Published %s event (code %s)", event_type, alert_code)
            
            # Reset to 'OFF' after 5 seconds
            def _reset_mqtt():
                time.sleep(5)
                try:
                    for t in [main_topic, user_topic, global_topic]:
                        publish.single(t, "OFF", hostname=mqtt_host, port=mqtt_port, auth=auth)
                except: pass
            threading.Thread(target=_reset_mqtt, daemon=True).start()

        except Exception as e:
            logger.error("⚡ Real-time Push failed to publish to HA MQTT: %s", e)

        # Update the local events list via EventStore
        # We pass it as a list, and EventStore will handle its own background image processing
        _event_store.add_events([{
            "alarm_id": ev_id,
            "alarm_type": alert_code,
            "alarm_name": msg.get("alert", "Event"),
            "alarm_time": push_time,
            "pic_url": msg.get("image", ""),
            "is_push": True
        }])

        # Trigger a cloud event fetch in 5 seconds to get the proper cloud picURL if it was missing 
        # (Ezviz push messages often lack the image URL immediately)
        def _deferred_event_refresh():
            time.sleep(5)
            try:
                client = get_client()
                polled = client.get_alarm_list(max_count=5)
                if polled:
                    _event_store.add_events(polled)
            except: pass
        threading.Thread(target=_deferred_event_refresh, daemon=True).start()


def _send_mqtt_discovery():
    """Send MQTT Discovery config to Home Assistant so sensors appear automatically."""
    mqtt_host = os.environ.get("MQTT_HOST")
    if not mqtt_host:
        logger.warning("⚠️ MQTT_HOST not set, cannot send Discovery config")
        return
    
    mqtt_port = int(os.environ.get("MQTT_PORT", 1883))
    mqtt_user = os.environ.get("MQTT_USER", "")
    mqtt_pass = os.environ.get("MQTT_PASSWORD", "")
    auth = {'username': mqtt_user, 'password': mqtt_pass} if mqtt_user else None

    device_info = {
        "identifiers": [f"ezviz_{CAMERA_SERIAL}"],
        "name": f"Ezviz Camera {CAMERA_SERIAL}",
        "model": "Ezviz Proxy",
        "manufacturer": "Ezviz"
    }

    sensors = [
        ("motion", "Movimento", "motion"),
        ("doorbell", "Campanello", "occupancy"),
        ("alarm", "Allarme", "problem")
    ]

    for s_type, s_name, s_class in sensors:
        config_topic = f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{s_type}/config"
        payload = {
            "name": s_name,
            "state_topic": f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{s_type}/state",
            "device_class": s_class,
            "unique_id": f"ezviz_{CAMERA_SERIAL}_{s_type}",
            "device": device_info,
            "payload_on": "ON",
            "payload_off": "OFF",
            "json_attributes_topic": f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{s_type}/attributes"
        }
        try:
            publish.single(config_topic, json.dumps(payload), hostname=mqtt_host, port=mqtt_port, auth=auth, retain=True)
            logger.info("⚡ MQTT Discovery: Sent config for %s", s_type)
        except Exception as e:
            logger.error("Failed to send MQTT discovery for %s: %s", s_type, e)

    # Add refresh button
    btn_topic = f"homeassistant/button/ezviz_{CAMERA_SERIAL}_refresh/config"
    btn_payload = {
        "name": "Aggiorna Snapshot",
        "command_topic": f"homeassistant/button/ezviz_{CAMERA_SERIAL}_refresh/set",
        "unique_id": f"ezviz_{CAMERA_SERIAL}_refresh",
        "device": device_info,
        "icon": "mdi:refresh",
        "payload_press": "PRESS",
        "entity_category": "config"
    }
    try:
        publish.single(btn_topic, json.dumps(btn_payload), hostname=mqtt_host, port=mqtt_port, auth=auth, retain=True)
        logger.info("⚡ MQTT Discovery: Sent config for refresh button")
    except Exception as e:
        logger.error("Failed to send MQTT discovery for refresh button: %s", e)


def _local_mqtt_worker():
    """Background thread: listen for commands from local Home Assistant MQTT."""
    mqtt_host = os.environ.get("MQTT_HOST")
    if not mqtt_host:
        logger.warning("⚠️ MQTT_HOST not set, local MQTT listener disabled")
        return

    mqtt_port = int(os.environ.get("MQTT_PORT", 1883))
    mqtt_user = os.environ.get("MQTT_USER", "")
    mqtt_pass = os.environ.get("MQTT_PASSWORD", "")
    
    # Use a specific client ID to avoid collisions
    client_id = f"ezviz_proxy_{CAMERA_SERIAL}_listener"
    client = mqtt.Client(client_id=client_id, clean_session=True)
    
    if mqtt_user:
        client.username_pw_set(mqtt_user, mqtt_pass)

    command_topic = f"homeassistant/button/ezviz_{CAMERA_SERIAL}_refresh/set"

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info("⚡ Connected to local MQTT broker, subscribing to %s", command_topic)
            client.subscribe(command_topic)
        else:
            logger.error("Failed to connect to local MQTT broker, rc=%s", rc)

    def on_message(client, userdata, msg):
        payload = msg.payload.decode().upper()
        logger.info("⚡ Received MQTT command on %s: %s", msg.topic, payload)
        if msg.topic == command_topic and payload == "PRESS":
            logger.info("⚡ Refresh command received via MQTT. Triggering snapshot...")
            # Use the existing event-driven fetch helper
            threading.Thread(target=_fetch_snapshot_on_event, daemon=True, name="mqtt-refresh-worker").start()

    client.on_connect = on_connect
    client.on_message = on_message

    logger.info("Starting local MQTT listener for commands...")
    try:
        client.connect(mqtt_host, mqtt_port, 60)
        client.loop_forever()
    except Exception as e:
        logger.error("Local MQTT listener error: %s", e)


def _snapshot_worker():
    """Background thread: fetch a new snapshot every SNAPSHOT_INTERVAL seconds."""
    global _last_status, _snapshot_error, _status_error, _last_snapshot_time, _seen_events, ezviz_mqtt

    logger.info("Snapshot worker started (interval=%ds)", SNAPSHOT_INTERVAL)
    # Initial delay to allow app startup
    time.sleep(5)

    consecutive_errors = 0
    ezviz_mqtt = None

    while True:
        try:
            client = get_client()

            # Ensure logged in
            if not client.is_connected():
                logger.info("Snapshot worker: logging in...")
                client.login()
                
                # Send MQTT discovery once per session
                _send_mqtt_discovery()

                # Start real-time push listener once logged in
                if ENABLE_MQTT_EVENTS and ezviz_mqtt is None:
                    try:
                        logger.info("⚡ Subscribing to Ezviz Cloud real-time push events...")
                        ezviz_mqtt = client._client.get_mqtt_client(on_message_callback=_on_ezviz_push_message)
                        ezviz_mqtt.connect()
                    except Exception as mqtt_err:
                        logger.error("Failed to start Ezviz Cloud MQTT push: %s", mqtt_err)

            # Fetch status first (lighter call, also provides last_alarm_pic)
            try:
                _last_status = client.get_device_status()
                _status_error = ""
                consecutive_errors = 0
                logger.info("Status updated: online=%s, battery=%s, alarm_pic=%s",
                            _last_status.get('online'),
                            _last_status.get('battery_level'),
                            _last_status.get('last_alarm_pic', '')[:80] or 'none')
            except EzvizDeviceError as e:
                _status_error = str(e)
                logger.error("Status fetch failed: %s", e)
                consecutive_errors += 1

            # Fetch snapshot ONLY if polling interval > 0
            if SNAPSHOT_INTERVAL > 0:
                try:
                    img_bytes = client.get_snapshot()
                    if img_bytes:
                        with open(CURRENT_SNAPSHOT_FILE, "wb") as f:
                            f.write(img_bytes)
                        _last_snapshot_time = datetime.now(timezone.utc)
                        _snapshot_error = ""
                        consecutive_errors = 0
                        logger.debug("Snapshot saved (%d bytes)", len(img_bytes))
                    else:
                        _snapshot_error = "Snapshot returned empty data — camera may be in sleep mode"
                        logger.warning(_snapshot_error)
                except EzvizDeviceError as e:
                    _snapshot_error = str(e)
                    logger.error("Snapshot failed: %s", e)
                    consecutive_errors += 1

            # Fetch recent events ANYWAY (light cloud call, doesn't wake camera)
            try:
                polled_events = client.get_alarm_list(max_count=20)
                if polled_events:
                    _event_store.add_events(polled_events)
            except Exception as e:
                logger.error("Event fetch failed: %s", e)
        except EzvizAuthError as e:
            _snapshot_error = f"Auth error: {e}"
            handle_auth_error(e)
            consecutive_errors += 1
            # Force re-login on next cycle
            client = get_client()
            client.invalidate_session()
            logger.warning("Authentication failed in worker. Cooling down for 5 minutes...")
            time.sleep(300) 
            continue

        except Exception as e:
            _snapshot_error = f"Unexpected error: {e}"
            logger.exception("Unexpected error in snapshot worker: %s", e)
            consecutive_errors += 1

        # Back off if we're seeing many consecutive errors
        if consecutive_errors > 5:
            base_interval = SNAPSHOT_INTERVAL if SNAPSHOT_INTERVAL > 0 else 60
            backoff = min(300, base_interval * consecutive_errors)
            logger.warning("Many consecutive errors (%d), backing off %ds",
                          consecutive_errors, backoff)
            time.sleep(backoff)
        else:
            # Sleep SNAPSHOT_INTERVAL, but at least every 300s (5m) for status if snapshots are off
            # Status polling is cloud-only, but we don't need it every minute when Push is active.
            sleep_time = SNAPSHOT_INTERVAL if SNAPSHOT_INTERVAL > 0 else 300
            time.sleep(sleep_time)


# Start background threads
_worker_thread = threading.Thread(target=_snapshot_worker, daemon=True, name="snapshot-worker")
_worker_thread.start()

_mqtt_listener_thread = threading.Thread(target=_local_mqtt_worker, daemon=True, name="local-mqtt-listener")
_mqtt_listener_thread.start()

# ---------------------------------------------------------------------------
# Helper: get current snapshot bytes
# ---------------------------------------------------------------------------

def _get_current_snapshot_bytes() -> bytes | None:
    if CURRENT_SNAPSHOT_FILE.exists():
        try:
            return CURRENT_SNAPSHOT_FILE.read_bytes()
        except Exception as e:
            logger.error("Could not read snapshot file: %s", e)
    return None


def _placeholder_image() -> bytes:
    """Generate a simple placeholder JPEG when no snapshot is available."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (640, 360), color=(30, 30, 40))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 640, 360], outline=(60, 100, 180), width=4)
        draw.text((50, 140), "Ezviz HP2", fill=(100, 150, 255))
        draw.text((50, 180), "Fetching snapshot...", fill=(180, 180, 180))
        draw.text((50, 220), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), fill=(120, 120, 120))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        # Minimal 1x1 gray JPEG if Pillow fails
        return bytes([
            0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
            0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
            0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
            0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
            0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
            0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
            0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
            0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
            0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
            0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
            0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
            0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
            0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
            0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
            0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
            0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
            0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
            0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD2,
            0x8A, 0x28, 0x03, 0xFF, 0xD9,
        ])


# ---------------------------------------------------------------------------
# Ingress-aware URL helper
# ---------------------------------------------------------------------------

def ingress_url(path: str) -> str:
    """Prepend INGRESS_ENTRY to a path for absolute URLs inside templates."""
    base = INGRESS_ENTRY.rstrip("/")
    return f"{base}{path}"


# Make the helper available in templates
app.jinja_env.globals["ingress_url"] = ingress_url
app.jinja_env.globals["snapshot_interval"] = SNAPSHOT_INTERVAL

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Main dashboard."""
    logger.info("⚡ Request: / (Dashboard), Ingress=%s", INGRESS_ENTRY)
    events = _event_store.get_all()
    return render_template(
        "index.html",
        camera_serial=CAMERA_SERIAL,
        snapshot_interval=SNAPSHOT_INTERVAL,
        ingress_entry=INGRESS_ENTRY,
        last_snapshot_time=_last_snapshot_time.isoformat() if _last_snapshot_time else None,
        status=_last_status,
        events=events[:10],
        snapshot_error=_snapshot_error,
        status_error=_status_error,
    )


@app.route("/api/snapshot")
def api_snapshot():
    """Return the current snapshot JPEG."""
    logger.info("⚡ Request: /api/snapshot")
    img = _get_current_snapshot_bytes()
    if not img:
        img = _placeholder_image()
    return Response(img, mimetype="image/jpeg", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.route("/api/debug/alarms")
def debug_alarms():
    """Debug endpoint to see raw cloud alarm list across multiple subtypes."""
    try:
        client = get_client()
        if not client.is_connected():
            client.login()
        
        # Test standard, doorbell, and app default subtypes
        subtypes = ["92", "101", "102", "2701"]
        all_results = {}
        
        for stype in subtypes:
            try:
                alarms = client.get_alarm_list(CAMERA_SERIAL, max_count=10, s_type=stype)
                all_results[f"stype_{stype}"] = alarms
            except Exception as se:
                all_results[f"stype_{stype}_error"] = str(se)
                
        return jsonify({
            "camera_serial": CAMERA_SERIAL,
            "results": all_results
        })
    except Exception as e:
        logger.error("Debug alarms failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/snapshot/refresh", methods=["POST", "GET"])
def api_snapshot_refresh():
    """Trigger an immediate snapshot fetch from the cloud."""
    global _snapshot_error, _last_snapshot_time
    try:
        client = get_client()
        if not client.is_connected():
            client.login()
        img_bytes = client.get_snapshot()
        if img_bytes:
            with open(CURRENT_SNAPSHOT_FILE, "wb") as f:
                f.write(img_bytes)
            _last_snapshot_time = datetime.now(timezone.utc)
            _snapshot_error = ""
            return jsonify({
                "success": True,
                "timestamp": _last_snapshot_time.isoformat(),
                "bytes": len(img_bytes),
            })
        else:
            _snapshot_error = "Cloud returned empty snapshot"
            return jsonify({"success": False, "error": _snapshot_error}), 502

    except EzvizAuthError as e:
        handle_auth_error(e)
        return jsonify({"success": False, "error": str(e)}), 401
    except Exception as e:
        logger.exception("Snapshot refresh failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status")
def api_status():
    """Return camera status as JSON."""
    if _status_error:
        return jsonify({
            "error": _status_error,
            "camera_serial": CAMERA_SERIAL,
            "connected": False,
        }), 502

    status = dict(_last_status)
    status.pop("raw", None)  # Don't expose raw internal data
    status["last_snapshot"] = _last_snapshot_time.isoformat() if _last_snapshot_time else None
    status["timestamp"] = status["last_snapshot"] # for frontend compatibility
    status["snapshot_interval_s"] = SNAPSHOT_INTERVAL
    return jsonify(status)


@app.route("/api/events")
@app.route("/api/events/list")
def api_events():
    """Return recent alarm events as JSON."""
    logger.info("⚡ Request: /api/events")
    events = _event_store.get_all()
    return jsonify({
        "events": events,
        "count": len(events),
        "camera_serial": CAMERA_SERIAL,
    })


@app.route("/api/events/image/<alarm_id>")
def api_event_image(alarm_id):
    """Serve a locally-saved event image."""
    filename = f"event_{alarm_id}.jpg"
    filepath = EVENT_SNAPSHOT_PATH / filename
    if filepath.exists():
        return send_file(filepath, mimetype="image/jpeg")
    return jsonify({"error": "Image not found locally"}), 404


@app.route("/api/stream")
def api_stream():
    """MJPEG stream: continuously push snapshots as MJPEG frames."""
    is_history = request.args.get("history", "false").lower() == "true"
    logger.info("⚡ MJPEG Request: history=%s, Ingress=%s", is_history, INGRESS_ENTRY)

    def generate():
        frame_delay = 1.0 
        boundary = b"ezviz_boundary"
        logger.info("⚡ MJPEG Generator starting (history=%s)", is_history)
        
        try:
            # Start the multipart response
            yield b"--" + boundary + b"\r\n"
            
            while True:
                if is_history:
                    images = _event_store.get_image_list()
                    if not images:
                        logger.warning("⚡ History mode: NO IMAGES FOUND on disk")
                        img = _placeholder_image()
                        yield (
                            b"Content-Type: image/jpeg\r\n\r\n" +
                            img + b"\r\n" +
                            b"--" + boundary + b"\r\n"
                        )
                        time.sleep(2)
                    else:
                        logger.info("⚡ History mode: playing %d images", len(images))
                        for img_path in images:
                            try:
                                img = img_path.read_bytes()
                                if not img: continue
                                yield (
                                    b"Content-Type: image/jpeg\r\n" +
                                    f"Content-Length: {len(img)}\r\n\r\n".encode() +
                                    img + b"\r\n" +
                                    b"--" + boundary + b"\r\n"
                                )
                                time.sleep(frame_delay)
                            except Exception as e:
                                logger.error("Failed to read history image %s: %s", img_path, e)
                        # After one full loop, pause
                        time.sleep(1.0)
                else:
                    img = _get_current_snapshot_bytes()
                    if not img:
                        img = _placeholder_image()
                    
                    yield (
                        b"Content-Type: image/jpeg\r\n" +
                        f"Content-Length: {len(img)}\r\n\r\n".encode() +
                        img + b"\r\n" +
                        b"--" + boundary + b"\r\n"
                    )
                    time.sleep(frame_delay)
                
        except Exception as ge:
            logger.error("⚡ MJPEG Generator crashed: %s", ge)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=ezviz_boundary",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/devices")
def api_devices():
    """List all devices on the Ezviz account."""
    try:
        client = get_client()
        if not client.is_connected():
            client.login()
        devices = client.get_all_devices()
        return jsonify({"devices": devices, "count": len(devices)})
    except EzvizAuthError as e:
        return jsonify({"error": f"Auth failed: {e}"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def api_health():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "camera_serial": CAMERA_SERIAL,
        "connected": get_client().is_connected(),
        "last_snapshot": _last_snapshot_time.isoformat() if _last_snapshot_time else None,
        "snapshot_interval_s": SNAPSHOT_INTERVAL,
        "ingress_entry": INGRESS_ENTRY,
    })


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found", "path": request.path}), 404


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error", "detail": str(e)}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Ezviz Camera Proxy starting")
    logger.info("Camera serial : %s", CAMERA_SERIAL or "(not configured)")
    logger.info("Region        : %s", EZVIZ_REGION)
    logger.info("Snapshot every: %ds", SNAPSHOT_INTERVAL)
    logger.info("Ingress entry : %s", INGRESS_ENTRY or "/")
    logger.info("=" * 60)

    # Validate required config
    if not EZVIZ_USERNAME or not EZVIZ_PASSWORD:
        logger.error("EZVIZ_USERNAME and EZVIZ_PASSWORD must be set in Add-on config!")
    if not CAMERA_SERIAL:
        logger.error("CAMERA_SERIAL must be set in Add-on config!")

    # Start background threads
    threading.Thread(target=_snapshot_worker, daemon=True, name="snapshot-worker").start()
    threading.Thread(target=_local_mqtt_worker, daemon=True, name="local-mqtt-listener").start()

    app.run(
        host="0.0.0.0",
        port=8099,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
