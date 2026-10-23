"""Runtime face recognition against enrolled persons."""
import threading

import numpy as np

from . import db
from .config import FACE_MATCH_THRESHOLD
from .enroll import get_engine

_matcher = None
_lock = threading.Lock()


class Matcher:
    """Caches all enrolled embeddings; reloads when DB version changes."""

    def __init__(self):
        self._embs: np.ndarray | None = None
        self._names: list[str] = []
        self._version = -1

    def _refresh_if_needed(self):
        v = db.embeddings_version()
        if v == self._version:
            return
        embs, names = db.load_all_embeddings()
        self._embs = embs if embs.size else None
        self._names = names
        self._version = v

    def match(self, embedding: np.ndarray) -> tuple[str, float] | None:
        self._refresh_if_needed()
        if self._embs is None:
            return None
        sims = self._embs @ embedding               # both L2-normalised → cosine
        idx = int(np.argmax(sims))
        score = float(sims[idx])
        return (self._names[idx], score) if score >= FACE_MATCH_THRESHOLD else None


def get_matcher() -> Matcher:
    global _matcher
    with _lock:
        if _matcher is None:
            _matcher = Matcher()
        return _matcher


def identify(frame: np.ndarray, boxes: list) -> list:
    """For each YOLO person box, return (name, score) or None."""
    try:
        engine = get_engine()
    except RuntimeError:
        return [None] * len(boxes)

    matcher = get_matcher()
    results: list = [None] * len(boxes)
    fh, fw = frame.shape[:2]

    for i, (x1, y1, x2, y2, _) in enumerate(boxes):
        try:
            w, h = x2 - x1, y2 - y1
            pad = int(0.1 * max(w, h))
            cx1, cy1 = max(0, x1 - pad), max(0, y1 - pad)
            cx2, cy2 = min(fw, x2 + pad), min(fh, y1 + int(0.6 * h))
            crop = frame[cy1:cy2, cx1:cx2]
            if crop.size == 0:
                continue
            faces = engine.detect(crop)
            if not faces:
                continue
            face = max(faces, key=lambda f: f[2] * f[3])
            emb = engine.embed(crop, face)
            m = matcher.match(emb)
            if m is not None:
                results[i] = m
        except Exception:
            continue                                  # per-box resilience
    return results