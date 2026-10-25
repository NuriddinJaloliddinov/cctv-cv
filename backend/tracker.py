"""Per-camera person tracking with face-anchored, conservative visual recovery.

Only a face match establishes a name. Short-lived appearance matching can
reconnect a recently lost *face-identified* track in the same part of the
image; it is not a biometric identifier or a cross-camera ReID model.

Association compares a detection with a constant-velocity prediction of the
track's box, so a person missed for one frame keeps their track id (and
therefore their phone-use timer) instead of being re-created.
"""
from dataclasses import dataclass

import cv2
import numpy as np


def _iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    area = max(0, x2 - x1) * max(0, y2 - y1)
    if not area:
        return 0.0
    aa = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    bb = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    return area / (aa + bb - area)


def _center(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _distance(a, b):
    ax, ay = _center(a)
    bx, by = _center(b)
    return float(np.hypot(ax - bx, ay - by))


def _appearance(frame, box):
    """Cheap, illumination-tolerant upper/lower-body colour descriptor.

    The central region avoids most background; omit the head (which changes
    when a person turns). Return None for tiny or invalid crops rather than
    trusting noisy colours. These histograms are *not* learned Body ReID.
    """
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = box[:4]
    x1, x2 = max(0, x1), min(width, x2)
    y1, y2 = max(0, y1), min(height, y2)
    w, h = x2 - x1, y2 - y1
    if w < 24 or h < 48:
        return None
    left, right = x1 + int(w * .18), x2 - int(w * .18)
    top, middle, bottom = y1 + int(h * .22), y1 + int(h * .53), y1 + int(h * .90)
    if right <= left or middle <= top or bottom <= middle:
        return None
    parts = []
    for low, high in ((top, middle), (middle, bottom)):
        hsv = cv2.cvtColor(frame[low:high, left:right], cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
        hist = hist.flatten()
        total = hist.sum()
        if total <= 0:
            return None
        parts.append(hist / total)
    return np.concatenate(parts)


def _similarity(a, b):
    if a is None or b is None:
        return None
    # Histogram intersection is in [0, 1].
    return float(np.minimum(a, b).sum() / 2)


@dataclass
class Track:
    id: int
    box: tuple
    appearance: np.ndarray | None
    seen_at: float
    name: str | None = None
    face_score: float = 0.0
    face_at: float = 0.0
    source: str = "track"     # face, track, or visual
    face_confirmed: bool = False
    velocity: tuple = (0.0, 0.0)      # box centre, pixels per second

    def label(self):
        if self.name is None:
            return None
        return self.name, self.face_score, self.source

    def predict(self, now, horizon):
        """Box extrapolated from the last observation, capped at ``horizon``.

        The cap keeps a stale track from drifting across the frame after a
        long gap; without motion the prediction is simply the last box.
        """
        dt = min(max(0.0, now - self.seen_at), max(0.0, horizon))
        vx, vy = self.velocity
        dx, dy = vx * dt, vy * dt
        x1, y1, x2, y2 = self.box[:4]
        return (x1 + dx, y1 + dy, x2 + dx, y2 + dy)

    def moved_to(self, box, now, max_dt):
        """Record an observation and update the smoothed velocity."""
        dt = now - self.seen_at
        if dt > 1e-3 and dt <= max_dt + 1e-3:
            dx = ((box[0] + box[2]) - (self.box[0] + self.box[2])) / 2.0
            dy = ((box[1] + box[3]) - (self.box[1] + self.box[3])) / 2.0
            vx, vy = dx / dt, dy / dt
            if self.velocity == (0.0, 0.0):
                self.velocity = (vx, vy)       # first real motion sample
            else:
                self.velocity = (0.7 * self.velocity[0] + 0.3 * vx,
                                 0.7 * self.velocity[1] + 0.3 * vy)
        elif dt > max_dt + 1e-3:
            self.velocity = (0.0, 0.0)         # too long a gap to extrapolate
        self.box = box
        self.seen_at = now


class PersonTracker:
    """Assign detections one-to-one; never infer an identity from clothing alone.

    Tracks and appearance descriptors stay in memory only for this pipeline.
    ``update`` is called from the inference thread, so no internal lock is
    needed. Face matches have precedence over every visual association.
    """

    def __init__(self, max_gap=2.0, identity_ttl=30.0,
                 visual_threshold=0.88, visual_margin=0.08,
                 continuity_gap=0.75):
        self.max_gap = max(0, max_gap)
        self.identity_ttl = max(0, identity_ttl)
        self.visual_threshold = visual_threshold
        self.visual_margin = visual_margin
        self.continuity_gap = max(0, continuity_gap)
        self.tracks: dict[int, Track] = {}
        self.next_id = 1

    def reset(self):
        self.tracks.clear()

    def _new(self, box, appearance, now):
        track = Track(self.next_id, box, appearance, now)
        self.tracks[track.id] = track
        self.next_id += 1
        return track

    def update(self, frame, boxes, faces, now, with_ids=False):
        """Return a label per detection, in detection order.

        ``faces`` is a list of face matches or None when the face pass was
        skipped. A failed face match is not proof of a different identity.

        With ``with_ids=True`` returns ``[(track_id, label), ...]`` so callers
        can keep stable per-person state (e.g. phone-use timers).
        """
        if faces is not None and len(faces) != len(boxes):
            raise ValueError("face results and boxes must have the same length")

        for tid, track in list(self.tracks.items()):
            if now - track.seen_at > self.max_gap:
                del self.tracks[tid]
            elif track.name and now - track.face_at > self.identity_ttl:
                track.name, track.face_score = None, 0.0
                track.face_confirmed = False

        descriptors = [_appearance(frame, box) for box in boxes]
        assigned: dict[int, Track] = {}
        used_tracks: set[int] = set()

        # Phase 1: overlap or very close movement. Appearance reduces label
        # swaps when people cross; reject ambiguous pairs instead of guessing.
        candidates = []
        for index, (box, descriptor) in enumerate(zip(boxes, descriptors)):
            for track in self.tracks.values():
                gap = now - track.seen_at
                if gap < 0 or gap > self.continuity_gap:
                    continue
                predicted = track.predict(now, self.continuity_gap)
                overlap = _iou(box, predicted)
                distance = _distance(box, predicted)
                size = max(track.box[2] - track.box[0], track.box[3] - track.box[1], 1)
                similarity = _similarity(descriptor, track.appearance)
                if overlap < 0.15 and not (gap <= 0.8 and distance < size * 0.35):
                    continue
                if similarity is not None and similarity < 0.42:
                    continue
                score = 0.7 * overlap + 0.3 * (similarity if similarity is not None else 0.5)
                candidates.append((score, index, track.id))

        candidates.sort(reverse=True)
        for score, index, tid in candidates:
            if index in assigned or tid in used_tracks:
                continue
            # If two face-anchored people look equally plausible for this box,
            # suppress the label rather than arbitrarily choosing one.
            alternatives = [s for s, i, t in candidates
                            if i == index and t != tid and t not in used_tracks]
            if (self.tracks[tid].name and alternatives
                    and score - max(alternatives) < 0.06):
                continue
            assigned[index] = self.tracks[tid]
            used_tracks.add(tid)

        # Phase 2: a lost face-identified track can reattach after a short
        # gap when both appearance and position agree, and it is clearly the
        # only candidate. Never search an enrolled person's clothing gallery.
        for index, (box, descriptor) in enumerate(zip(boxes, descriptors)):
            if index in assigned or descriptor is None:
                continue
            options = []
            for track in self.tracks.values():
                if track.id in used_tracks or not track.name or not track.face_confirmed:
                    continue
                gap = now - track.seen_at
                if gap <= 0 or gap > self.max_gap:
                    continue
                size = max(track.box[2] - track.box[0], track.box[3] - track.box[1], 1)
                predicted = track.predict(now, self.continuity_gap)
                if _distance(box, predicted) > size * (0.4 + gap * 0.3):
                    continue
                sim = _similarity(descriptor, track.appearance)
                if sim is not None and sim >= self.visual_threshold:
                    options.append((sim, track, predicted))
            options.sort(key=lambda item: item[0], reverse=True)
            if not options:
                continue
            best_sim, best_track, best_predicted = options[0]
            if len(options) > 1 and best_sim - options[1][0] < self.visual_margin:
                continue
            # Also require this track to prefer this detection, not another
            # nearby person with nearly the same outfit.
            best_gap = now - best_track.seen_at
            rivals = [_similarity(d, best_track.appearance)
                      for j, d in enumerate(descriptors) if j != index and j not in assigned
                      and d is not None and _distance(boxes[j], best_predicted)
                      <= max(best_track.box[2] - best_track.box[0],
                             best_track.box[3] - best_track.box[1], 1) * (0.4 + best_gap * 0.3)]
            if rivals and best_sim - max(rivals) < self.visual_margin:
                continue
            assigned[index] = best_track
            used_tracks.add(best_track.id)
            best_track.source = "visual"

        for index, (box, descriptor) in enumerate(zip(boxes, descriptors)):
            track = assigned.get(index)
            if track is None:
                track = self._new(box, descriptor, now)
                assigned[index] = track
            else:
                track.moved_to(box, now, self.continuity_gap)
                if descriptor is not None:
                    # Slow smoothing absorbs small illumination/crop changes.
                    track.appearance = (descriptor if track.appearance is None else
                                        0.7 * track.appearance + 0.3 * descriptor)
                if track.source != "visual":
                    track.source = "track"

            match = faces[index] if faces is not None else None
            if match is not None:
                track.name, track.face_score = match[:2]
                track.face_at = now
                track.face_confirmed = True
                track.source = "face"

        if with_ids:
            return [(assigned[i].id, assigned[i].label())
                    for i in range(len(boxes))]
        return [assigned[i].label() for i in range(len(boxes))]
