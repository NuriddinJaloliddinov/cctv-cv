"""YOLOv8 person detector (ONNX Runtime)."""
import threading

import cv2
import numpy as np
import onnxruntime as ort

from .capabilities import resolve_device

INPUT_SIZE = 640
PERSON_CLASS = 0     # COCO class 0 = person


class Detector:
    def __init__(self, model_path: str, device: str = "auto",
                 conf: float = 0.4, iou: float = 0.45):
        provider = resolve_device(device)
        self.conf, self.iou = conf, iou

        providers = [provider]
        if provider != "CPUExecutionProvider":
            providers.append("CPUExecutionProvider")

        self.session = ort.InferenceSession(model_path, providers=providers)
        self.input_name = self.session.get_inputs()[0].name

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int, float]]:
        oh, ow = frame.shape[:2]
        blob, scale, (px, py) = self._preprocess(frame)
        out = self.session.run(None, {self.input_name: blob})[0]
        return self._postprocess(out, ow, oh, scale, px, py)

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

    def _postprocess(self, out, ow, oh, scale, px, py):
        pred = out[0].T                       # (N, 84) for yolov8n
        scores = pred[:, 4 + PERSON_CLASS]
        keep = scores >= self.conf
        if not keep.any():
            return []
        pred, scores = pred[keep], scores[keep]

        cx, cy, w, h = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
        x1 = (cx - w / 2 - px) / scale
        y1 = (cy - h / 2 - py) / scale
        bw, bh = w / scale, h / scale
        boxes = np.stack([x1, y1, bw, bh], axis=1)

        idxs = cv2.dnn.NMSBoxes(
            boxes.tolist(), scores.tolist(), self.conf, self.iou
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


def get_detector(model_path: str, device: str, conf: float) -> Detector:
    """Return a cached Detector so N cameras on the same device share one session."""
    key = (model_path, device, conf)
    with _detectors_lock:
        d = _detectors.get(key)
        if d is None:
            d = Detector(model_path, device, conf)
            _detectors[key] = d
        return d