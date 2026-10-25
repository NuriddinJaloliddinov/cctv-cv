"""Central configuration."""
import os
from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
DEFAULT_FPS = int(os.getenv("DEFAULT_FPS", "10"))
MODEL_NAME = os.getenv("MODEL_NAME", "data/yolov8n.onnx")
CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.4"))
DATA_DIR = "data"
ONVIF_USER = os.getenv("ONVIF_USER", "")
ONVIF_PASS = os.getenv("ONVIF_PASS", "")

# --- Authentication ---
# Bootstrap administrator. It is created only when the users table is empty.
AUTH_DEFAULT_USERNAME = "admin"
AUTH_DEFAULT_PASSWORD = "12345"
# Opaque login session lifetime. Expired sessions are rejected server-side.
AUTH_SESSION_HOURS = float(os.getenv("CCTV_SESSION_HOURS", "12"))
# PBKDF2-HMAC-SHA256 work factor for stored password hashes.
AUTH_HASH_ITERATIONS = int(os.getenv("CCTV_AUTH_ITERATIONS", "240000"))
# Lock an account after this many consecutive wrong passwords.
AUTH_MAX_FAILED_ATTEMPTS = int(os.getenv("CCTV_MAX_FAILED_ATTEMPTS", "3"))
# "auto" marks cookies Secure when the request is HTTPS (including a trusted
# X-Forwarded-Proto proxy). Set to "1"/"0" to force the behaviour.
AUTH_SECURE_COOKIE = os.getenv("CCTV_SECURE_COOKIE", "auto").lower()


# --- Streaming / latency ---
# Longest side of the frame that is processed and sent to the browser.
# Lower values reduce CPU and bandwidth; 0 keeps the camera resolution.
STREAM_MAX_WIDTH = int(os.getenv("STREAM_MAX_WIDTH", "1280"))
# Longest side used for inference (person/phone detection, phone crops and
# face matching). 0 keeps the camera's native resolution. This is separate
# from STREAM_MAX_WIDTH because downscaling a 4K stream for the browser also
# erases the detail that small-object detection depends on.
DETECT_MAX_WIDTH = int(os.getenv("DETECT_MAX_WIDTH", "1920"))
# Seconds between person-detection runs. 0 = run on every output frame.
DETECT_INTERVAL = float(os.getenv("DETECT_INTERVAL", "0.2"))
# Seconds between face-recognition runs (only when a person is detected).
FACE_INTERVAL = float(os.getenv("FACE_INTERVAL", "1.0"))
# Identity continuity within one camera. Visual matching is a short-range,
# appearance-only fallback, never an independent source of identity.
TRACK_MAX_GAP = float(os.getenv("TRACK_MAX_GAP", "2.0"))
TRACK_IDENTITY_TTL = float(os.getenv("TRACK_IDENTITY_TTL", "30.0"))
TRACK_VISUAL_THRESHOLD = float(os.getenv("TRACK_VISUAL_THRESHOLD", "0.88"))
# RTSP transport: "tcp" is reliable, "udp" is lower latency on a clean LAN.
RTSP_TRANSPORT = os.getenv("RTSP_TRANSPORT", "tcp").lower()
# Ask FFmpeg to favour latency over buffering (recommended).
RTSP_LOW_LATENCY = os.getenv("RTSP_LOW_LATENCY", "1").lower() not in (
    "0", "false", "no", "off")

# --- Phone-use detection ---
# Full-frame COCO "cell phone" confidence. Phones are small, so this is lower
# than the person threshold; the geometric checks below reject noise.
PHONE_CONF_THRESHOLD = float(os.getenv("PHONE_CONF_THRESHOLD", "0.25"))
# Phones are tiny at full-frame scale, so each person is also re-scanned in a
# tighter crop. The crop pass can use a lower threshold without adding many
# full-frame false positives.
PHONE_CROP_CONF_THRESHOLD = float(
    os.getenv("PHONE_CROP_CONF_THRESHOLD", "0.15"))
# Largest number of people per frame to re-scan; limits CPU on crowded scenes.
PHONE_CROP_MAX_PEOPLE = int(os.getenv("PHONE_CROP_MAX_PEOPLE", "4"))
# The frame is also searched in a grid of tiles, so a phone held away from
# the body, or belonging to someone crowded out of the crop budget, is still
# looked for at close range. 0 disables the tile pass.
PHONE_TILE_GRID = int(os.getenv("PHONE_TILE_GRID", "2"))
# Tiles scanned per pass, busiest first (bounds CPU on crowded scenes).
PHONE_TILE_MAX = int(os.getenv("PHONE_TILE_MAX", "1"))
# Margin around each tile as a fraction of its size, so a phone sitting on a
# tile boundary is not cut in half.
PHONE_TILE_OVERLAP = float(os.getenv("PHONE_TILE_OVERLAP", "0.15"))
# Confidence for the tile pass; tiles are larger than person crops, so this
# sits between the full-frame and crop thresholds.
PHONE_TILE_CONF_THRESHOLD = float(
    os.getenv("PHONE_TILE_CONF_THRESHOLD", "0.20"))
# How often (seconds) to run the phone/person geometric analysis.
PHONE_USE_INTERVAL = float(os.getenv("PHONE_USE_INTERVAL", "0.5"))
# Phone use must persist this long before an alert is raised.
PHONE_USE_MIN_SECONDS = float(os.getenv("PHONE_USE_MIN_SECONDS", "2.0"))
# Short detector dropouts are tolerated for this long.
PHONE_USE_GRACE_SECONDS = float(os.getenv("PHONE_USE_GRACE_SECONDS", "1.0"))
# A phone-use episode survives a detector dropout of up to this long
# (seconds) instead of restarting its timer, then ends.
PHONE_USE_EPISODE_SECONDS = float(
    os.getenv("PHONE_USE_EPISODE_SECONDS", "3.0"))
# Share of the recent analyses that must show phone use before alerting.
PHONE_USE_EVIDENCE = float(os.getenv("PHONE_USE_EVIDENCE", "0.5"))
# Do not alert again for the same track within this many seconds.
PHONE_ALERT_COOLDOWN = float(os.getenv("PHONE_ALERT_COOLDOWN", "120"))
# Head-pitch ratio (0 = eye line, 1 = mouth line). It rises as the head tilts
# down, so a value at or above this counts as looking at the phone. 0.70 is
# high enough to ignore someone who is merely holding a phone while walking.
PHONE_LOOK_PITCH_RATIO = float(os.getenv("PHONE_LOOK_PITCH_RATIO", "0.70"))
# Optional, independent person-crop classifier. Only loaded when the weights
# exist; it can catch phone use even when the COCO phone box is missed.
PHONE_CLASSIFIER_MODEL = os.getenv(
    "PHONE_CLASSIFIER_MODEL", "data/phone_detection_model.tflite")
PHONE_CLASSIFIER_THRESHOLD = float(os.getenv("PHONE_CLASSIFIER_THRESHOLD", "0.80"))
PHONE_CLASSIFIER_MAX_PEOPLE = int(os.getenv("PHONE_CLASSIFIER_MAX_PEOPLE", "4"))
# Review samples contain sensitive camera imagery; collection is opt-in.
REVIEW_CAPTURE = os.getenv("REVIEW_CAPTURE", "0").lower() in ("1", "true", "yes")
REVIEW_MAX_SAMPLES = int(os.getenv("REVIEW_MAX_SAMPLES", "2000"))
REVIEW_CAPTURE_PER_HOUR = int(os.getenv("REVIEW_CAPTURE_PER_HOUR", "1"))

# --- Face recognition ---
FACE_DETECT_MODEL = os.getenv(
    "FACE_DETECT_MODEL", "data/face_detection_yunet_2023mar.onnx")
FACE_EMBED_MODEL = os.getenv(
    "FACE_EMBED_MODEL", "data/face_recognition_sface_2021dec.onnx")
FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "0.40"))
FACE_MATCH_HIGH = float(os.getenv("FACE_MATCH_HIGH", "0.55"))
FACE_MIN_SIZE = int(os.getenv("FACE_MIN_SIZE", "64"))
ENROLL_MAX_SAMPLES = int(os.getenv("ENROLL_MAX_SAMPLES", "30"))
