"""Face detection + embedding (YuNet + SFace via OpenCV).

Used both for enrollment (offline, from images/video) and runtime
recognition (per-frame, driven by recognizer.py).
"""
import threading
from pathlib import Path

import cv2
import numpy as np

from .config import (
    FACE_DETECT_MODEL, FACE_EMBED_MODEL,
    FACE_MIN_SIZE, ENROLL_MAX_SAMPLES,
)

_engine = None
_engine_lock = threading.Lock()


def is_available() -> bool:
    return hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")


class FaceEngine:
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

        self.detector = cv2.FaceDetectorYN.create(
            detector_path, "", (320, 320), 0.9, 0.3, 5000,
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


def _face_quality_ok(face: np.ndarray, image_bgr: np.ndarray) -> bool:
    x, y, w, h = [int(v) for v in face[:4]]
    if w < FACE_MIN_SIZE or h < FACE_MIN_SIZE:
        return False
    if float(face[14]) < 0.9:               # detector confidence
        return False
    ih, iw = image_bgr.shape[:2]
    crop = image_bgr[max(0, y):min(ih, y + h), max(0, x):min(iw, x + w)]
    if crop.size == 0:
        return False
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return cv2.Laplacian(gray, cv2.CV_64F).var() >= 100   # sharpness


def _extract_one(img: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """Return [(aligned_crop, embedding)] only if exactly ONE good face."""
    engine = get_engine()
    faces = engine.detect(img)
    good = [f for f in faces if _face_quality_ok(f, img)]
    if len(good) != 1:
        return []                                     # 0 = no face, >1 = ambiguous
    face = good[0]
    crop = engine.recognizer.alignCrop(img, face)
    emb = engine.embed(img, face)
    return [(crop, emb)]


def extract_from_image(image_bytes: bytes) -> list[tuple[np.ndarray, np.ndarray]]:
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return []
    return _extract_one(img)


def extract_from_video(
    video_path: str, max_samples: int = ENROLL_MAX_SAMPLES,
) -> list[tuple[np.ndarray, np.ndarray]]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, int(round(fps / 3)))                # sample ~3 fps
    samples: list[tuple[np.ndarray, np.ndarray]] = []
    idx = 0
    try:
        while len(samples) < max_samples:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                samples.extend(_extract_one(frame))
            idx += 1
    finally:
        cap.release()
    return samples