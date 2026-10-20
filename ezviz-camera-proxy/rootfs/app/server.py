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
import re
import subprocess
import shutil
import threading
import time
import requests
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
EZVIZ_OPEN_DOMAIN = os.environ.get("EZVIZ_OPEN_DOMAIN", "https://ieuopen.ezvizlife.com")
EZVIZ_APP_KEY = os.environ.get("EZVIZ_APP_KEY", "")
EZVIZ_APP_SECRET = os.environ.get("EZVIZ_APP_SECRET", "")
EZVIZ_ACCESS_TOKEN = os.environ.get("EZVIZ_ACCESS_TOKEN", "")
CAMERA_SERIAL = os.environ.get("CAMERA_SERIAL", "")
CAMERA_PASSWORD = os.environ.get("CAMERA_PASSWORD", "")
EZVIZ_ENCRYPTION_KEY = os.environ.get("EZVIZ_ENCRYPTION_KEY", "")
SNAPSHOT_INTERVAL = int(os.environ.get("SNAPSHOT_INTERVAL", "30"))
ENABLE_MQTT_EVENTS = os.environ.get("ENABLE_MQTT_EVENTS", "true").lower() == "true"
SAVE_EVENT_CLIPS = os.environ.get("SAVE_EVENT_CLIPS", "true").lower() == "true"

# Ingress support: HA sets INGRESS_ENTRY e.g. "/api/hassio_ingress/abcdef"
INGRESS_ENTRY = os.environ.get("INGRESS_ENTRY", "/").rstrip("/")

DATA_PATH = Path(os.environ.get("DATA_PATH", "/data"))
SNAPSHOT_PATH = DATA_PATH / "snapshots"
SNAPSHOT_PATH.mkdir(parents=True, exist_ok=True)

CURRENT_SNAPSHOT_FILE = SNAPSHOT_PATH / "current.jpg"
EVENT_SNAPSHOT_PATH = SNAPSHOT_PATH / "events"
EVENT_SNAPSHOT_PATH.mkdir(parents=True, exist_ok=True)
EVENT_VIDEO_PATH = DATA_PATH / "videos"
EVENT_VIDEO_PATH.mkdir(parents=True, exist_ok=True)
_last_alarm_type = "Mai"
_last_alarm_time = "Mai"
_last_doorbell_time = "Mai"


def _normalize_alarm_label(label: str) -> str:
    """Fix Ezviz Cloud concatenated strings such as 'Lauraappeared' -> 'Laura appeared'."""
    if not label:
        return ""
    text = str(label).strip()
    return re.sub(r"(?i)([^\s])(appeared)\b", r"\1 \2", text)


def _alarm_type_priority(label: str) -> int:
    """Return specificity priority so named detections ('Laura appeared') and Doorbell beat generic 'AI Human Detection'."""
    low = str(label or "").strip().lower()
    if not low or low.isdigit() or low in ("event", "alarm", "evento", "allarme"):
        return 0
    if low in ("motion", "pir", "rilevamento movimento"):
        return 1
    if low in ("ai human detection", "face recognition", "doorbell pir", "person detection", "face detection"):
        return 2
    if "appeared" in low:
        return 3
    if "doorbell" in low or "campanello" in low:
        return 4
    return 2



class EventStore:
    """Manages a list of recent events with deduplication and local image caching."""
    def __init__(self, max_size=20):
        self.max_size = max_size
        self.events = []
        self.lock = threading.Lock()
        self.clip_locks: dict[str, threading.Lock] = {}

    def _get_clip_lock(self, alarm_id: str) -> threading.Lock:
        with self.lock:
            if alarm_id not in self.clip_locks:
                self.clip_locks[alarm_id] = threading.Lock()
            return self.clip_locks[alarm_id]

    @staticmethod
    def event_age_seconds(alarm_time: str) -> float:
        if not alarm_time:
            return 999999.0
        try:
            ev_dt = datetime.strptime(alarm_time, "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - ev_dt).total_seconds()
        except Exception:
            return 999999.0

    def is_valid_video(self, v_path: Path, alarm_time: str = "") -> bool:
        """Return True only if the MP4 exists, is non-empty, and was recorded during the event window."""
        try:
            if not v_path.exists() or v_path.stat().st_size <= 10000:
                return False
            if not alarm_time:
                ev_id = v_path.name[6:-4]
                for e in self.events:
                    if str(e.get("alarm_id")) == str(ev_id):
                        alarm_time = e.get("alarm_time", "")
                        break
            if alarm_time:
                ev_dt = datetime.strptime(alarm_time, "%Y-%m-%d %H:%M:%S")
                mtime_dt = datetime.fromtimestamp(v_path.stat().st_mtime)
                delay = (mtime_dt - ev_dt).total_seconds()
                # Genuine event clips are recorded within [-30s, +180s] of alarm_time.
                # Clips with delay > 180s were recorded posthumously from the live stream.
                if delay < -30 or delay > 180:
                    return False
            return True
        except Exception:
            return False

    def _reconcile_legacy_videos(self):
        """Migrate genuine clips recorded under numeric push IDs to canonical MVE IDs and purge posthumous live clips."""
        try:
            all_mp4s = list(EVENT_VIDEO_PATH.glob("video_*.mp4"))
            for e in self.events:
                ev_id = str(e["alarm_id"])
                ev_time = e.get("alarm_time", "")
                target_v = EVENT_VIDEO_PATH / f"video_{ev_id}.mp4"
                if not self.is_valid_video(target_v, ev_time) and ev_time:
                    for cand in all_mp4s:
                        if cand == target_v or not cand.exists():
                            continue
                        if self.is_valid_video(cand, ev_time):
                            st = cand.stat()
                            cand.replace(target_v)
                            os.utime(target_v, (st.st_atime, st.st_mtime))
                            logger.info("Migrated genuine event clip %s -> %s", cand.name, target_v.name)
                            break
                e["local_video"] = self.is_valid_video(target_v, ev_time)
        except Exception as e:
            logger.error("Video reconciliation error: %s", e)

    def add_events(self, new_events: list[dict], download_images=True):
        """Merge new events into the store, deduplicating with fuzzy time matching."""
        with self.lock:
            for event in new_events:
                raw_id = event.get("alarm_id")
                if not raw_id: continue
                
                ev_id = str(raw_id)
                raw_type = _normalize_alarm_label(event.get("alarm_type") or event.get("alarm_name") or "Event")
                raw_name = _normalize_alarm_label(event.get("alarm_name") or "")
                sub_type = str(event.get("sub_type") or "")
                
                # Normalize types according to Ezviz APK AlarmType.java
                type_map = {
                    "2701": "Doorbell",
                    "20007": "Doorbell",
                    "10054": "Doorbell",
                    "10055": "Doorbell",
                    "10016": "Doorbell PIR",
                    "10000": "PIR",
                    "10002": "Motion",
                    "10120": "AI Human Detection",
                    "10121": "AI Human Detection",
                    "10140": "PIR",
                    "12663": "Face Recognition",
                    "1": "Motion",
                    "11514": "Motion",
                    "11502": "Motion",
                }
                if "appeared" in raw_name.lower():
                    ev_type = raw_name
                elif "appeared" in raw_type.lower():
                    ev_type = raw_type
                else:
                    ev_type = type_map.get(sub_type) or type_map.get(str(raw_type), str(raw_type))
                ev_type = _normalize_alarm_label(ev_type)
                if any(kw in ev_type.lower() for kw in ("doorbell", "campanello", "chiamata", "squillo", "citofono", "door_bell")):
                    ev_type = "Doorbell"
                ev_time = (event.get("alarm_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")).replace("+", " ")

                match_idx = None
                # 1. Match by ID
                for i, e in enumerate(self.events):
                    if e["alarm_id"] == ev_id:
                        match_idx = i
                        break
                
                # 2. Match by fuzzy time (10s) and same camera
                if match_idx is None:
                    try:
                        dt = datetime.strptime(ev_time, "%Y-%m-%d %H:%M:%S")
                        for i, e in enumerate(self.events):
                            if e.get("device_serial") == event.get("device_serial"):
                                e_dt = datetime.strptime(e["alarm_time"], "%Y-%m-%d %H:%M:%S")
                                if abs((dt - e_dt).total_seconds()) <= 10:
                                    match_idx = i
                                    break
                    except: pass

                v_path = EVENT_VIDEO_PATH / f"video_{ev_id}.mp4"
                normalized = {
                    "alarm_id": ev_id,
                    "alarm_type": ev_type,
                    "alarm_time": ev_time,
                    "alarm_pic_url": event.get("alarm_pic_url") or event.get("pic_url") or "",
                    "is_push": event.get("is_push", False),
                    "local_pic": event.get("local_pic", False) or (EVENT_SNAPSHOT_PATH / f"event_{ev_id}.jpg").exists(),
                    "local_video": self.is_valid_video(v_path, ev_time),
                    "device_serial": event.get("device_serial", CAMERA_SERIAL)
                }

                if match_idx is not None:
                    old = self.events[match_idx]
                    old_id = str(old["alarm_id"])
                    # Prefer canonical MVE... alarm_id if the existing record used a numeric/push fallback ID
                    if old_id != ev_id and ev_id.startswith("MVE") and not old_id.startswith("MVE"):
                        old_img = EVENT_SNAPSHOT_PATH / f"event_{old_id}.jpg"
                        new_img = EVENT_SNAPSHOT_PATH / f"event_{ev_id}.jpg"
                        if old_img.exists() and not new_img.exists():
                            try:
                                old_img.replace(new_img)
                            except Exception:
                                pass
                        old_vid = EVENT_VIDEO_PATH / f"video_{old_id}.mp4"
                        new_vid = EVENT_VIDEO_PATH / f"video_{ev_id}.mp4"
                        if old_vid.exists() and not new_vid.exists():
                            try:
                                st = old_vid.stat()
                                old_vid.replace(new_vid)
                                os.utime(new_vid, (st.st_atime, st.st_mtime))
                            except Exception:
                                pass
                        old["alarm_id"] = ev_id

                    # Merge: prefer more specific type (Doorbell > '<Name> appeared' > 'AI Human Detection' > 'Motion' > generic)
                    old["alarm_type"] = _normalize_alarm_label(old.get("alarm_type", "Event"))
                    if _alarm_type_priority(ev_type) > _alarm_type_priority(old["alarm_type"]):
                        old["alarm_type"] = ev_type
                    
                    # Merge flags
                    if normalized["is_push"]: old["is_push"] = True
                    if normalized["alarm_pic_url"]: old["alarm_pic_url"] = normalized["alarm_pic_url"]
                    if normalized["local_pic"] or (EVENT_SNAPSHOT_PATH / f"event_{old['alarm_id']}.jpg").exists():
                        old["local_pic"] = True
                    cur_vid = EVENT_VIDEO_PATH / f"video_{old['alarm_id']}.mp4"
                    old["local_video"] = self.is_valid_video(cur_vid, old.get("alarm_time", ""))
                else:
                    self.events.append(normalized)

            # Sort and Prune
            self.events.sort(key=lambda x: x["alarm_time"], reverse=True)
            self.events = self.events[:self.max_size]
            
            # Global status update for sidebar
            if self.events:
                global _last_alarm_type, _last_alarm_time, _last_doorbell_time
                latest = self.events[0]
                _last_alarm_type = latest["alarm_type"]
                _last_alarm_time = latest["alarm_time"]
                for e in self.events:
                    if e["alarm_type"] == "Doorbell":
                        _last_doorbell_time = e["alarm_time"]
                        break
            
            # Reconcile any genuine numeric-ID clips and prune invalid/old files from disk
            self._reconcile_legacy_videos()
            self._prune_disk()
            
        if download_images:
            threading.Thread(target=self._process_images, daemon=True, name="event-image-processor").start()
            if SAVE_EVENT_CLIPS:
                threading.Thread(target=self._process_videos, daemon=True, name="event-video-processor").start()

    def _prune_disk(self):
        """Delete images and videos from disk that are no longer in self.events or are invalid posthumous clips."""
        try:
            ev_map = {str(e["alarm_id"]): e.get("alarm_time", "") for e in self.events}
            for f in EVENT_SNAPSHOT_PATH.glob("event_*.jpg"):
                try:
                    f_id = f.name[6:-4]
                    if f_id not in ev_map:
                        f.unlink()
                        logger.debug("Pruned old event image: %s", f.name)
                except: continue

            for f in EVENT_VIDEO_PATH.glob("video_*.mp4"):
                try:
                    f_id = f.name[6:-4]
                    if f_id not in ev_map or not self.is_valid_video(f, ev_map[f_id]):
                        f.unlink()
                        logger.info("Pruned obsolete or posthumous live video clip: %s", f.name)
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
                        for e in self.events:
                            if e["alarm_id"] == ev["alarm_id"]:
                                e["local_pic"] = True
                                break
                        is_latest = bool(self.events and self.events[0]["alarm_id"] == ev["alarm_id"])
                    if is_latest:
                        try:
                            CURRENT_SNAPSHOT_FILE.write_bytes(img_bytes)
                            global _last_snapshot_time, _snapshot_error
                            _last_snapshot_time = datetime.now(timezone.utc)
                            _snapshot_error = ""
                        except Exception:
                            pass
                    logger.info("Saved event image: %s", filename)
                time.sleep(1) # Throttle
        except Exception as e:
            logger.error("Event image processor failed: %s", e)

    def generate_clip(self, alarm_id: str) -> bool:
        """
        Generate MP4 clip for a specific event via ffmpeg:
        1. Primary: retrieve the camera's autonomous event recording (CloudPlay recType=1 or MicroSD recType=2,
           which includes the 5s pre-buffer and full subject approach) via /api/lapp/video/by/time and OpenPlayback (/v3/openpb/).
        2. Fallback: if no recording is indexed and the event is currently active (<= 90s), capture from the live HLS stream.
        """
        if not shutil.which("ffmpeg"):
            logger.warning("ffmpeg not found, cannot generate clip")
            return False

        alarm_id_str = str(alarm_id)
        clip_lock = self._get_clip_lock(alarm_id_str)
        with clip_lock:
            event = None
            with self.lock:
                for e in self.events:
                    if str(e.get("alarm_id")) == alarm_id_str:
                        event = e
                        break

            if not event:
                logger.warning("Event %s not found in store for clip generation", alarm_id_str)
                return False

            ev_time = event.get("alarm_time", "")
            out_path = EVENT_VIDEO_PATH / f"video_{alarm_id_str}.mp4"
            if self.is_valid_video(out_path, ev_time):
                return True

            if not ev_time:
                return False

            try:
                ev_dt = datetime.strptime(ev_time, "%Y-%m-%d %H:%M:%S")
                ev_ts = ev_dt.timestamp()
            except Exception as te:
                logger.warning("Error parsing event time '%s': %s", ev_time, te)
                return False

            age_s = self.event_age_seconds(ev_time)
            if age_s > 86400:
                logger.info("Skipping clip generation for old event %s (age=%.0fs > 86400s)", alarm_id_str, age_s)
                return False

            client = get_client()
            tmp_out = EVENT_VIDEO_PATH / f"tmp_{alarm_id_str}.mp4"

            # 1. Wait for the camera to finish its 10-16s autonomous event recording and index it on CloudPlay/SD
            if 0 <= age_s < 16:
                wait_s = max(1.0, 16.0 - age_s)
                logger.info(
                    "⏳ Waiting %.1fs for camera to complete autonomous event recording for %s...",
                    wait_s, alarm_id_str
                )
                time.sleep(wait_s)

            rec_info = None
            max_polls = 6 if self.event_age_seconds(ev_time) < 60 else 1
            for poll_idx in range(max_polls):
                rec_info = client.get_event_record_info(ev_time)
                if rec_info:
                    # Ensure the recording end time has already passed before pulling playback stream
                    rec_end_dt = datetime.strptime(rec_info["stop_time"], "%Y-%m-%d %H:%M:%S")
                    rem_s = (rec_end_dt - datetime.now()).total_seconds()
                    if rem_s > 0 and rem_s <= 20:
                        logger.info("⏳ Recording %s still in progress, waiting %.1fs...", alarm_id_str, rem_s + 1.5)
                        time.sleep(rem_s + 1.5)
                    break
                if poll_idx < max_polls - 1:
                    logger.info(
                        "⏳ Autonomous recording not indexed yet for %s (poll %d/%d), waiting 3s...",
                        alarm_id_str, poll_idx + 1, max_polls
                    )
                    time.sleep(3.0)

            # 2. Primary Path: Download autonomous recorded playback clip (CloudPlay type=3 or MicroSD type=2)
            if rec_info:
                start_str = rec_info["start_time"]
                stop_str = rec_info["stop_time"]
                dur_s = rec_info.get("duration_s", 20)
                addr_types = [rec_info["addr_type"]]
                if 2 not in addr_types:
                    addr_types.append(2)

                for addr_t in addr_types:
                    for proto in (3, 4):  # 3 = RTMP (/v3/openpb/), 4 = HTTP-FLV (/v3/openpb/)
                        pb_url = client.get_playback_stream_url(
                            start_time=start_str,
                            stop_time=stop_str,
                            addr_type=addr_t,
                            protocol=proto,
                        )
                        if not pb_url:
                            continue

                        logger.info(
                            "⚡ Exporting recorded playback clip (%s -> %s, %ds, type=%d, proto=%d) for event %s...",
                            start_str, stop_str, dur_s, addr_t, proto, alarm_id_str
                        )
                        # Normalize to 1280x720 H.264 because CS-HP2 CloudPlay recordings switch SPS/PPS
                        # from 3712x2160 (pre-buffer burst) to 1280x720 mid-stream, which breaks iOS -c copy playback
                        cmd_Lists = [
                            [
                                "ffmpeg", "-y",
                                "-i", pb_url,
                                "-vf", "scale=1280:720",
                                "-c:v", "libx264",
                                "-preset", "ultrafast",
                                "-crf", "23",
                                "-pix_fmt", "yuv420p",
                                "-c:a", "aac",
                                "-movflags", "+faststart",
                                "-t", str(dur_s + 4),
                                str(tmp_out),
                            ],
                            [
                                "ffmpeg", "-y",
                                "-i", pb_url,
                                "-c", "copy",
                                "-movflags", "+faststart",
                                "-t", str(dur_s + 4),
                                str(tmp_out),
                            ],
                        ]
                        for cmd in cmd_Lists:
                            try:
                                if tmp_out.exists():
                                    tmp_out.unlink()
                                proc = subprocess.run(
                                    cmd,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    timeout=max(35, dur_s + 20),
                                )
                                if tmp_out.exists() and tmp_out.stat().st_size > 10000:
                                    tmp_out.replace(out_path)
                                    # Align file mtime with the event window so is_valid_video always validates it
                                    os.utime(out_path, (ev_ts + dur_s, ev_ts + dur_s))
                                    logger.info(
                                        "✅ Successfully exported autonomous playback clip: %s (%d bytes)",
                                        out_path.name, out_path.stat().st_size
                                    )
                                    with self.lock:
                                        for e in self.events:
                                            if str(e.get("alarm_id")) == alarm_id_str:
                                                e["local_video"] = True
                                                break
                                    return True
                            except subprocess.TimeoutExpired:
                                # With HTTP-FLV (proto=4), server may hold socket open after sending all frames;
                                # check if tmp_out already contains a valid clip
                                if tmp_out.exists() and tmp_out.stat().st_size > 50000:
                                    tmp_out.replace(out_path)
                                    os.utime(out_path, (ev_ts + dur_s, ev_ts + dur_s))
                                    logger.info(
                                        "✅ Saved playback clip after stream timeout: %s (%d bytes)",
                                        out_path.name, out_path.stat().st_size
                                    )
                                    with self.lock:
                                        for e in self.events:
                                            if str(e.get("alarm_id")) == alarm_id_str:
                                                e["local_video"] = True
                                                break
                                    return True
                                if tmp_out.exists():
                                    tmp_out.unlink()
                            except Exception as fe:
                                if tmp_out.exists():
                                    tmp_out.unlink()
                                logger.warning("Playback ffmpeg attempt failed for %s: %s", alarm_id_str, fe)

            # 3. Fallback Path: Live HLS stream capture if no recorded clip was found and event is still recent
            if self.event_age_seconds(ev_time) > 120:
                logger.info(
                    "No CloudPlay/SD recording found for past event %s (age=%.0fs > 120s), skipping live fallback",
                    alarm_id_str, self.event_age_seconds(ev_time)
                )
                return False

            logger.info("⚠️ No autonomous recording found for %s, falling back to live HLS stream...", alarm_id_str)
            start_str = (ev_dt - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
            stop_str = (ev_dt + timedelta(seconds=35)).strftime("%Y-%m-%d %H:%M:%S")
            stream_url = client.get_hls_stream_url(start_time=start_str, stop_time=stop_str)
            if not stream_url:
                logger.warning("Could not obtain fallback HLS stream URL for event %s", alarm_id_str)
                return False

            for attempt in range(8):
                try:
                    check_resp = requests.get(stream_url, timeout=4, verify=False, allow_redirects=True)
                    content = check_resp.text or ""
                    if check_resp.status_code == 200 and ".ts" in content and "ErrCode" not in content:
                        break
                except Exception:
                    pass
                time.sleep(1.5)

            logger.info("⚡ Recording fallback live MP4 clip (25s) for event %s...", alarm_id_str)
            cmd = [
                "ffmpeg", "-y",
                "-timeout", "15000000",
                "-i", stream_url,
                "-c", "copy",
                "-movflags", "+faststart",
                "-t", "25",
                str(tmp_out)
            ]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
                if proc.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 10000:
                    tmp_out.replace(out_path)
                    os.utime(out_path, (ev_ts + 25, ev_ts + 25))
                    logger.info("✅ Successfully generated fallback live clip: %s (%d bytes)", out_path.name, out_path.stat().st_size)
                    with self.lock:
                        for e in self.events:
                            if str(e.get("alarm_id")) == alarm_id_str:
                                e["local_video"] = True
                                break
                    return True
                else:
                    if tmp_out.exists():
                        tmp_out.unlink()
                    logger.warning("Fallback FFmpeg finished with code %s for event %s", proc.returncode, alarm_id_str)
                    return False
            except Exception as fe:
                if tmp_out.exists():
                    tmp_out.unlink()
                logger.error("Fallback FFmpeg error on event %s: %s", alarm_id_str, fe)
                return False

    def _process_videos(self):
        """Download MP4 clips for recent events (up to 6 hours old via CloudPlay playback)."""
        if not shutil.which("ffmpeg"):
            logger.warning("ffmpeg not found, skipping video clip downloads")
            return

        try:
            with self.lock:
                to_process = [
                    e for e in self.events[:5]
                    if not e.get("local_video") and self.event_age_seconds(e.get("alarm_time", "")) <= 21600
                ]

            for ev in to_process:
                ev_id = ev["alarm_id"]
                out_path = EVENT_VIDEO_PATH / f"video_{ev_id}.mp4"
                if self.is_valid_video(out_path, ev.get("alarm_time", "")):
                    with self.lock:
                        ev["local_video"] = True
                    continue

                self.generate_clip(ev_id)
                time.sleep(2)
        except Exception as e:
            logger.error("Event video processor error: %s", e)

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
_seen_events: set = set()
_seen_events_lock = threading.Lock()
_last_event_trigger_time: float = 0
_last_auth_fail_time: float = 0
_live_stream_cache: dict = {"url": "", "expire": 0}
_live_stream_lock = threading.Lock()


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
                encryption_key=EZVIZ_ENCRYPTION_KEY,
                open_domain=EZVIZ_OPEN_DOMAIN,
                app_key=EZVIZ_APP_KEY,
                app_secret=EZVIZ_APP_SECRET,
                access_token=EZVIZ_ACCESS_TOKEN,
                token_cache_path=str(DATA_PATH / "open_token.json"),
                session_cache_path=str(DATA_PATH / "cloud_session.json"),
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

def _fetch_snapshot_on_event(ev_id: str | None = None) -> bool:
    """Trigger an immediate snapshot when motion/doorbell is pushed."""
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
            if ev_id:
                ev_img_path = EVENT_SNAPSHOT_PATH / f"event_{ev_id}.jpg"
                if not ev_img_path.exists():
                    ev_img_path.write_bytes(img_bytes)
                    with _event_store.lock:
                        for e in _event_store.events:
                            if str(e.get("alarm_id")) == str(ev_id):
                                e["local_pic"] = True
                                break
            logger.debug("Event-driven snapshot saved (%d bytes)", len(img_bytes))
            return True
        else:
            logger.warning("Event-driven snapshot returned empty data")
    except Exception as e:
        logger.error("Failed to fetch snapshot on event: %s", e)
    return False

def publish_alarm_to_ha_mqtt(ev_id: str, event_type: str, alert_code: int, alert_text: str, is_alarm: bool, attr_data: dict, push_time: str):
    """Publish an alarm (motion/doorbell/alarm) to Home Assistant MQTT topics."""
    mqtt_host = os.environ.get("MQTT_HOST")
    if not mqtt_host:
        logger.info("⚠️ MQTT_HOST not set, skipping local MQTT publish")
        return

    mqtt_port = int(os.environ.get("MQTT_PORT", 1883))
    mqtt_user = os.environ.get("MQTT_USER", "")
    mqtt_pass = os.environ.get("MQTT_PASSWORD", "")
    auth = {'username': mqtt_user, 'password': mqtt_pass} if mqtt_user else None

    if event_type == "doorbell":
        global _last_doorbell_time
        _last_doorbell_time = push_time

    main_topic = f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{event_type}/state"
    global_topic = f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_alarm/state"
    user_topic = f"homeassistant/camera/ezviz/{CAMERA_SERIAL}/{event_type}"

    try:
        # Publish 'ON' to specific sensor
        for t in [main_topic, user_topic]:
            publish.single(t, "ON", hostname=mqtt_host, port=mqtt_port, auth=auth)

        # ONLY trigger global Alarm topic if it's a real problem event
        if is_alarm:
            logger.info("⚡ TRIGGERING GLOBAL ALARM for code %s", alert_code)
            publish.single(global_topic, "ON", hostname=mqtt_host, port=mqtt_port, auth=auth)

        # Build attribute payload
        payload_attr = attr_data.copy() if isinstance(attr_data, dict) else {}
        if "ext" in payload_attr and isinstance(payload_attr["ext"], dict):
            payload_attr.update(payload_attr.pop("ext"))

        # Add direct video endpoints
        payload_attr["image_url"] = ingress_url(f"/api/events/image/{ev_id}")
        payload_attr["video_url"] = ingress_url(f"/api/events/video/{ev_id}")
        payload_attr["clip_url"] = ingress_url(f"/api/events/clip/{ev_id}")
        payload_attr["player_url"] = ingress_url(f"/player?alarm_id={ev_id}")

        # Shorten long URLs to prevent Home Assistant >255 char state errors
        for k, v in list(payload_attr.items()):
            if isinstance(v, str) and v.startswith("http") and len(v) > 200:
                payload_attr[k] = ingress_url(f"/api/events/image/{ev_id}")

        attr_topic = main_topic.replace("/state", "/attributes")
        publish.single(attr_topic, json.dumps(payload_attr), hostname=mqtt_host, port=mqtt_port, auth=auth)

        global_attr_topic = global_topic.replace("/state", "/attributes")
        publish.single(global_attr_topic, json.dumps(payload_attr), hostname=mqtt_host, port=mqtt_port, auth=auth)

        logger.info("⚡ Real-time Push: Published %s event (code %s, id %s)", event_type, alert_code, ev_id)

        # Reset to 'OFF' after 5 seconds
        def _reset_mqtt():
            time.sleep(5)
            try:
                for t in [main_topic, user_topic]:
                    publish.single(t, "OFF", hostname=mqtt_host, port=mqtt_port, auth=auth)
                if is_alarm:
                    publish.single(global_topic, "OFF", hostname=mqtt_host, port=mqtt_port, auth=auth)
            except Exception:
                pass
        threading.Thread(target=_reset_mqtt, daemon=True).start()

    except Exception as e:
        logger.error("⚡ Real-time Push failed to publish to HA MQTT: %s", e)


def _on_ezviz_push_message(msg):
    """Callback triggered instantly by Ezviz Cloud when motion/doorbell occurs."""
    if not ENABLE_MQTT_EVENTS:
        return

    logger.info("⚡ Real-time Push Message received: %s", json.dumps(msg))

    ext = msg.get("ext", {}) if isinstance(msg.get("ext"), dict) else {}
    channel_type = str(ext.get("channel_type") or "")

    # For DoorBellPushAlarm (channel_type == 14), calling_status: 1 = incoming ring, 2 = answered, 3 = ended/missed
    if channel_type == "14":
        raw_call_status = ext.get("calling_status") if ext.get("calling_status") is not None else ext.get("media_url_alt1")
        if raw_call_status is not None and str(raw_call_status).strip() not in ("", "1"):
            logger.info(
                "⚡ Ignoring DoorBellPushAlarm call status update (calling_status=%s, calling_id=%s)",
                raw_call_status,
                ext.get("calling_id") or ext.get("msgId"),
            )
            return

    # Prefer ext["msgId"] (canonical MVE... ID or callingId) so push and cloud polling share the same ID
    raw_msg_id = ext.get("msgId") or ext.get("calling_id") or (ext.get("default_pic_url") if channel_type == "14" and not str(ext.get("default_pic_url", "")).startswith("http") else None)
    raw_push_id = msg.get("id") or msg.get("extras", {}).get("ticket")
    ev_id = raw_msg_id or raw_push_id

    # Capture the exact time from the push message if available (normalize '+' from DoorBellPushAlarm)
    push_time = (ext.get("time") or "").replace("+", " ")
    if not push_time:
        push_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not ev_id:
        # Fallback to a hash of alert text and time if ID is missing
        ev_id = f"push_{msg.get('alert', 'Event')}_{push_time}".replace(" ", "_").replace(":", "-")
    ev_id = str(ev_id)
    alt_id = str(raw_push_id) if raw_push_id else ""

    with _seen_events_lock:
        already_seen = ev_id in _seen_events or (alt_id and alt_id in _seen_events)
        if not already_seen:
            _seen_events.add(ev_id)
            if alt_id:
                _seen_events.add(alt_id)

    if not already_seen:
        logger.info("⚡ New event %s detected (channel_type: %s, type: %s). Processing...", ev_id, channel_type, ext.get("alert_type_code"))

        # Determine event type & extract image URL from push payload
        try:
            alert_code = int(ext.get("alert_type_code") or (2701 if channel_type == "14" else 0))
        except (ValueError, TypeError):
            alert_code = 2701 if channel_type == "14" else 0

        alert_text = str(msg.get("alert") or ext.get("device_name") or "Event")
        is_doorbell = (
            channel_type == "14"
            or alert_code in (2701, 20007, 10054, 10055)
            or any(kw in alert_text.lower() for kw in ("doorbell", "campanello", "chiamata", "squillo", "citofono", "door_bell"))
        )

        push_pic_url = (
            ext.get("image")
            or ext.get("picUrl")
            or ext.get("alarmPicUrl")
            or (ext.get("media_url_alt2") if channel_type == "14" and str(ext.get("media_url_alt2", "")).startswith("http") else "")
            or (ext.get("default_pic_url") if str(ext.get("default_pic_url", "")).startswith("http") else "")
            or msg.get("image")
            or msg.get("picUrl")
            or ""
        )

        # 1. Immediately download the event image from push_pic_url BEFORE publishing MQTT ON
        #    so Home Assistant automations never grab the previous event's snapshot.
        has_event_pic = False
        if isinstance(push_pic_url, str) and push_pic_url.startswith("http"):
            try:
                client = get_client()
                for attempt in range(4):
                    img_bytes = client._download_image(push_pic_url)
                    if img_bytes:
                        ev_img_path = EVENT_SNAPSHOT_PATH / f"event_{ev_id}.jpg"
                        ev_img_path.write_bytes(img_bytes)
                        CURRENT_SNAPSHOT_FILE.write_bytes(img_bytes)
                        global _last_snapshot_time, _snapshot_error
                        _last_snapshot_time = datetime.now(timezone.utc)
                        _snapshot_error = ""
                        has_event_pic = True
                        logger.info(
                            "⚡ Push event image downloaded immediately (%d bytes) -> %s & current.jpg",
                            len(img_bytes),
                            ev_img_path.name,
                        )
                        break
                    if attempt < 3:
                        time.sleep(0.6)
            except Exception as pe:
                logger.warning("Immediate push image download failed for %s: %s", ev_id, pe)

        # 2. Register the event in EventStore (with local_pic=True if already saved)
        _event_store.add_events(
            [{
                "alarm_id": ev_id,
                "alarm_type": "Doorbell" if is_doorbell else alert_code,
                "sub_type": str(2701 if is_doorbell else alert_code),
                "alarm_name": "Doorbell" if is_doorbell else alert_text,
                "alarm_time": push_time,
                "pic_url": push_pic_url,
                "local_pic": has_event_pic,
                "is_push": True,
            }],
            download_images=not has_event_pic,
        )

        # 2b. Immediately launch video clip capture
        if SAVE_EVENT_CLIPS:
            threading.Thread(
                target=_event_store.generate_clip,
                args=(ev_id,),
                daemon=True,
                name=f"push-clip-{ev_id}",
            ).start()

        # 3. Only if push_pic_url was missing or failed download, force a synchronous snapshot
        #    before notifying HA so current.jpg is never stale.
        global _last_event_trigger_time
        now = time.time()
        if not has_event_pic:
            if now - _last_event_trigger_time > 10:
                _last_event_trigger_time = now
                _fetch_snapshot_on_event(ev_id)
            else:
                logger.debug("⚡ Event debounce active (last snapshot was %.1fs ago)", now - _last_event_trigger_time)
        else:
            _last_event_trigger_time = now

        # 4. Publish to local HA MQTT now that the new snapshot is ready on disk
        # Alarm/Problem: tamper (12021, 12027), smoke (10005), signal loss (10101), error (10115) etc.
        is_alarm = alert_code in [10005, 10101, 10115, 12021, 12027] or "Tamper" in alert_text
        event_type = "doorbell" if is_doorbell else "motion"
        logger.info("⚡ Push Event: channel_type=%s, code=%s, type=%s, msg=%s, is_alarm=%s", channel_type, alert_code, event_type, alert_text, is_alarm)

        publish_alarm_to_ha_mqtt(
            ev_id=ev_id,
            event_type=event_type,
            alert_code=alert_code,
            alert_text=alert_text,
            is_alarm=is_alarm,
            attr_data=msg,
            push_time=push_time,
        )

        # Trigger deferred event refresh and video clip generation in background
        def _deferred_event_refresh():
            time.sleep(5)
            try:
                client = get_client()
                polled = client.get_alarm_list(max_count=5)
                if polled:
                    _event_store.add_events(polled)
                elif SAVE_EVENT_CLIPS:
                    _event_store._process_videos()
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
        ("doorbell", "Campanello", None),
        ("alarm", "Allarme", "problem")
    ]

    for s_type, s_name, s_class in sensors:
        config_topic = f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{s_type}/config"
        payload = {
            "name": s_name,
            "state_topic": f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{s_type}/state",
            "unique_id": f"ezviz_{CAMERA_SERIAL}_{s_type}",
            "device": device_info,
            "payload_on": "ON",
            "payload_off": "OFF",
            "json_attributes_topic": f"homeassistant/binary_sensor/ezviz_{CAMERA_SERIAL}_{s_type}/attributes"
        }
        if s_class:
            payload["device_class"] = s_class
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
                if ENABLE_MQTT_EVENTS:
                    need_connect = False
                    if ezviz_mqtt is None:
                        try:
                            logger.info("⚡ Subscribing to Ezviz Cloud real-time push events...")
                            ezviz_mqtt = client._client.get_mqtt_client(on_message_callback=_on_ezviz_push_message)
                            need_connect = True
                        except Exception as mqtt_err:
                            logger.error("Failed to initialize Ezviz Cloud MQTT push: %s", mqtt_err)
                    elif ezviz_mqtt.mqtt_client is None or not ezviz_mqtt.mqtt_client.is_connected():
                        logger.info("⚡ Ezviz Cloud MQTT push client disconnected, attempting reconnection...")
                        need_connect = True

                    if need_connect and ezviz_mqtt is not None:
                        try:
                            ezviz_mqtt.connect()
                            logger.info("⚡ Ezviz Cloud real-time push connected successfully")
                        except Exception as conn_err:
                            logger.error("Failed to connect Ezviz Cloud MQTT push: %s", conn_err)

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

                    # Hybrid Polling Fallback: Check if any recent event (<90s) wasn't processed by push MQTT
                    if ENABLE_MQTT_EVENTS:
                        for pe in polled_events:
                            pe_id = str(pe.get("alarm_id") or "")
                            if not pe_id:
                                continue
                            pe_time = pe.get("alarm_time") or ""
                            age_s = _event_store.event_age_seconds(pe_time)
                            if age_s > 90:
                                continue

                            with _seen_events_lock:
                                if pe_id in _seen_events:
                                    continue
                                _seen_events.add(pe_id)

                            raw_code = pe.get("sub_type") or pe.get("alarm_type") or "0"
                            try:
                                alert_code = int(raw_code)
                            except (ValueError, TypeError):
                                alert_code = 0
                            pe_type_str = str(pe.get("alarm_type") or "")
                            is_doorbell = (
                                alert_code in (2701, 20007, 10054, 10055)
                                or any(kw in pe_type_str.lower() for kw in ("doorbell", "campanello", "chiamata", "squillo", "citofono", "door_bell"))
                            )
                            event_type = "doorbell" if is_doorbell else "motion"
                            is_alarm = alert_code in [10005, 10101, 10115, 12021, 12027] or "Tamper" in pe_type_str

                            logger.info(
                                "⚡ [Polling Fallback] Detected unnotified fresh event %s (age=%.1fs, type=%s, code=%s). Triggering HA MQTT...",
                                pe_id, age_s, event_type, alert_code
                            )

                            publish_alarm_to_ha_mqtt(
                                ev_id=pe_id,
                                event_type=event_type,
                                alert_code=alert_code,
                                alert_text=pe_type_str or "Event",
                                is_alarm=is_alarm,
                                attr_data=pe,
                                push_time=pe_time,
                            )
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
            base_interval = SNAPSHOT_INTERVAL if SNAPSHOT_INTERVAL > 0 else 20
            backoff = min(300, base_interval * consecutive_errors)
            logger.warning("Many consecutive errors (%d), backing off %ds",
                          consecutive_errors, backoff)
            time.sleep(backoff)
        else:
            # Sleep SNAPSHOT_INTERVAL if > 0, otherwise poll cloud events every 20s
            sleep_time = SNAPSHOT_INTERVAL if SNAPSHOT_INTERVAL > 0 else 20
            time.sleep(sleep_time)




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
                alarms = client.get_alarm_list(max_count=10, s_type=stype)
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
    status["timestamp"] = status["last_snapshot"]
    status["snapshot_interval_s"] = SNAPSHOT_INTERVAL
    status["last_alarm_type"] = _normalize_alarm_label(_last_alarm_type)
    status["last_alarm_time"] = _last_alarm_time
    status["last_doorbell_time"] = _last_doorbell_time
    
    # Shorten last_alarm_pic to prevent HA >255 char state limit errors
    pic_url = status.get("last_alarm_pic", "")
    if pic_url and len(pic_url) > 200:
        events = _event_store.get_all()
        if events:
            status["last_alarm_pic"] = ingress_url(f"/api/events/image/{events[0]['alarm_id']}")
        else:
            status["last_alarm_pic"] = ingress_url("/api/snapshot") # Safe fallback

    return jsonify(status)


@app.route("/api/events")
@app.route("/api/events/list")
def api_events():
    """Return recent alarm events as JSON."""
    logger.info("⚡ Request: /api/events")
    events = _event_store.get_all()
    
    # Shorten URLs to prevent HA >255 char state errors if mapped
    clean_events = []
    for ev in events:
        clean_ev = dict(ev)
        clean_ev["alarm_type"] = _normalize_alarm_label(clean_ev.get("alarm_type", "Event"))
        pic_url = clean_ev.get("alarm_pic_url", "")
        if pic_url and len(pic_url) > 200:
            clean_ev["alarm_pic_url"] = ingress_url(f"/api/events/image/{ev['alarm_id']}")
        v_file = EVENT_VIDEO_PATH / f"video_{ev['alarm_id']}.mp4"
        tmp_file = EVENT_VIDEO_PATH / f"tmp_{ev['alarm_id']}.mp4"
        ev_time = clean_ev.get("alarm_time", "")
        clean_ev["local_video"] = _event_store.is_valid_video(v_file, ev_time)
        clean_ev["video_recording"] = tmp_file.exists() or (
            not clean_ev["local_video"]
            and SAVE_EVENT_CLIPS
            and _event_store.event_age_seconds(ev_time) <= 75
        )
        clean_events.append(clean_ev)
        
    return jsonify({
        "events": clean_events,
        "count": len(clean_events),
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


@app.route("/api/events/clip/<alarm_id>")
def api_event_clip(alarm_id):
    """Serve a locally-downloaded event MP4 video clip, generating only if the event is currently active."""
    filename = f"video_{alarm_id}.mp4"
    filepath = EVENT_VIDEO_PATH / filename
    event = None
    for e in _event_store.get_all():
        if str(e.get("alarm_id")) == str(alarm_id):
            event = e
            break
    ev_time = event.get("alarm_time", "") if event else ""

    # 1. If clip already exists and was recorded during the event window, return it as attachment
    if _event_store.is_valid_video(filepath, ev_time):
        return send_file(filepath, mimetype="video/mp4", as_attachment=True, download_name=f"event_{alarm_id}.mp4")
    
    # 2. Check query parameter: check=true
    check_only = request.args.get("check", "false").lower() == "true"
    if check_only:
        return jsonify({"ready": False, "status": "processing"}), 200

    # 3. Generate/extract clip from CloudPlay/SD playback (or active recording) for recent events (<= 24h)
    tmp_file = EVENT_VIDEO_PATH / f"tmp_{alarm_id}.mp4"
    if tmp_file.exists() or (ev_time and _event_store.event_age_seconds(ev_time) <= 86400):
        logger.info("⚡ Waiting/extracting playback clip for alarm %s", alarm_id)
        success = _event_store.generate_clip(alarm_id)
        if success and _event_store.is_valid_video(filepath, ev_time):
            return send_file(filepath, mimetype="video/mp4", as_attachment=True, download_name=f"event_{alarm_id}.mp4")

    return jsonify({
        "error": "Registrazione video MP4 non disponibile per questo evento.",
        "ready": False,
    }), 404


@app.route("/api/events/video/<alarm_id>")
def api_event_video(alarm_id):
    """Serve the recorded event MP4 video stream (never redirect to the live camera feed)."""
    logger.info("⚡ Request event video playback for alarm %s", alarm_id)
    filepath = EVENT_VIDEO_PATH / f"video_{alarm_id}.mp4"
    event = None
    for e in _event_store.get_all():
        if str(e.get("alarm_id")) == str(alarm_id):
            event = e
            break
    ev_time = event.get("alarm_time", "") if event else ""

    if _event_store.is_valid_video(filepath, ev_time):
        return send_file(filepath, mimetype="video/mp4", conditional=True)

    tmp_file = EVENT_VIDEO_PATH / f"tmp_{alarm_id}.mp4"
    if tmp_file.exists() or (ev_time and _event_store.event_age_seconds(ev_time) <= 86400):
        logger.info("⚡ Extracting/waiting for event %s playback clip...", alarm_id)
        if _event_store.generate_clip(alarm_id) and _event_store.is_valid_video(filepath, ev_time):
            return send_file(filepath, mimetype="video/mp4", conditional=True)

    return jsonify({
        "error": "Registrazione video non disponibile per questo evento (solo istantanea salvata).",
        "alarm_id": alarm_id,
    }), 404


@app.route("/api/live/stream.m3u8")
def api_live_hls():
    """Return redirect to live HLS (.m3u8) stream for Home Assistant with wake-up verification."""
    try:
        client = get_client()
        stream_url = client.get_hls_stream_url()
        if not stream_url:
            return jsonify({"error": "Could not obtain live HLS stream from Ezviz"}), 502

        # Verify that camera is awake and stream is ready (wait up to 25 seconds for battery deep sleep)
        for attempt in range(15):
            try:
                check_resp = requests.get(stream_url, timeout=5, verify=False, allow_redirects=True)
                content = check_resp.text or ""
                is_err = "ErrCode" in content or "EXT-X-ENDLIST" in content
                has_ts = ".ts" in content

                if check_resp.status_code == 200 and has_ts and not is_err:
                    logger.info("⚡ Live HLS stream is awake and ready on attempt %d (%d bytes playlist)", attempt + 1, len(content))
                    return redirect(stream_url)
                elif is_err:
                    logger.info("⚡ Camera wake-up in progress (attempt %d/15)... waiting 1.5s", attempt + 1)
                    time.sleep(1.5)
                else:
                    logger.info("⚡ Waiting for video frames (attempt %d/15)...", attempt + 1)
                    time.sleep(1.5)
            except Exception as ce:
                logger.info("HLS pre-check waiting (attempt %d/15): %s", attempt + 1, ce)
                time.sleep(1.5)

        logger.warning("Camera wake-up timed out after 25s. Serving redirect as last resort.")
        return redirect(stream_url)
    except Exception as e:
        logger.error("Error generating live HLS stream: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/player")
def html5_player():
    """Universal HTML5 video player page: HLS for Live stream, native MP4 for recorded events."""
    alarm_id = request.args.get("alarm_id", "")
    is_live = request.args.get("live", "false").lower() == "true" or not alarm_id
    
    if is_live:
        raw_path = "/api/live/stream.m3u8"
        title = f"Live Stream — {CAMERA_SERIAL}"
    else:
        event_time_label = ""
        for e in _event_store.get_all():
            if str(e.get("alarm_id")) == str(alarm_id):
                event_time_label = f" ({e.get('alarm_time', '')})"
                break
        raw_path = f"/api/events/video/{alarm_id}"
        title = f"Registrazione Evento{event_time_label}"

    ingress_path = ingress_url(raw_path)
    is_live_js = "true" if is_live else "false"

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <style>
    body {{
      margin: 0;
      background: #000;
      color: #fff;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }}
    .player-container {{
      width: 100%;
      max-width: 960px;
      padding: 10px;
      box-sizing: border-box;
      text-align: center;
    }}
    video {{
      width: 100%;
      height: auto;
      max-height: 80vh;
      background: #111;
      border-radius: 8px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.8);
    }}
    h1 {{
      font-size: 16px;
      font-weight: 500;
      margin-bottom: 12px;
      color: #aaa;
    }}
    #error-box {{
      display: none;
      margin-top: 16px;
      padding: 14px 18px;
      border-radius: 8px;
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid rgba(239, 68, 68, 0.4);
      color: #fca5a5;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <div class="player-container">
    <h1>{title}</h1>
    <video id="video" controls autoplay playsinline></video>
    <div id="error-box"></div>
  </div>
  <script>
    const video = document.getElementById('video');
    const errorBox = document.getElementById('error-box');
    const isIngress = window.location.pathname.includes('/api/hassio_ingress/');
    const videoSrc = isIngress ? '{ingress_path}' : '{raw_path}';
    const isLive = {is_live_js};

    function showError(msg) {{
      video.style.display = 'none';
      errorBox.style.display = 'block';
      errorBox.textContent = msg;
    }}

    if (!isLive) {{
      // Recorded event clip is a standard MP4 file
      video.src = videoSrc;
      video.addEventListener('loadedmetadata', function() {{
        video.play().catch(e => console.log('Autoplay prevented:', e));
      }});
      video.addEventListener('error', function() {{
        fetch(videoSrc)
          .then(r => r.json())
          .then(d => showError(d.error || 'Registrazione video non disponibile per questo evento.'))
          .catch(() => showError('Registrazione video non disponibile per questo evento.'));
      }});
    }} else if (Hls.isSupported()) {{
      const hls = new Hls({{
        enableWorker: true,
        lowLatencyMode: false,
        manifestLoadingTimeOut: 15000,
        manifestLoadingMaxRetry: 10,
        manifestLoadingRetryDelay: 1500,
        levelLoadingTimeOut: 15000,
        levelLoadingMaxRetry: 8,
        fragLoadingTimeOut: 20000,
        fragLoadingMaxRetry: 8
      }});
      hls.loadSource(videoSrc);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, function() {{
        video.play().catch(e => console.log('Autoplay prevented:', e));
      }});
      hls.on(Hls.Events.ERROR, function(event, data) {{
        console.warn('HLS stream notice:', data);
        if (data.fatal) {{
          switch (data.type) {{
            case Hls.ErrorTypes.NETWORK_ERROR:
              console.log('Network error, retrying...');
              hls.startLoad();
              break;
            case Hls.ErrorTypes.MEDIA_ERROR:
              console.log('Media error, recovering...');
              hls.recoverMediaError();
              break;
            default:
              hls.destroy();
              break;
          }}
        }}
      }});
    }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
      // Native Safari / iOS HLS
      video.src = videoSrc;
      video.addEventListener('loadedmetadata', function() {{
        video.play().catch(e => console.log('Autoplay prevented:', e));
      }});
    }}
  </script>
</body>
</html>"""



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
