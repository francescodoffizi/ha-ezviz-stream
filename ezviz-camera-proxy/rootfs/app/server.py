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
from datetime import datetime, timezone
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
SNAPSHOT_INTERVAL = int(os.environ.get("SNAPSHOT_INTERVAL", "30"))
ENABLE_MQTT_EVENTS = os.environ.get("ENABLE_MQTT_EVENTS", "true").lower() == "true"

# Ingress support: HA sets INGRESS_ENTRY e.g. "/api/hassio_ingress/abcdef"
INGRESS_ENTRY = os.environ.get("INGRESS_ENTRY", "/").rstrip("/")

DATA_PATH = Path(os.environ.get("DATA_PATH", "/data"))
SNAPSHOT_PATH = DATA_PATH / "snapshots"
SNAPSHOT_PATH.mkdir(parents=True, exist_ok=True)

CURRENT_SNAPSHOT_FILE = SNAPSHOT_PATH / "current.jpg"

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
_last_events: list = []
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
    
    mqtt_host = os.environ.get("MQTT_HOST")
    ext = msg.get("ext", {})
    ev_id = ext.get("msgId")
    if not ev_id:
        logger.warning("⚡ Push message missing msgId in ext: %s", ext)
        return

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
            logger.info("⚠️ MQTT_HOST not set, skipping local MQTT publish (check Add-on logs for discovery errors)")
            return

        mqtt_port = int(os.environ.get("MQTT_PORT", 1883))
        mqtt_user = os.environ.get("MQTT_USER", "")
        mqtt_pass = os.environ.get("MQTT_PASSWORD", "")
        auth = {'username': mqtt_user, 'password': mqtt_pass} if mqtt_user else None
        
        # Determine event type
        alert_code = int(ext.get("alert_type_code", 0))
        # 10120, 10100 = motion/person. 10006, 10000, 10002 = doorbell? 
        # For HP2/EP4, let's treat 10120 as motion and any other specific as doorbell if it sounds like it
        # Actually, let's publish to BOTH if we are unsure, or use a heuristic.
        is_doorbell = alert_code in [10000, 10001, 10002, 10006, 10022]
        event_type = "doorbell" if is_doorbell else "motion"
        
        # Topics requested by user: homeassistant/camera/ezviz/{serial}/{type}
        # Note: Users often want 'state' at the end for MQTT sensors, but we'll use exactly what they asked
        # Actually, let's use the standard discovery-compatible path too.
        main_topic = f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{event_type}/state"
        user_topic = f"homeassistant/camera/ezviz/{CAMERA_SERIAL}/{event_type}"
        
        try:
            # Publish 'ON' to both topics
            for t in [main_topic, user_topic]:
                publish.single(t, "ON", hostname=mqtt_host, port=mqtt_port, auth=auth)
            
            logger.info("⚡ Real-time Push: Published %s event to %s", event_type, user_topic)
            
            # Reset to 'OFF' after 5 seconds (simulated pulse)
            def _reset_mqtt():
                time.sleep(5)
                try:
                    for t in [main_topic, user_topic]:
                        publish.single(t, "OFF", hostname=mqtt_host, port=mqtt_port, auth=auth)
                except: pass
            threading.Thread(target=_reset_mqtt, daemon=True).start()

        except Exception as e:
            logger.error("⚡ Real-time Push failed to publish to HA MQTT: %s", e)

        # Update the local events list so the dashboard shows it instantly
        global _last_events
        new_ev = {
            "alarm_id": ev_id,
            "alarm_type": ext.get("alert_type_code"),
            "alarm_name": msg.get("alert", "Event"),
            "alarm_time": ext.get("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "pic_url": msg.get("image", ""),
            "is_push": True
        }
        _last_events.insert(0, new_ev)
        _last_events = _last_events[:20]  # Keep last 20


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
        ("motion", "Motion", "motion"),
        ("doorbell", "Doorbell", "occupancy")
    ]

    for s_type, s_name, s_class in sensors:
        config_topic = f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{s_type}/config"
        payload = {
            "name": f"{s_name} ({CAMERA_SERIAL})",
            "state_topic": f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{s_type}/state",
            "device_class": s_class,
            "unique_id": f"ezviz_{CAMERA_SERIAL}_{s_type}",
            "device": device_info,
            "payload_on": "ON",
            "payload_off": "OFF"
        }
        try:
            publish.single(config_topic, json.dumps(payload), hostname=mqtt_host, port=mqtt_port, auth=auth, retain=True)
            logger.info("⚡ MQTT Discovery: Sent config for %s", s_type)
        except Exception as e:
            logger.error("Failed to send MQTT discovery for %s: %s", s_type, e)


def _snapshot_worker():
    """Background thread: fetch a new snapshot every SNAPSHOT_INTERVAL seconds."""
    global _last_status, _last_events, _snapshot_error, _status_error, _last_snapshot_time, _seen_events, ezviz_mqtt

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
                _last_events = client.get_alarm_list(max_count=10)
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
            # Sleep SNAPSHOT_INTERVAL, but at least every 60s for event polling if snapshots are off
            sleep_time = SNAPSHOT_INTERVAL if SNAPSHOT_INTERVAL > 0 else 60
            time.sleep(sleep_time)


# Start background thread
_worker_thread = threading.Thread(target=_snapshot_worker, daemon=True, name="snapshot-worker")
_worker_thread.start()

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
    return render_template(
        "index.html",
        camera_serial=CAMERA_SERIAL,
        snapshot_interval=SNAPSHOT_INTERVAL,
        ingress_entry=INGRESS_ENTRY,
        last_snapshot_time=_last_snapshot_time.isoformat() if _last_snapshot_time else None,
        status=_last_status,
        events=_last_events[:5],
        snapshot_error=_snapshot_error,
        status_error=_status_error,
    )


@app.route("/api/snapshot")
def api_snapshot():
    """Return the current snapshot JPEG."""
    img = _get_current_snapshot_bytes()
    if not img:
        img = _placeholder_image()
    return Response(img, mimetype="image/jpeg", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


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
    status["snapshot_interval_s"] = SNAPSHOT_INTERVAL
    return jsonify(status)


@app.route("/api/events")
def api_events():
    """Return recent alarm events as JSON."""
    return jsonify({
        "events": _last_events,
        "count": len(_last_events),
        "camera_serial": CAMERA_SERIAL,
    })


@app.route("/api/stream")
def api_stream():
    """
    MJPEG stream: continuously push the latest snapshot as MJPEG frames.
    Frame rate is determined by SNAPSHOT_INTERVAL (or faster for UI smoothness).
    """
    def generate():
        # Molti client MJPEG (incluso Home Assistant) vanno in timeout se il ritardo
        # tra un frame e l'altro supera i 5-10 secondi. Trasmettiamo a 1 FPS per 
        # simulare uno stream fluido anche se la foto di base cambia ogni minuto.
        frame_delay = 1.0
        while True:
            img = _get_current_snapshot_bytes()
            if not img:
                img = _placeholder_image()
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(img)).encode() + b"\r\n\r\n"
                + img
                + b"\r\n"
            )
            time.sleep(frame_delay)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering for streaming
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

    app.run(
        host="0.0.0.0",
        port=8099,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
