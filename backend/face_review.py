"""Opt-in, bounded queue of enrollment-ready but unrecognised face crops."""
import json
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from . import db, enroll


ROOT = Path("data/face_candidates")
KEY = "face_review_policy"
MAX_CANDIDATES = 300
TRACK_COOLDOWN = 600.0
DEDUP_SIMILARITY = .85
_lock = threading.Lock()
_last_track = {}
_last_attempt = {}


def policy():
    raw = db.get_setting(KEY)
    if raw:
        try:
            value = json.loads(raw)
            if (isinstance(value, dict) and isinstance(value.get("enabled"), bool)
                    and isinstance(value.get("per_hour"), int)
                    and 1 <= value["per_hour"] <= 30
                    and isinstance(value.get("overrides"), dict)):
                return value
        except (ValueError, TypeError):
            pass
    return {"enabled": False, "per_hour": 2, "overrides": {}}


def effective(camera_id, settings=None):
    settings = settings if settings is not None else policy()
    local = settings["overrides"].get(camera_id, {})
    return {"enabled": bool(settings["enabled"] and local.get("enabled", True)),
            "per_hour": local.get("per_hour", settings["per_hour"])}


def save_policy(scope, enabled, per_hour, camera_ids=()):
    if (scope not in ("global", "exclusive") or not isinstance(enabled, bool)
            or isinstance(per_hour, bool) or not isinstance(per_hour, int)
            or not 1 <= per_hour <= 30):
        raise ValueError("Invalid face review policy")
    with _lock:
        settings = policy()
        if scope == "global":
            settings = {"enabled": enabled, "per_hour": per_hour, "overrides": {}}
        else:
            if not camera_ids:
                raise ValueError("Choose at least one camera")
            for camera_id in camera_ids:
                settings["overrides"][camera_id] = {
                    "enabled": enabled, "per_hour": per_hour}
        db.set_settings({KEY: json.dumps(settings)})
    return settings


def remove_camera(camera_id):
    with _lock:
        settings = policy()
        if camera_id in settings["overrides"]:
            del settings["overrides"][camera_id]
            db.set_settings({KEY: json.dumps(settings)})


def maybe_capture(camera_id, track_id, face_image, embedding, now=None):
    """Keep only a single, sharp, enrollable face and suppress near duplicates."""
    now = time.monotonic() if now is None else now
    with _lock:
        if len(_last_attempt) > 2000:
            for cache in (_last_attempt, _last_track):
                for key, seen_at in list(cache.items()):
                    if now - seen_at > TRACK_COOLDOWN:
                        cache.pop(key, None)
        settings = effective(camera_id)
        if not settings["enabled"]:
            return None
        key = (camera_id, track_id)
        if (now - _last_track.get(key, float("-inf")) < TRACK_COOLDOWN
                or now - _last_attempt.get(key, float("-inf")) < 3.0
                or db.face_candidate_count() >= MAX_CANDIDATES
                or db.face_capture_count(camera_id) >= settings["per_hour"]):
            return None
        if (face_image is None or face_image.size == 0
                or embedding is None or not np.all(np.isfinite(embedding))):
            return None
        _last_attempt[key] = now
        # The same accepted image must also pass the regular enrollment route.
        ok, encoded = cv2.imencode(".jpg", face_image,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 93])
        if not ok:
            return None
        samples, _ = enroll.extract_from_image(encoded.tobytes())
        if len(samples) != 1:
            return None
        approved_embedding = samples[0][1]
        if any(float(other @ approved_embedding) >= DEDUP_SIMILARITY
               for other in db.recent_face_embeddings()):
            _last_track[key] = now
            return None
        ROOT.mkdir(parents=True, exist_ok=True)
        candidate_id = uuid.uuid4().hex
        path = ROOT / f"{candidate_id}.jpg"
        path.write_bytes(encoded.tobytes())
        try:
            db.add_face_candidate(candidate_id, camera_id, str(path),
                                  approved_embedding)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        _last_track[key] = now
        return candidate_id


def discard(candidate_id):
    path = db.remove_face_candidate(candidate_id)
    if path:
        Path(path).unlink(missing_ok=True)
    return path is not None
