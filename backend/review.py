"""Bounded, opt-in active-learning snapshots; no automatic model updates."""
import threading
import time
import uuid
import json
from pathlib import Path

import cv2

from . import db
from .config import (REVIEW_CAPTURE, REVIEW_MAX_SAMPLES, REVIEW_CAPTURE_PER_HOUR,
                      PHONE_CLASSIFIER_THRESHOLD)


ROOT = Path("data/review")
LABELS = {"unreviewed", "calling", "looking", "no_use", "no_person", "unclear"}
_capture_lock = threading.Lock()
_last_capture = {}


def capture_policy():
    values = db.get_settings(["review_capture", "review_capture_per_hour",
                              "review_capture_overrides"])
    try:
        overrides = json.loads(values.get("review_capture_overrides", "{}"))
        if not isinstance(overrides, dict):
            overrides = {}
    except (ValueError, TypeError):
        overrides = {}
    return {
        "enabled": (values["review_capture"] == "1" if "review_capture" in values
                    else REVIEW_CAPTURE),
        "max_per_hour": int(values.get("review_capture_per_hour",
                                       REVIEW_CAPTURE_PER_HOUR)),
        "overrides": overrides,
    }


def capture_for_camera(camera_id):
    policy = capture_policy()
    override = policy["overrides"].get(camera_id, {})
    return (policy["enabled"] and override.get("enabled", True),
            override.get("max_per_hour", policy["max_per_hour"]))


def capture_enabled(camera_id=None):
    return (capture_policy()["enabled"] if camera_id is None
            else capture_for_camera(camera_id)[0])


def set_capture_enabled(enabled):
    set_capture_policy("global", enabled, capture_policy()["max_per_hour"])


def set_capture_policy(scope, enabled, max_per_hour, camera_ids=()):
    if scope not in ("global", "exclusive") or not 1 <= max_per_hour <= 120:
        raise ValueError("Invalid capture policy")
    with _capture_lock:
        if scope == "global":
            # Global changes replace old exceptions, so 'all' means all.
            db.set_settings({"review_capture": "1" if enabled else "0",
                             "review_capture_per_hour": str(max_per_hour),
                             "review_capture_overrides": "{}"})
        else:
            if not camera_ids:
                raise ValueError("Select at least one camera")
            policy = capture_policy()
            overrides = policy["overrides"]
            for camera_id in camera_ids:
                overrides[camera_id] = {"enabled": enabled,
                                        "max_per_hour": max_per_hour}
            db.set_settings({"review_capture_overrides": json.dumps(overrides)})
    return capture_policy()


def save_image(camera_id, frame, source="upload", score=None, box=None):
    """Store one single-person image; crop before resizing to preserve detail."""
    if sum(db.review_counts().values()) >= REVIEW_MAX_SAMPLES:
        raise ValueError("Review storage limit reached; delete old images first")
    if box is not None:
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = (int(x) for x in box[:4])
        pw, ph = x2 - x1, y2 - y1
        x1, x2 = max(0, x1 - int(.12 * pw)), min(width, x2 + int(.12 * pw))
        y1, y2 = max(0, y1 - int(.08 * ph)), min(height, y2 + int(.08 * ph))
        frame = frame[y1:y2, x1:x2]
    if frame is None or frame.size == 0 or min(frame.shape[:2]) < 32:
        raise ValueError("A single-person image of at least 32 pixels is required")
    if frame.shape[0] * frame.shape[1] > 24_000_000:
        raise ValueError("Image too large (maximum 24 megapixels)")
    if frame.shape[1] > 1200:
        frame = cv2.resize(frame, (1200, max(1, int(
            frame.shape[0] * 1200 / frame.shape[1]))),
            interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        raise ValueError("Could not encode review image")
    ROOT.mkdir(parents=True, exist_ok=True)
    sample_id = uuid.uuid4().hex
    path = ROOT / f"{sample_id}.jpg"
    path.write_bytes(encoded.tobytes())
    try:
        db.add_review_sample(sample_id, camera_id, source, str(path), score)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return sample_id


def maybe_capture(camera_id, frame, people, scores, track_ids):
    """Sample uncertain predictions and occasional low-score cases.

    Throttling is per camera and per track. Low-score examples are important:
    near-threshold-only sampling cannot recover very distant false negatives.
    """
    now = time.monotonic()
    with _capture_lock:
        enabled, max_per_hour = capture_for_camera(camera_id)
        if not enabled or db.review_captures_last_hour(camera_id) >= max_per_hour:
            return
        if sum(db.review_counts().values()) >= REVIEW_MAX_SAMPLES:
            return
        for i, score in enumerate(scores):
            if score is None or i >= len(people) or i >= len(track_ids):
                continue
            near = abs(score - PHONE_CLASSIFIER_THRESHOLD) <= .15
            if not near and score >= PHONE_CLASSIFIER_THRESHOLD:
                continue
            interval = 30 if near else 120
            key = (camera_id, track_ids[i], interval)
            if now - _last_capture.get(key, 0) < interval:
                continue
            # Limit normal negatives: collect rare low scores for manual
            # review, not every person in view.
            if not near and (now - _last_capture.get((camera_id, "low"), 0)
                                 < 120):
                continue
            if not near:
                _last_capture[(camera_id, "low")] = now
            _last_capture[key] = now
            save_image(camera_id, frame, "uncertain" if near else
                       "low-score", score, people[i])
            break  # at most one snapshot per analysis
