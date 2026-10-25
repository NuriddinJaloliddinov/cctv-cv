"""YOLOv8 person + phone detector (ONNX Runtime)."""
import threading

import cv2
import numpy as np
import onnxruntime as ort

from .capabilities import resolve_device
from .config import (PHONE_CONF_THRESHOLD, PHONE_CROP_CONF_THRESHOLD,
                     PHONE_TILE_CONF_THRESHOLD, PHONE_TILE_GRID,
                     PHONE_TILE_MAX, PHONE_TILE_OVERLAP)

INPUT_SIZE = 640
PERSON_CLASS = 0     # COCO class 0 = person
PHONE_CLASS = 67     # COCO class 67 = cell phone

# Person-relative crop used for the phone re-scan. Phones at ear level sit in
# the upper part of the body, and a phone held at the waist or over the lap
# sits near the bottom of the person box, so the crop keeps nearly all of it
# while still making the phone much larger than in the full frame.
PHONE_CROP_BOTTOM = 0.95
PHONE_CROP_TOP_PAD = 0.05
PHONE_CROP_SIDE_PAD = 0.15


class Detector:
    def __init__(self, model_path: str, device: str = "auto",
                 conf: float = 0.4, phone_conf: float = PHONE_CONF_THRESHOLD,
                 iou: float = 0.45,
                 phone_crop_conf: float = PHONE_CROP_CONF_THRESHOLD,
                 phone_crop_max_people: int = 4,
                 phone_tile_grid: int = PHONE_TILE_GRID,
                 phone_tile_max: int = PHONE_TILE_MAX,
                 phone_tile_overlap: float = PHONE_TILE_OVERLAP,
                 phone_tile_conf: float = PHONE_TILE_CONF_THRESHOLD):
        provider = resolve_device(device)
        self.conf, self.iou = conf, iou
        self.phone_conf = phone_conf
        self.phone_crop_conf = phone_crop_conf
        self.phone_crop_max_people = max(0, int(phone_crop_max_people))
        self.phone_tile_grid = max(0, int(phone_tile_grid))
        self.phone_tile_max = max(0, int(phone_tile_max))
        self.phone_tile_overlap = min(0.45, max(0.0, float(phone_tile_overlap)))
        self.phone_tile_conf = phone_tile_conf

        providers = [provider]
        if provider != "CPUExecutionProvider":
            providers.append("CPUExecutionProvider")

        self.session = ort.InferenceSession(model_path, providers=providers)
        self.input_name = self.session.get_inputs()[0].name

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int, float]]:
        return self._detect_class(frame, PERSON_CLASS, self.conf)

    def detect_objects(self, frame: np.ndarray):
        """Return (people, phones) from a single inference pass."""
        oh, ow = frame.shape[:2]
        blob, scale, (px, py) = self._preprocess(frame)
        out = self.session.run(None, {self.input_name: blob})[0]
        people = self._postprocess(
            out, ow, oh, scale, px, py, PERSON_CLASS, self.conf)
        phones = self._postprocess(
            out, ow, oh, scale, px, py, PHONE_CLASS, self.phone_conf)
        return people, phones

    def detect_phone_crops(self, frame: np.ndarray, people) -> list:
        """Re-scan each person's head/torso for a phone.

        YOLOv8n is trained on close-up phone photos and routinely misses the
        small phones seen at CCTV distance. Cropping each person before the
        pass makes the phone several times larger, which is the difference
        between detecting a call and missing it entirely.
        """
        if not people or self.phone_crop_max_people <= 0:
            return []

        h, w = frame.shape[:2]
        # Largest people first: their phones are the most reliable detections.
        ordered = sorted(
            people, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]),
            reverse=True)[:self.phone_crop_max_people]

        found = []
        for person in ordered:
            x1, y1, x2, y2 = (int(v) for v in person[:4])
            pw, ph = max(1, x2 - x1), max(1, y2 - y1)
            left = max(0, int(x1 - PHONE_CROP_SIDE_PAD * pw))
            right = min(w, int(x2 + PHONE_CROP_SIDE_PAD * pw))
            top = max(0, int(y1 - PHONE_CROP_TOP_PAD * ph))
            bottom = min(h, int(y1 + PHONE_CROP_BOTTOM * ph))
            crop = frame[top:bottom, left:right]
            if crop.shape[0] < 32 or crop.shape[1] < 32:
                continue
            for bx1, by1, bx2, by2, score in self._detect_class(
                    crop, PHONE_CLASS, self.phone_crop_conf):
                found.append((bx1 + left, by1 + top,
                              bx2 + left, by2 + top, float(score)))
        return found

    def detect_phone_tiles(self, frame: np.ndarray, people) -> list:
        """Scan person-dense tiles of the frame for phones.

        ``detect_phone_crops`` zooms on each person, which misses a phone held
        out to the side and anyone crowded out of the crop budget. Splitting the
        frame instead keeps those people large enough to detect, at a cost
        bounded by ``phone_tile_max`` extra passes.
        """
        if not people or self.phone_tile_grid <= 0 or self.phone_tile_max <= 0:
            return []

        h, w = frame.shape[:2]
        cell_w = w / self.phone_tile_grid
        cell_h = h / self.phone_tile_grid
        if cell_w < 48 or cell_h < 48:
            return []
        pad_x = int(cell_w * self.phone_tile_overlap)
        pad_y = int(cell_h * self.phone_tile_overlap)

        cells = []
        for gy in range(self.phone_tile_grid):
            for gx in range(self.phone_tile_grid):
                left, top = gx * cell_w, gy * cell_h
                inside = [p for p in people
                          if left <= (p[0] + p[2]) / 2.0 < left + cell_w
                          and top <= (p[1] + p[3]) / 2.0 < top + cell_h]
                if not inside:
                    continue
                # One person filling most of the cell is already covered by
                # the tighter (more zoomed) per-person crop.
                if len(inside) == 1:
                    person = inside[0]
                    if ((person[2] - person[0]) * (person[3] - person[1])
                            >= 0.6 * cell_w * cell_h):
                        continue
                cells.append((len(inside), int(left), int(top)))

        cells.sort(reverse=True)          # busiest tile first
        found = []
        for _, left, top in cells[:self.phone_tile_max]:
            x1 = max(0, left - pad_x)
            y1 = max(0, top - pad_y)
            x2 = min(w, left + int(cell_w) + pad_x)
            y2 = min(h, top + int(cell_h) + pad_y)
            crop = frame[y1:y2, x1:x2]
            if crop.shape[0] < 48 or crop.shape[1] < 48:
                continue
            for bx1, by1, bx2, by2, score in self._detect_class(
                    crop, PHONE_CLASS, self.phone_tile_conf):
                found.append((bx1 + x1, by1 + y1,
                              bx2 + x1, by2 + y1, float(score)))
        return found

    def _detect_class(self, frame, class_id, conf):
        oh, ow = frame.shape[:2]
        blob, scale, (px, py) = self._preprocess(frame)
        out = self.session.run(None, {self.input_name: blob})[0]
        return self._postprocess(out, ow, oh, scale, px, py, class_id, conf)

    def _preprocess(self, frame):
        h, w = frame.shape[:2]
        scale = min(INPUT_SIZE / w, INPUT_SIZE / h)
        nw, nh = int(w * scale), int(h * scale)
        px, py = (INPUT_SIZE - nw) // 2, (INPUT_SIZE - nh) // 2
        canvas = np.full((INPUT_SIZE, INPUT_SIZE, 3), 114, np.uint8)
        canvas[py:py + nh, px:px + nw] = cv2.resize(frame, (nw, nh))
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        blob = blob.astype(np.float32) / 255.0
        blob = blob.transpose(2, 0, 1)[None]
        return np.ascontiguousarray(blob), scale, (px, py)

    def _postprocess(self, out, ow, oh, scale, px, py, class_id, conf):
        pred = out[0].T                       # (N, 84) for yolov8n
        if pred.shape[1] <= 4 + class_id:
            return []                         # custom model without this class
        scores = pred[:, 4 + class_id]
        keep = scores >= conf
        if not keep.any():
            return []
        pred, scores = pred[keep], scores[keep]

        cx, cy, w, h = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
        x1 = (cx - w / 2 - px) / scale
        y1 = (cy - h / 2 - py) / scale
        bw, bh = w / scale, h / scale
        boxes = np.stack([x1, y1, bw, bh], axis=1)

        idxs = cv2.dnn.NMSBoxes(
            boxes.tolist(), scores.tolist(), conf, self.iou
        )
        return [
            (max(0, int(boxes[i][0])),
             max(0, int(boxes[i][1])),
             min(ow, int(boxes[i][0] + boxes[i][2])),
             min(oh, int(boxes[i][1] + boxes[i][3])),
             float(scores[i]))
            for i in idxs
        ]


_detectors: dict = {}
_detectors_lock = threading.Lock()


def get_detector(model_path: str, device: str, conf: float,
                 phone_conf: float = PHONE_CONF_THRESHOLD,
                 phone_crop_conf: float = PHONE_CROP_CONF_THRESHOLD,
                 phone_crop_max_people: int = 4,
                 phone_tile_grid: int = PHONE_TILE_GRID,
                 phone_tile_max: int = PHONE_TILE_MAX,
                 phone_tile_overlap: float = PHONE_TILE_OVERLAP,
                 phone_tile_conf: float = PHONE_TILE_CONF_THRESHOLD
                 ) -> Detector:
    """Return a cached Detector so N cameras on the same device share one session."""
    key = (model_path, device, conf, phone_conf, phone_crop_conf,
           phone_crop_max_people, phone_tile_grid, phone_tile_max,
           phone_tile_overlap, phone_tile_conf)
    with _detectors_lock:
        d = _detectors.get(key)
        if d is None:
            d = Detector(model_path, device, conf, phone_conf,
                         phone_crop_conf=phone_crop_conf,
                         phone_crop_max_people=phone_crop_max_people,
                         phone_tile_grid=phone_tile_grid,
                         phone_tile_max=phone_tile_max,
                         phone_tile_overlap=phone_tile_overlap,
                         phone_tile_conf=phone_tile_conf)
            _detectors[key] = d
        return d