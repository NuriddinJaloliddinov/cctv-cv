"""Per-camera CV pipelines."""
import threading
import time
from .recognizer import identify

import cv2

from .config import CONF_THRESHOLD, MODEL_NAME
from .detector import get_detector
from .source import open_stream


def _draw(frame, boxes, identities):
    for (x1, y1, x2, y2, conf), ident in zip(boxes, identities):
        if ident is None:
            color = (0, 0, 220)        # red — unknown
            label = "Unknown"
        else:
            name, score = ident
            color = (0, 180, 0)        # green — known
            label = f"{name} {score:.2f}"
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame, label, (x1, max(y1 - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return frame


class Pipeline:
    def __init__(self, camera_id, name, url, device, fps, conf=CONF_THRESHOLD):
        self.camera_id, self.name = camera_id, name or camera_id
        self.url, self.device, self.fps, self.conf = url, device, fps, conf
        self.latest_frame = None
        self._stop = threading.Event()
        self._thread = None
        self._detector = None

    def start(self):
        self._detector = get_detector(MODEL_NAME, self.device, self.conf)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        interval = 1.0 / max(self.fps, 1)
        next_t = time.monotonic()
        try:
            for frame in open_stream(self.url):
                if self._stop.is_set():
                    break
                now = time.monotonic()
                if now < next_t:
                    time.sleep(next_t - now)
                next_t = max(now + interval, next_t + interval)
                boxes = self._detector.detect(frame)
                identities = identify(frame, boxes)          # NEW
                self.latest_frame = _draw(frame.copy(), boxes, identities)
        except Exception as e:
            print(f"[pipeline {self.camera_id}] error: {e}")


_pipelines: dict[str, Pipeline] = {}
_lock = threading.Lock()


def start_pipeline(camera_id, name, url, device, fps):
    with _lock:
        if camera_id in _pipelines:
            _pipelines[camera_id].stop()
        p = Pipeline(camera_id, name, url, device, fps)
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
        return [{"camera_id": p.camera_id, "name": p.name,
                 "device": p.device, "fps": p.fps}
                for p in _pipelines.values()]