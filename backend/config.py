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

# --- Face recognition ---
FACE_DETECT_MODEL = os.getenv(
    "FACE_DETECT_MODEL", "data/face_detection_yunet_2023mar.onnx")
FACE_EMBED_MODEL = os.getenv(
    "FACE_EMBED_MODEL", "data/face_recognition_sface_2021dec.onnx")
FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "0.40"))
FACE_MATCH_HIGH = float(os.getenv("FACE_MATCH_HIGH", "0.55"))
FACE_MIN_SIZE = int(os.getenv("FACE_MIN_SIZE", "64"))
ENROLL_MAX_SAMPLES = int(os.getenv("ENROLL_MAX_SAMPLES", "30"))