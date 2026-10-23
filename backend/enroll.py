"""Face detection + embedding (YuNet + SFace via OpenCV).

Handles:
- JPEG / PNG / BMP / TIFF / WebP via OpenCV fast path
- HEIC / HEIF via Pillow + pillow-heif fallback (registered on import)
- EXIF rotation applied automatically (via Pillow on the fallback path,
  via 90/180/270 retry on the OpenCV path)
- Downscaling very large images so YuNet doesn't choke
- Per-file failure reasons so the API can tell users WHAT went wrong
"""
import io
import threading
from pathlib import Path

import cv2
import numpy as np

from .config import (
    FACE_DETECT_MODEL, FACE_EMBED_MODEL,
    FACE_MIN_SIZE, ENROLL_MAX_SAMPLES,
)

# --- Tunables ---
_MIN_SHARPNESS = 30.0           # Laplacian variance; >=30 = "not blurry"
_MAX_DIM = 1920                 # downscale long side above this before detection
_MIN_DETECT_SCORE = 0.5         # secondary filter on top of YuNet's own score

# --- Optional HEIC/HEIF support (silent no-op if pillow-heif absent) ---
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


_engine = None
_engine_lock = threading.Lock()


def is_available() -> bool:
    """True iff cv2 ships YuNet + SFace (contrib build)."""
    return hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")


class FaceEngine:
    """Thin wrapper around YuNet (detect) + SFace (embed)."""

    def __init__(self, detector_path: str, embedder_path: str):
        if not is_available():
            raise RuntimeError(
                "cv2.FaceDetectorYN / FaceRecognizerSF missing. "
                "Install opencv-contrib-python-headless and restart."
            )
        if not Path(detector_path).exists():
            raise RuntimeError(f"Face detector model missing: {detector_path}")
        if not Path(embedder_path).exists():
            raise RuntimeError(f"Face embedder model missing: {embedder_path}")

        # score_threshold=0.7 (was 0.9) — more tolerant, negligible false positives
        self.detector = cv2.FaceDetectorYN.create(
            detector_path, "", (320, 320), 0.7, 0.3, 5000,
        )
        self.recognizer = cv2.FaceRecognizerSF.create(embedder_path, "")

    def detect(self, image_bgr: np.ndarray) -> list[np.ndarray]:
        h, w = image_bgr.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(image_bgr)
        if faces is None:
            return []
        return [faces[i] for i in range(len(faces))]

    def embed(self, image_bgr: np.ndarray, face_row: np.ndarray) -> np.ndarray:
        aligned = self.recognizer.alignCrop(image_bgr, face_row)
        feat = self.recognizer.feature(aligned).flatten().astype(np.float32)
        n = np.linalg.norm(feat)
        return feat / n if n > 0 else feat


def get_engine() -> FaceEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = FaceEngine(FACE_DETECT_MODEL, FACE_EMBED_MODEL)
        return _engine


# ---------------------------------------------------------------------------
#  Image decoding
# ---------------------------------------------------------------------------

def _decode_image(image_bytes: bytes) -> np.ndarray | None:
    """Decode supported image formats to BGR numpy.

    Fast path: OpenCV (JPEG, PNG, BMP, TIFF, WebP).
    Slow path: Pillow — adds HEIC/HEIF (via pillow-heif), applies EXIF
    rotation, and handles CMYK/RGBA/palette modes that cv2 mishandles.
    """
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is not None:
        return img

    try:
        from PIL import Image, ImageOps
        pil = Image.open(io.BytesIO(image_bytes))
        pil = ImageOps.exif_transpose(pil)      # honour phone orientation
        if pil.mode != "RGB":
            pil = pil.convert("RGB")
        return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def _downscale(img: np.ndarray) -> np.ndarray:
    """Shrink very large images so YuNet doesn't silently fail."""
    h, w = img.shape[:2]
    m = max(h, w)
    if m <= _MAX_DIM:
        return img
    scale = _MAX_DIM / m
    return cv2.resize(img, (int(w * scale), int(h * scale)),
                      interpolation=cv2.INTER_AREA)


# ---------------------------------------------------------------------------
#  Sample extraction
# ---------------------------------------------------------------------------

def _extract_one(img: np.ndarray) -> tuple[list, str | None]:
    """Return (samples, reason). reason is None on success, else why not.

    Requires exactly one face that is large enough, sharp enough, and
    high-confidence. Returns [] with a reason otherwise.
    """
    engine = get_engine()
    faces = engine.detect(img)
    n = len(faces)

    if n == 0:
        return [], "no face detected"
    if n > 1:
        return [], f"{n} faces detected (need exactly 1)"

    face = faces[0]
    x, y, w, h = [int(v) for v in face[:4]]
    score = float(face[14])

    if w < FACE_MIN_SIZE or h < FACE_MIN_SIZE:
        return [], f"face too small ({w}x{h}, need >= {FACE_MIN_SIZE})"
    if score < _MIN_DETECT_SCORE:
        return [], f"low confidence ({score:.2f})"

    ih, iw = img.shape[:2]
    crop = img[max(0, y):min(ih, y + h), max(0, x):min(iw, x + w)]
    if crop.size == 0:
        return [], "face crop empty (bbox out of bounds)"

    sharpness = cv2.Laplacian(
        cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F
    ).var()
    if sharpness < _MIN_SHARPNESS:
        return [], (f"face too blurry "
                    f"(sharpness {sharpness:.0f}, need >= {_MIN_SHARPNESS:.0f})")

    aligned = engine.recognizer.alignCrop(img, face)
    emb = engine.embed(img, face)
    return [(aligned, emb)], None


def _try_all_orientations(img: np.ndarray) -> tuple[list, str | None]:
    """cv2.imdecode ignores EXIF. If the first pass finds no face, try 90/180/270.

    This covers the case where a portrait phone photo went through the cv2
    fast path (which skips Pillow's exif_transpose), so the image is sideways.
    """
    samples, reason = _extract_one(img)
    if samples:
        return samples, None

    rotations = [
        ("90°",  cv2.ROTATE_90_CLOCKWISE),
        ("180°", cv2.ROTATE_180),
        ("270°", cv2.ROTATE_90_COUNTERCLOCKWISE),
    ]
    for _, code in rotations:
        rotated = cv2.rotate(img, code)
        s, _ = _extract_one(rotated)
        if s:
            return s, None

    return [], (reason or "no face detected") + " (tried all 4 orientations)"


def extract_from_image(image_bytes: bytes) -> tuple[list, str | None]:
    """Extract (aligned_crop, embedding) tuples from a single image.

    Returns (samples, reason). samples is non-empty on success; reason is
    a human-readable explanation on failure.
    """
    if not image_bytes:
        return [], "empty file"

    img = _decode_image(image_bytes)
    if img is None:
        # Helpful hint for the most common "cv2 can't read this" case
        if len(image_bytes) > 12 and image_bytes[4:8] == b"ftyp":
            return [], ("HEIC/HEIF/AVIF not supported — install pillow-heif "
                        "(pip install pillow-heif) or convert to JPEG first")
        return [], "could not decode image (unsupported format or corrupt file)"

    img = _downscale(img)
    return _try_all_orientations(img)


def extract_from_video(
    video_path: str, max_samples: int = ENROLL_MAX_SAMPLES,
) -> tuple[list, str | None]:
    """Sample ~3 fps from the video and collect usable single-face frames."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return [], "could not open video"

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, int(round(fps / 3)))
    samples: list = []
    frames_sampled = 0
    last_reason = "no face detected"

    idx = 0
    try:
        while len(samples) < max_samples:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                frames_sampled += 1
                frame = _downscale(frame)
                s, reason = _extract_one(frame)
                if s:
                    samples.extend(s)
                elif reason:
                    last_reason = reason
            idx += 1
    finally:
        cap.release()

    if not samples:
        return [], f"{last_reason} (across {frames_sampled} sampled frames)"

    return samples, None