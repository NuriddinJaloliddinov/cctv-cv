"""Per-camera CV pipelines.

Capture, inference, and display are decoupled:

* ``FrameSource`` keeps draining the camera and exposes only its newest frame.
* An inference thread runs person detection, lightweight tracking, and (less
  often) face recognition on its own cadence.
* A display thread publishes a fresh annotated frame at the requested fps,
  using the most recent boxes. A slow detector or face match can therefore
  never stall or age the live video.
"""
import threading
import time
from collections import deque
from pathlib import Path

import cv2

from .config import (CONF_THRESHOLD, DETECT_INTERVAL, DETECT_MAX_WIDTH,
                     FACE_INTERVAL, MODEL_NAME, PHONE_ALERT_COOLDOWN,
                     PHONE_CONF_THRESHOLD, PHONE_CROP_CONF_THRESHOLD,
                     PHONE_CROP_MAX_PEOPLE, PHONE_LOOK_PITCH_RATIO,
                     PHONE_TILE_CONF_THRESHOLD, PHONE_TILE_GRID,
                     PHONE_TILE_MAX, PHONE_TILE_OVERLAP,
                     PHONE_USE_EPISODE_SECONDS, PHONE_USE_EVIDENCE,
                     PHONE_USE_GRACE_SECONDS, PHONE_USE_INTERVAL,
                      PHONE_USE_MIN_SECONDS, STREAM_MAX_WIDTH, TRACK_MAX_GAP,
                      TRACK_IDENTITY_TTL, TRACK_VISUAL_THRESHOLD,
                      PHONE_CLASSIFIER_MODEL, PHONE_CLASSIFIER_THRESHOLD,
                      PHONE_CLASSIFIER_MAX_PEOPLE)
from .detector import get_detector
from .recognizer import identify
from .source import FrameSource
from .tracker import PersonTracker
from .crossing import CrossingMonitor
from . import alerts
from . import phone_use as phone_use_mod
from . import review
from . import face_review
from .phone_classifier import get_classifier, active_model_path


PHONE_BOX_COLOR = (255, 210, 0)         # cyan-blue: any detected phone
ACTIVE_PHONE_COLOR = (0, 165, 255)      # amber: the phone causing the alert

# Phone use is confirmed from a window of analyses rather than a single
# detection. At the default 0.5 s cadence the window holds ~4 s of history.
PHONE_EVIDENCE_WINDOW = 8
PHONE_MIN_HITS = 3          # positive analyses required in that window
# How far the phone may move relative to the person between analyses before
# it counts as a different (usually false) detection.
PHONE_OFFSET_TOLERANCE = 0.35


def _draw(frame, boxes, identities, phone_use=None, phones=None,
          active_phone_boxes=None, tripwire=None):
    if tripwire is not None and tripwire.line is not None:
        height, width = frame.shape[:2]
        ax, ay, bx, by = tripwire.line
        a, b = (int(ax * width), int(ay * height)), (int(bx * width), int(by * height))
        cv2.line(frame, a, b, (0, 210, 255), 2)
        # Yellow and blue markers show the two sides; editor uses the same
        # colour/direction convention, avoiding ambiguous "left/right" in CCTV.
        dx, dy = bx - ax, by - ay
        length = max((dx * dx + dy * dy) ** .5, .001)
        mx, my = (ax + bx) / 2, (ay + by) / 2
        sides = [(-dy / length, dx / length), (dy / length, -dx / length)]
        points = [(int((mx + nx * .045) * width),
                   int((my + ny * .045) * height)) for nx, ny in sides]
        cv2.circle(frame, points[0], 6, (0, 220, 255), -1)  # yellow
        cv2.circle(frame, points[1], 6, (255, 130, 0), -1)  # blue
        src, dst = (0, 1) if tripwire.direction == "positive_to_negative" else (1, 0)
        cv2.arrowedLine(frame, points[src], points[dst], (0, 255, 0), 2,
                        tipLength=.32)
        cv2.putText(frame, tripwire.role.title(), a,
                    cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 210, 255), 2, cv2.LINE_AA)
    phone_use = list(phone_use or [])
    for index, ((x1, y1, x2, y2, conf), ident) in enumerate(
            zip(boxes, identities)):
        use = phone_use[index] if index < len(phone_use) else None
        if ident is None:
            color = (0, 0, 220)        # red — unknown
            label = "Unknown"
        else:
            name, score, source = ident
            color = ((0, 165, 220) if source == "visual" else (0, 180, 0))
            # Only a fresh face recognition has a biometric match score.
            label = (f"{name} {score:.2f} (face)" if source == "face" else
                     f"{name} (visual)" if source == "visual" else
                     f"{name} (tracked)")
        if use:
            color = (0, 165, 255)      # amber — phone use
            label += (" [on call]" if use == "calling" else
                      " [looking at phone]" if use == "looking" else
                      " [phone use (model)]")
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame, label, (x1, max(y1 - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    # Draw every phone the detector found, so it is obvious what was seen.
    for phone in phones or []:
        px1, py1, px2, py2 = (int(v) for v in phone[:4])
        cv2.rectangle(frame, (px1, py1), (px2, py2), PHONE_BOX_COLOR, 1)
        cv2.putText(frame, "phone", (px1, max(py1 - 5, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, PHONE_BOX_COLOR, 1,
                    cv2.LINE_AA)

    # Then highlight the one that actually triggered the phone-use state.
    for phone in active_phone_boxes or []:
        if not phone:
            continue
        px1, py1, px2, py2 = (int(v) for v in phone[:4])
        cv2.rectangle(frame, (px1, py1), (px2, py2), ACTIVE_PHONE_COLOR, 2)
        cv2.putText(frame, "PHONE IN USE", (px1, max(py2 + 14, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, ACTIVE_PHONE_COLOR, 1,
                    cv2.LINE_AA)
    return frame


def _fit_width(frame, max_width, copy=False):
    """Downscale for streaming; boxes and inference share this coordinate space.

    With ``copy=True`` the caller may draw on the result without touching the
    source frame that another thread may still be reading.
    """
    if max_width <= 0:
        return frame.copy() if copy else frame
    h, w = frame.shape[:2]
    if w <= max_width:
        return frame.copy() if copy else frame
    scale = max_width / w
    return cv2.resize(frame, (max_width, max(1, int(h * scale))),
                      interpolation=cv2.INTER_AREA)


def _scale_box(box, sx, sy):
    """Move one box from the inference frame into the output frame."""
    if box is None:
        return None
    x1, y1, x2, y2 = (float(v) for v in box[:4])
    return (int(round(x1 * sx)), int(round(y1 * sy)),
            int(round(x2 * sx)), int(round(y2 * sy)), *box[4:])


def _inference_to_display(shape, in_shape, boxes, identities, phone_use,
                          phone_boxes, active_phones):
    """Map overlay state from the inference frame to the output frame.

    Inference can run at a higher resolution than the WebRTC stream
    (``DETECT_MAX_WIDTH`` vs ``STREAM_MAX_WIDTH``), so every box is scaled
    into the output frame instead of being drawn in the wrong space.
    """
    if not in_shape:
        return [], [], [], [], []
    if tuple(shape) == tuple(in_shape):
        return boxes, identities, phone_use, phone_boxes, active_phones
    sy = shape[0] / max(1, in_shape[0])
    sx = shape[1] / max(1, in_shape[1])
    if abs(sx - sy) > 0.02:
        return [], [], [], [], []      # camera changed aspect: boxes stale
    return ([_scale_box(b, sx, sy) for b in boxes],
            identities, phone_use,
            [_scale_box(b, sx, sy) for b in phone_boxes],
            [_scale_box(b, sx, sy) for b in active_phones])


def _phone_offset(person_box, phone_box):
    """Phone centre inside the person box, in person-box units."""
    if not phone_box:
        return None
    x1, y1, x2, y2 = (float(v) for v in person_box[:4])
    pw = max(1.0, x2 - x1)
    ph = max(1.0, y2 - y1)
    cx = (float(phone_box[0]) + float(phone_box[2])) / 2.0
    cy = (float(phone_box[1]) + float(phone_box[3])) / 2.0
    return ((cx - x1) / pw, (cy - y1) / ph)


def _offset_jump(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


class Pipeline:
    def __init__(self, camera_id, name, url, device, fps, conf=CONF_THRESHOLD,
                 phone_conf=PHONE_CONF_THRESHOLD,
                 phone_pitch=PHONE_LOOK_PITCH_RATIO, role=None,
                 tripwire=None, tripwire_direction=None,
                 person_detection=True, phone_use_detection=True,
                 count_customer=False):
        self.camera_id, self.name = camera_id, name or camera_id
        self.url, self.device, self.fps, self.conf = url, device, fps, conf
        self.phone_conf, self.phone_pitch = phone_conf, phone_pitch
        self._count_customer = bool(count_customer)
        self._crossing = CrossingMonitor(role, tripwire, tripwire_direction)
        self._detection_flags = (bool(person_detection),
                                 bool(person_detection and phone_use_detection))

        self._source = FrameSource(url)
        self._detector = None
        self._phone_classifier = None
        self._phone_classifier_status = "not installed"
        self._tracker = PersonTracker(
            TRACK_MAX_GAP, TRACK_IDENTITY_TTL, TRACK_VISUAL_THRESHOLD,
            continuity_gap=min(1.5, max(0.75, DETECT_INTERVAL * 1.5)))
        self._stop = threading.Event()
        self._infer_thread = None
        self._display_thread = None

        # Shared overlay state (written by inference, read by display).
        self._box_lock = threading.Lock()
        self._boxes = []
        self._identities = []
        self._phone_use = []
        self._phone_boxes = []
        self._active_phone_boxes = []
        self._box_shape = None
        self._box_at = 0.0
        self._inference_ms = 0.0
        self._inference_count = 0
        self._inference_times = deque(maxlen=200)
        self._face_pass_count = 0

        # Per-track phone-use state machine (inference thread only).
        self._phone_tracks: dict[int, dict] = {}
        self._last_phone_analysis = 0.0

        # Shared output frame (read by WebRTC).
        self._frame_lock = threading.Lock()
        self.latest_frame = None
        self._published_seq = 0
        self._published_at = 0.0
        self._frame_count = 0
        self._publish_fps = 0.0
        self._fps_started = time.monotonic()

    # ---------- lifecycle ----------

    def start(self):
        # Load the model first: a failure here should not leave reader threads
        # running in the background.
        self._detector = get_detector(
            MODEL_NAME, self.device, self.conf, self.phone_conf,
            PHONE_CROP_CONF_THRESHOLD, PHONE_CROP_MAX_PEOPLE,
            PHONE_TILE_GRID, PHONE_TILE_MAX, PHONE_TILE_OVERLAP,
            PHONE_TILE_CONF_THRESHOLD)
        classifier_path = active_model_path(PHONE_CLASSIFIER_MODEL)
        if Path(classifier_path).is_file():
            try:
                self._phone_classifier = get_classifier(classifier_path)
                self._phone_classifier_status = (
                    "trained" if classifier_path != PHONE_CLASSIFIER_MODEL
                    else "active")
            except Exception as e:
                self._phone_classifier_status = f"unavailable: {e}"
                print(f"[phone] classifier unavailable for {self.name}: {e}")
        self._source.start()
        self._infer_thread = threading.Thread(
            target=self._infer_loop, name=f"infer-{self.camera_id}",
            daemon=True)
        self._display_thread = threading.Thread(
            target=self._display_loop, name=f"display-{self.camera_id}",
            daemon=True)
        self._infer_thread.start()
        self._display_thread.start()

    def stop(self):
        self._stop.set()
        self._source.stop()
        for thread in (self._infer_thread, self._display_thread):
            if thread:
                thread.join(timeout=3)

    def set_crossing(self, role, line, direction):
        """Atomically replace the monitor; forget stale crossing observations."""
        self._crossing = CrossingMonitor(role, line, direction)

    def set_count_customer(self, value):
        """Change only the customer tag; keep the current crossing state."""
        self._count_customer = bool(value)

    def set_detection(self, person, phone):
        """Change live inference switches without stopping the video stream."""
        self._detection_flags = (bool(person), bool(person and phone))
        if not person or not phone:
            with self._box_lock:
                if not person:
                    self._boxes = []
                    self._identities = []
                self._phone_boxes = []
                self._phone_use = []
                self._active_phone_boxes = []

    # ---------- consumer API ----------

    def get_latest_frame(self):
        with self._frame_lock:
            return self.latest_frame

    def status(self) -> dict:
        source = self._source.status()
        with self._box_lock:
            inference_ms = self._inference_ms
            inference_count = self._inference_count
            times = list(self._inference_times)
            face_pass_count = self._face_pass_count
            person_count = len(self._boxes)
            phones = len(self._phone_boxes)
            phone_use = sum(1 for kind in self._phone_use if kind)
        _, raw = self._source.latest()
        with self._frame_lock:
            age = (time.monotonic() - self._published_at) * 1000 \
                if self._published_at else None
        return {
            **source,
            "publish_fps": round(self._publish_fps, 1),
            "frames_shown": self._frame_count,
            "inference_ms": round(inference_ms, 1),
            "inference_count": inference_count,
            "face_pass_count": face_pass_count,
            "person_count": person_count,
            "inference_p95_ms": round(sorted(times)[int(.95 * (len(times) - 1))], 1) if times else None,
            "source_width": int(raw.shape[1]) if raw is not None else None,
            "source_height": int(raw.shape[0]) if raw is not None else None,
            "configured_fps": self.fps,
            "device": self.device,
            "display_age_ms": round(age, 1) if age is not None else None,
            "phones": phones,
            "phone_use": phone_use,
            "phone_classifier": self._phone_classifier_status,
            "role": self._crossing.role,
            "count_customer": self._count_customer,
            "person_detection_enabled": self._detection_flags[0],
            "phone_use_detection_enabled": self._detection_flags[1],
        }

    # ---------- inference ----------

    def _infer_loop(self):
        interval = max(DETECT_INTERVAL, 0.0)
        next_t = 0.0
        last_seq = 0
        last_face = 0.0
        last_phone = 0.0
        phone_boxes = []
        shape = None
        last_flags = None

        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_t:
                time.sleep(min(next_t - now, 0.02))
                continue
            next_t = max(next_t + interval, time.monotonic())

            seq, raw = self._source.latest()
            if raw is None or seq == last_seq:
                time.sleep(0.01)
                continue
            last_seq = seq

            flags = self._detection_flags
            if flags != last_flags:
                # Changing phone use alone must not disrupt person identities
                # or an in-progress entrance/exit crossing.
                if last_flags is None or flags[0] != last_flags[0]:
                    self._tracker.reset()
                    monitor = self._crossing
                    self.set_crossing(monitor.role, monitor.line, monitor.direction)
                    last_face = 0.0
                    shape = None
                self._phone_tracks.clear()
                last_phone = 0.0
                phone_boxes = []
                last_flags = flags
            if not flags[0]:
                with self._box_lock:
                    self._boxes = []
                    self._identities = []
                    self._phone_boxes = []
                    self._phone_use = []
                    self._active_phone_boxes = []
                    self._box_shape = None
                    self._box_at = time.monotonic()
                    self._inference_ms = 0.0
                continue

            # Inference keeps more detail than the stream: a 4K frame
            # downscaled for WebRTC would erase the small phones and distant
            # faces the detection passes look for.
            frame = _fit_width(raw, DETECT_MAX_WIDTH)
            if shape != frame.shape[:2]:
                shape = frame.shape[:2]
                self._tracker.reset()
                self._phone_tracks.clear()
                monitor = self._crossing
                self.set_crossing(monitor.role, monitor.line, monitor.direction)
                phone_boxes = []
                last_face = 0.0
                last_phone = 0.0

            started = time.perf_counter()
            detect_objects = getattr(self._detector, "detect_objects", None)
            if callable(detect_objects):
                boxes, phones = detect_objects(frame)
            else:                       # custom/fake detectors in tests
                boxes, phones = self._detector.detect(frame), []
            now = time.monotonic()

            faces = None
            unknown_faces = []
            if boxes and now - last_face >= FACE_INTERVAL:
                if face_review.effective(self.camera_id)["enabled"]:
                    faces, unknown_faces = identify(frame, boxes, with_unknown=True)
                else:
                    faces = identify(frame, boxes)
                with self._box_lock:
                    self._face_pass_count += 1
                last_face = now

            if not boxes:
                # Force a fresh face pass when someone appears again.
                last_face = 0.0

            assigned = self._tracker.update(
                frame, boxes, faces, time.monotonic(), with_ids=True)
            track_ids = [track_id for track_id, _ in assigned]
            identities = [label for _, label in assigned]

            if unknown_faces and self._detection_flags[0]:
                for index, crop, embedding in unknown_faces:
                    if index >= len(track_ids) or identities[index] is not None:
                        continue  # track already has a reliable face name
                    try:
                        face_review.maybe_capture(
                            self.camera_id, track_ids[index], crop, embedding, now)
                    except Exception as exc:
                        print(f"[enrollment] face review failed for {self.name}: {exc}")

            monitor = self._crossing
            try:
                crossings = monitor.update(track_ids, boxes, frame.shape, now)
                for i, track_id in crossings:
                    if not self._detection_flags[0]:
                        break
                    name = identities[i][0] if identities[i] else "Unknown"
                    alerts.report_crossing(
                        camera_id=self.camera_id, camera_name=self.name,
                        track_id=track_id, person_name=name, role=monitor.role,
                        score=boxes[i][4], frame=frame, person_box=boxes[i][:4],
                        line=monitor.line, direction=monitor.direction,
                        count_customer=self._count_customer)
            except Exception as exc:
                print(f"[crossing] analysis failed for {self.name}: {exc}")

            phone_boxes = phone_boxes if boxes else []
            if not flags[1]:
                phone_boxes = []
                self._phone_tracks.clear()
            elif boxes and now - last_phone >= PHONE_USE_INTERVAL:
                # The full-frame pass misses small phones, so re-scan each
                # person in a tighter crop and merge the results.
                merged = list(phones)
                crop_scan = getattr(
                    self._detector, "detect_phone_crops", None)
                if callable(crop_scan):
                    try:
                        merged.extend(crop_scan(frame, boxes))
                    except Exception as e:
                        print(f"[phone] crop scan failed for {self.name}: {e}")
                # Crop passes miss a phone held away from the body, so the
                # frame is also searched in a few person-dense tiles.
                tile_scan = getattr(
                    self._detector, "detect_phone_tiles", None)
                if callable(tile_scan):
                    try:
                        merged.extend(tile_scan(frame, boxes))
                    except Exception as e:
                        print(f"[phone] tile scan failed for {self.name}: {e}")
                phone_boxes = phone_use_mod.filter_phones(boxes, merged)
                try:
                    results = phone_use_mod.analyze(
                        frame, boxes, phone_boxes, self.phone_pitch)
                except Exception as e:
                    print(f"[phone] analysis failed for {self.name}: {e}")
                    results = [None] * len(boxes)
                if self._phone_classifier is not None:
                    try:
                        scores = self._phone_classifier.predict(
                            frame, boxes, PHONE_CLASSIFIER_MAX_PEOPLE)
                        try:
                            if self._detection_flags[1]:
                                review.maybe_capture(
                                    self.camera_id, frame, boxes, scores, track_ids)
                        except Exception as e:
                            print(f"[review] capture failed for {self.name}: {e}")
                        for i, score in enumerate(scores[:len(results)]):
                            if (results[i] is None and score is not None
                                    and score >= PHONE_CLASSIFIER_THRESHOLD):
                                # No phone box or action label can be inferred
                                # from this binary person-crop classifier.
                                results[i] = phone_use_mod.PhoneUse(
                                    "using", score, None)
                    except Exception as e:
                        self._phone_classifier_status = f"unavailable: {e}"
                        self._phone_classifier = None
                        print(f"[phone] classifier failed for {self.name}: {e}")
                last_phone = now
                self._update_phone_states(
                    track_ids, results, boxes, identities, frame, now)
            elif boxes:
                self._prune_phone_states(now, set(track_ids))
            else:
                self._prune_phone_states(now)

            active_phone = ([self._phone_active(tid, now) for tid in track_ids]
                            if self._detection_flags[1] else [None] * len(track_ids))
            active_phone_boxes = [
                self._phone_tracks[tid].get("phone_box")
                if self._detection_flags[1] and self._phone_active(tid, now) else None
                for tid in track_ids]
            elapsed_ms = (time.perf_counter() - started) * 1000
            with self._box_lock:
                current_person, current_phone = self._detection_flags
                self._boxes = boxes if current_person else []
                self._identities = identities if current_person else []
                self._phone_use = active_phone if current_phone else []
                self._phone_boxes = phone_boxes if current_phone else []
                self._active_phone_boxes = active_phone_boxes if current_phone else []
                self._box_shape = shape
                self._box_at = time.monotonic()
                self._inference_ms = elapsed_ms
                self._inference_count += 1
                self._inference_times.append(elapsed_ms)

    # ---------- phone-use state ----------

    @staticmethod
    def _phone_grace() -> float:
        return max(PHONE_USE_GRACE_SECONDS, PHONE_USE_INTERVAL * 1.5)

    @staticmethod
    def _new_phone_state() -> dict:
        return {
            "since": None, "last": 0.0, "kind": None, "score": 0.0,
            "fired": False, "cooldown_until": 0.0, "phone_box": None,
            "offset": None, "window": deque(maxlen=PHONE_EVIDENCE_WINDOW),
        }

    @staticmethod
    def _reset_phone_state(state):
        state.update({"since": None, "kind": None, "score": 0.0,
                      "phone_box": None, "offset": None, "fired": False})
        state["window"].clear()

    @staticmethod
    def _phone_evidence(state) -> float:
        window = state["window"]
        return sum(window) / len(window) if window else 0.0

    def _phone_active(self, track_id, now):
        state = self._phone_tracks.get(track_id)
        if not state or state["since"] is None:
            return None
        if now - state["last"] > self._phone_grace():
            return None
        return state["kind"]

    def _update_phone_states(self, track_ids, results, boxes, identities,
                              frame, now):
        if not self._detection_flags[1]:
            return
        for index, track_id in enumerate(track_ids):
            if not self._detection_flags[1]:
                break
            result = results[index] if index < len(results) else None
            state = self._phone_tracks.get(track_id)
            if state is None:
                state = self._new_phone_state()
                self._phone_tracks[track_id] = state

            if result is None:
                # Keep the episode (and its timer) alive across a short
                # dropout: a small phone is routinely lost for a few
                # analyses while the person keeps using it. A genuine
                # absence still ends the episode.
                if (state["since"] is not None
                        and now - state["last"] > PHONE_USE_EPISODE_SECONDS):
                    self._reset_phone_state(state)
                state["window"].append(0)
                continue

            offset = _phone_offset(boxes[index], result.phone_box)
            if (state["since"] is not None and offset is not None
                    and state["offset"] is not None
                    and state["kind"] == result.kind
                    and _offset_jump(state["offset"], offset)
                    > PHONE_OFFSET_TOLERANCE):
                # Same person, but the phone jumped somewhere unrelated:
                # that is a new detection, not continued evidence.
                self._reset_phone_state(state)

            state["window"].append(1)
            if state["since"] is None:
                state["since"] = now
                state["kind"] = result.kind
                state["score"] = result.score
                # Suppress a re-alert for activity that starts while a
                # previous alert is still cooling down.
                state["fired"] = now < state["cooldown_until"]
            elif result.kind == "calling" and state["kind"] != "calling":
                state["kind"] = "calling"
                state["score"] = max(state["score"], result.score)
            elif result.kind == "looking" and state["kind"] == "using":
                state["kind"] = "looking"
                state["score"] = result.score
            elif result.kind == state["kind"] and result.score > state["score"]:
                state["kind"] = result.kind
                state["score"] = result.score

            state["last"] = now
            if result.phone_box is not None:
                state["phone_box"] = result.phone_box
                state["offset"] = offset

            if (not state["fired"]
                    and now - state["since"] >= PHONE_USE_MIN_SECONDS
                    and now >= state["cooldown_until"]
                    and sum(state["window"]) >= PHONE_MIN_HITS
                    and self._phone_evidence(state) >= PHONE_USE_EVIDENCE):
                if not self._detection_flags[1]:
                    break
                state["fired"] = True
                state["cooldown_until"] = now + PHONE_ALERT_COOLDOWN
                ident = identities[index] if index < len(identities) else None
                name = ident[0] if ident else "Unknown"
                try:
                    alerts.report_phone_use(
                        camera_id=self.camera_id,
                        camera_name=self.name,
                        track_id=track_id,
                        person_name=name,
                        kind=state["kind"],
                        score=state["score"],
                        frame=frame,
                        person_box=boxes[index][:4],
                        phone_box=state["phone_box"],
                    )
                except Exception as e:
                    print(f"[phone] alert failed for {self.name}: {e}")

        self._prune_phone_states(now, set(track_ids))

    def _prune_phone_states(self, now, active_ids=None):
        for track_id, state in list(self._phone_tracks.items()):
            if active_ids and track_id in active_ids:
                continue
            if (now - state["last"] > 60.0
                    and now >= state["cooldown_until"]):
                self._phone_tracks.pop(track_id, None)

    # ---------- display ----------

    def _display_loop(self):
        interval = 1.0 / max(self.fps, 1)
        next_t = time.monotonic()
        last_seq = 0

        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_t:
                # Short naps keep the loop responsive to a newer camera frame.
                time.sleep(min(next_t - now, 0.02))
                continue
            next_t = max(next_t + interval, time.monotonic())

            seq, raw = self._source.latest()
            if raw is None or seq == last_seq:
                continue
            last_seq = seq

            frame = _fit_width(raw, STREAM_MAX_WIDTH, copy=True)
            with self._box_lock:
                recent = time.monotonic() - self._box_at < max(1.0, 3 * DETECT_INTERVAL)
                in_shape = self._box_shape
                boxes = list(self._boxes) if recent else []
                identities = list(self._identities) if recent else []
                phone_use = list(self._phone_use) if recent else []
                phone_boxes = list(self._phone_boxes) if recent else []
                active_phones = list(self._active_phone_boxes) if recent else []

            boxes, identities, phone_use, phone_boxes, active_phones = (
                _inference_to_display(frame.shape[:2], in_shape, boxes,
                                      identities, phone_use, phone_boxes,
                                      active_phones))

            _draw(frame, boxes, identities, phone_use, phone_boxes,
                  active_phones,
                  self._crossing if self._detection_flags[0] else None)

            with self._frame_lock:
                self.latest_frame = frame
                self._published_seq = seq
                self._published_at = time.monotonic()
                self._frame_count += 1
                elapsed = time.monotonic() - self._fps_started
                if elapsed >= 1.0:
                    self._publish_fps = self._frame_count / elapsed
                    self._frame_count = 0
                    self._fps_started = time.monotonic()


_pipelines: dict[str, Pipeline] = {}
_lock = threading.Lock()


def start_pipeline(camera_id, name, url, device, fps,
                   phone_conf=PHONE_CONF_THRESHOLD,
                   phone_pitch=PHONE_LOOK_PITCH_RATIO, role=None,
                   tripwire=None, tripwire_direction=None,
                   person_detection=True, phone_use_detection=True,
                   count_customer=False):
    with _lock:
        old = _pipelines.pop(camera_id, None)
        if old:
            old.stop()
        p = Pipeline(camera_id, name, url, device, fps,
                     phone_conf=phone_conf, phone_pitch=phone_pitch,
                     role=role, tripwire=tripwire,
                     tripwire_direction=tripwire_direction,
                     person_detection=person_detection,
                     phone_use_detection=phone_use_detection,
                     count_customer=count_customer)
        p.start()
        _pipelines[camera_id] = p


def stop_pipeline(camera_id):
    with _lock:
        p = _pipelines.pop(camera_id, None)
    if p:
        p.stop()


def get_pipeline(camera_id):
    return _pipelines.get(camera_id)


def list_pipelines():
    with _lock:
        pipelines = list(_pipelines.values())
    return [{"camera_id": p.camera_id, "name": p.name,
             "device": p.device, "fps": p.fps, **p.status()}
            for p in pipelines]
