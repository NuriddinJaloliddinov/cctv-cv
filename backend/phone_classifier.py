"""Optional person-crop phone-use classifier (MobileNetV2 TFLite).

Weights: sreenathsree1578/mobile-phone-usage-detector, MIT. A positive
prediction means *using a phone*, not calling or looking specifically.
"""
import hashlib
import os
import tempfile
import threading
import urllib.request
from pathlib import Path

import cv2
import numpy as np


MODEL_URL = ("https://huggingface.co/sreenathsree1578/"
             "mobile-phone-usage-detector/resolve/"
             "5ecfbb2373809177c8d814b48c0a98af54208c25/"
             "phone_detection_model.tflite")
MODEL_SHA256 = "f6c55888102a6a499f31d338fba2f9535df959c57bea375546d2e63bfda12a17"
MODEL_SIZE = 9520784
INPUT_SIZE = (224, 224)


def download_model(path):
    """Download pinned weights once; never overwrite a user's model file."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} already exists; not overwriting it")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".phone-",
                                         suffix=".download", delete=False) as out:
            temporary = Path(out.name)
            req = urllib.request.Request(MODEL_URL, headers={
                "User-Agent": "cctv-cv-phone-model"})
            with urllib.request.urlopen(req, timeout=90) as response:
                digest = hashlib.sha256()
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MODEL_SIZE:
                        raise ValueError("Phone model download exceeds expected size")
                    digest.update(chunk)
                    out.write(chunk)
        if size != MODEL_SIZE or digest.hexdigest() != MODEL_SHA256:
            raise ValueError("Phone model hash/size mismatch; download not installed")
        os.link(temporary, path)  # exclusive; never replace an existing file
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class PhoneClassifier:
    def __init__(self, path):
        # TensorFlow is optional: the default ONNX-only installation still runs.
        import tensorflow as tf
        self._interpreter = tf.lite.Interpreter(model_path=str(path), num_threads=2)
        self._interpreter.allocate_tensors()
        input_detail = self._interpreter.get_input_details()[0]
        output_detail = self._interpreter.get_output_details()[0]
        if (tuple(input_detail["shape"]) != (1, 224, 224, 3)
                or input_detail["dtype"] != np.float32
                or tuple(output_detail["shape"]) != (1, 1)):
            raise ValueError("Unsupported phone-use TFLite model input/output")
        self._input = input_detail["index"]
        self._output = output_detail["index"]
        self._output_detail = output_detail
        self._lock = threading.Lock()  # interpreter is shared across cameras

    def predict(self, frame, people, max_people=4):
        """Return per-person positive probabilities, None for skipped crops."""
        scores = [None] * len(people)
        height, width = frame.shape[:2]
        ordered = sorted(range(len(people)), key=lambda i: (
            (people[i][2] - people[i][0]) * (people[i][3] - people[i][1])),
            reverse=True)[:max(0, max_people)]
        for i in ordered:
            x1, y1, x2, y2 = (int(v) for v in people[i][:4])
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(width, x2), min(height, y2)
            if x2 - x1 < 32 or y2 - y1 < 32:
                continue
            crop = frame[y1:y2, x1:x2]
            # Model card preprocessing: RGB, direct resize, float32 [0, 1].
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            image = cv2.resize(rgb, INPUT_SIZE).astype(np.float32) / 255.0
            with self._lock:
                self._interpreter.set_tensor(self._input, image[None])
                self._interpreter.invoke()
                score = float(self._interpreter.get_tensor(self._output)[0][0])
            if np.isfinite(score) and 0 <= score <= 1:
                scores[i] = score
        return scores


_classifier = None
_classifier_path = None
_load_lock = threading.Lock()


def active_model_path(default):
    """Use the explicitly deployed, locally trained model when configured."""
    import json
    pointer = Path("data/phone_use_active.json")
    if pointer.is_file():
        try:
            candidate_id = json.loads(pointer.read_text(encoding="utf-8"))["id"]
            uuid_from_id(candidate_id)
            path = Path("data/phone_candidates") / f"{candidate_id}.tflite"
            if path.is_file():
                return str(path)
        except (ValueError, KeyError, OSError):
            pass
    return default


def uuid_from_id(value):
    import uuid
    if uuid.UUID(value).hex != value:
        raise ValueError("Invalid model ID")


def get_classifier(path):
    global _classifier, _classifier_path
    with _load_lock:
        if _classifier is None or _classifier_path != str(path):
            _classifier = PhoneClassifier(path)
            _classifier_path = str(path)
        return _classifier


if __name__ == "__main__":
    from .config import PHONE_CLASSIFIER_MODEL
    download_model(PHONE_CLASSIFIER_MODEL)
    print(f"Installed verified phone-use weights at {PHONE_CLASSIFIER_MODEL}")
