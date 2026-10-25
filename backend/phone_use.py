"""Phone-use detection from person and cell-phone detections.

This module deliberately does **not** flag every detected phone. A phone is
only counted as *active use* when one of these geometric conditions holds:

* **calling** - the phone is beside the head, at ear level (near an ear),
  rather than centred in front of the face.
* **looking** - the phone is below the face, in front of the body (including
  held low over the lap or waist), *and* the face landmarks show the head
  pitched down toward it.

A phone merely held in a hand at the side / waist is therefore ignored.

Limitations: the phone detector is YOLOv8's COCO ``cell phone`` class, which
is tuned for close-up objects. ``Detector.detect_phone_crops`` therefore
re-scans each person's head/upper body so a small phone becomes large enough
to detect; the size/position checks here then reject implausible boxes. The
pitch/ear geometry and temporal confirmation in ``pipeline.py`` reduce false
positives further, but this is a heuristic, not a dedicated action-recognition
model.
"""
from dataclasses import dataclass

import numpy as np

from .config import PHONE_LOOK_PITCH_RATIO

# A real phone is small compared with the person holding it. Rejecting boxes
# larger than this removes the common "whole handbag / car window" false
# positive without affecting phones held close to the camera.
PHONE_MAX_WIDTH_RATIO = 0.50
PHONE_MAX_HEIGHT_RATIO = 0.40
# A person box narrows in profile while a phone held in front keeps its
# size, so the width cap also gets an allowance from the person's height.
PHONE_MAX_WIDTH_HEIGHT_RATIO = 0.22


@dataclass
class PhoneUse:
    kind: str                       # "calling" | "looking"
    score: float
    phone_box: tuple
    face: tuple | None = None


def _center(box):
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union else 0.0


def _point_box_distance(px, py, box):
    x1, y1, x2, y2 = box[:4]
    dx = max(x1 - px, 0, px - x2)
    dy = max(y1 - py, 0, py - y2)
    return float(np.hypot(dx, dy))


def _plausible_for_person(person, phone) -> bool:
    """True when the phone box could belong to this person."""
    px1, py1, px2, py2 = person[:4]
    pw, ph = max(1, px2 - px1), max(1, py2 - py1)
    x1, y1, x2, y2 = phone[:4]
    w, h = x2 - x1, y2 - y1
    if w <= 4 or h <= 4:
        return False
    max_w = max(PHONE_MAX_WIDTH_RATIO * pw, PHONE_MAX_WIDTH_HEIGHT_RATIO * ph)
    if w > max_w or h > PHONE_MAX_HEIGHT_RATIO * ph:
        return False
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    if px1 <= cx <= px2 and py1 <= cy <= py2:
        return True
    # Allow a phone just outside the box (arm outstretched, loose box).
    return _point_box_distance(cx, cy, person) <= 0.10 * ph


def filter_phones(people, phones):
    """Keep one plausible box per phone, preferring the strongest detection.

    The full-frame and per-person crop passes see the same phone at different
    sizes. De-duplicating here keeps the tighter, higher-confidence box and
    discards implausible full-frame boxes.
    """
    kept = []
    for phone in sorted(phones or [], key=lambda p: float(p[4]), reverse=True):
        if not any(_plausible_for_person(person, phone) for person in people):
            continue
        if any(_iou(phone, other) >= 0.45 for other in kept):
            continue
        kept.append(tuple(phone))
    return kept


def _associate(people, phones):
    """Assign each phone to at most one person; return phones per person."""
    assigned = [[] for _ in people]
    for phone in phones:
        pcx, pcy = _center(phone)
        best_score = None
        best_index = None
        for index, person in enumerate(people):
            if not _plausible_for_person(person, phone):
                continue
            x1, y1, x2, y2 = person[:4]
            w, h = max(1, x2 - x1), max(1, y2 - y1)
            inside = x1 <= pcx <= x2 and y1 <= pcy <= y2
            if inside:
                # Prefer the tightest person box when boxes overlap.
                score = (w * h) / 1_000_000.0
            else:
                distance = _point_box_distance(pcx, pcy, person)
                score = 0.5 + distance / h
            if best_score is None or score < best_score:
                best_score, best_index = score, index
        if best_index is not None:
            assigned[best_index].append(phone)
    return assigned


def head_pitch_ratio(face_row) -> float | None:
    """Landmark-based pitch proxy.

    YuNet returns ``[x, y, w, h, right_eye, left_eye, nose, right_mouth,
    left_mouth, score]``. The nose-tip position between the eye line and the
    mouth line is roughly 0.45-0.55 for an upright face and rises as the head
    tilts down. ``None`` means the landmarks were unusable.
    """
    try:
        re_y = float(face_row[5])
        le_y = float(face_row[7])
        nose_y = float(face_row[9])
        rm_y = float(face_row[11])
        lm_y = float(face_row[13])
    except (IndexError, TypeError, ValueError):
        return None
    eye_y = (re_y + le_y) / 2.0
    mouth_y = (rm_y + lm_y) / 2.0
    span = mouth_y - eye_y
    if span <= 1e-3:
        return None
    return (nose_y - eye_y) / span


def head_yaw_ratio(face_row) -> float | None:
    """Nose offset from the eye midpoint, normalised by face width."""
    try:
        eye_x = (float(face_row[4]) + float(face_row[6])) / 2.0
        nose_x = float(face_row[8])
        face_w = float(face_row[2])
    except (IndexError, TypeError, ValueError):
        return None
    if face_w <= 1e-3:
        return None
    return (nose_x - eye_x) / face_w


def _face_parts(face_row):
    x, y, w, h = (float(face_row[i]) for i in range(4))
    return x, y, w, h


def _looks_at_phone(person_box, phone_box, face_row,
                    pitch_ratio: float) -> bool:
    """Phone below the face + head pitched down + phone in front of body."""
    if face_row is None:
        return False
    x1, y1, x2, y2 = person_box[:4]
    h_person = max(1.0, float(y2 - y1))
    fx, fy, fw, fh = _face_parts(face_row)
    fcx, fcy = fx + fw / 2.0, fy + fh / 2.0
    pcx, pcy = _center(phone_box)

    # The phone must sit below the face but still on the person: a phone at
    # the waist or over the lap counts, one hanging at the side does not.
    if pcy < fy + 0.60 * fh:
        return False
    if pcy > y1 + 0.95 * h_person:
        return False
    # And in front of the person, not off to the side behind them.
    if abs(pcx - fcx) > 2.0 * fw:
        return False
    if not (x1 - 0.10 * (x2 - x1) <= pcx <= x2 + 0.10 * (x2 - x1)):
        return False
    # Phone distance below the face should still look like arms holding it,
    # including a phone resting low over the lap.
    if pcy - fcy > 0.80 * h_person:
        return False

    ratio = head_pitch_ratio(face_row)
    yaw = head_yaw_ratio(face_row)
    if ratio is None or ratio < pitch_ratio:
        return False
    if yaw is not None and abs(yaw) > 0.55:
        return False
    return True


def _near_ear(person_box, phone_box, face_row) -> bool:
    """Phone beside the head at ear level (calling), not centred in front."""
    x1, y1, x2, y2 = person_box[:4]
    w_person, h_person = max(1.0, x2 - x1), max(1.0, y2 - y1)
    pcx, pcy = _center(phone_box)

    if face_row is not None:
        fx, fy, fw, fh = _face_parts(face_row)
        if fw <= 1e-3 or fh <= 1e-3:
            return False
        # Ears sit just outside the face box, roughly at eye level.
        eye_y = (float(face_row[5]) + float(face_row[7])) / 2.0
        ears = ((fx - 0.05 * fw, eye_y), (fx + 1.05 * fw, eye_y))
        reach = max(0.65 * fw, 0.06 * h_person)
        if pcy < fy - 0.30 * fh or pcy > fy + 0.95 * fh:
            return False
        if abs(pcx - (fx + fw / 2.0)) < 0.28 * fw:
            return False                      # held in front of the face
        # Measure to the phone box, not its centre: a phone (plus the hand
        # holding it) can be tall, putting its centre well below the ear.
        return min(_point_box_distance(ex, ey, phone_box)
                   for ex, ey in ears) <= reach

    # Fallback when no face is visible (profile or turned away). A phone at
    # the ear is lateral, but still near the person's centreline; a phone
    # held in a hand at the side/waist is much farther out. When the person
    # box is a head-and-shoulders close-up, the head fills most of the height,
    # so the phone may sit lower than in a full-body box.
    aspect = h_person / w_person
    y_limit = (0.22 if aspect >= 2.2 else 0.58) * h_person
    if pcy < y1 or pcy > y1 + y_limit:
        return False
    offset = abs(pcx - (x1 + x2) / 2.0)
    return 0.05 * w_person <= offset <= 0.32 * w_person


def classify(person_box, phone_box, face_row=None,
             pitch_ratio: float = PHONE_LOOK_PITCH_RATIO) -> str | None:
    """Return ``"calling"``, ``"looking"`` or ``None`` for one phone."""
    if _near_ear(person_box, phone_box, face_row):
        return "calling"
    if _looks_at_phone(person_box, phone_box, face_row, pitch_ratio):
        return "looking"
    return None


def _largest_face(engine, frame, person_box):
    """Run YuNet on the person's head area and return a frame-space row."""
    x1, y1, x2, y2 = person_box[:4]
    h, w = max(1, y2 - y1), max(1, x2 - x1)
    top = max(0, int(y1 - 0.02 * h))
    bottom = min(frame.shape[0], int(y1 + 0.45 * h))
    left = max(0, int(x1 - 0.12 * w))
    right = min(frame.shape[1], int(x2 + 0.12 * w))
    crop = frame[top:bottom, left:right]
    if crop.size == 0:
        return None
    try:
        faces = engine.detect(crop)
    except Exception:
        return None
    if not faces:
        return None
    face = max(faces, key=lambda f: float(f[2]) * float(f[3]))
    if float(face[2]) < 24 or float(face[3]) < 24:
        return None
    row = np.array(face[:15], dtype=np.float32)
    row[0] += left
    row[1] += top
    for i in (4, 6, 8, 10, 12):
        row[i] += left
    for i in (5, 7, 9, 11, 13):
        row[i] += top
    return row


def analyze(frame, people, phones,
            pitch_ratio: float = PHONE_LOOK_PITCH_RATIO) -> list[PhoneUse | None]:
    """Return the strongest phone-use classification per person detection."""
    results: list[PhoneUse | None] = [None] * len(people)
    if not people or not phones:
        return results

    phones = filter_phones(people, phones)
    if not phones:
        return results

    per_person = _associate(people, phones)
    if not any(per_person):
        return results

    engine = None
    try:
        from .enroll import get_engine
        engine = get_engine()
    except Exception:
        engine = None

    for index, person in enumerate(people):
        candidates = per_person[index]
        if not candidates:
            continue
        face = (_largest_face(engine, frame, person)
                if engine is not None else None)
        best = None
        for phone in candidates:
            kind = classify(person, phone, face, pitch_ratio)
            if kind is None:
                continue
            item = PhoneUse(kind, float(phone[4]), tuple(phone[:4]),
                            tuple(face[:4]) if face is not None else None)
            # A call is an unambiguous signal; otherwise keep the best score.
            if best is None:
                best = item
            elif kind == "calling" and best.kind != "calling":
                best = item
            elif kind == best.kind and item.score > best.score:
                best = item
        results[index] = best
    return results
