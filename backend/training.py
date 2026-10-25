"""Offline candidate training; never changes a running classifier.

Person crops are split by camera and UTC day to reduce near-duplicate leakage.
Reported precision/recall are IMAGE-level held-out checks, not event-level
claims. Deployment requires an explicit API call and a camera restart.
"""
import json
import os
import random
import subprocess
import sys
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from . import db
from .config import PHONE_CLASSIFIER_MODEL, PHONE_CLASSIFIER_THRESHOLD
from .phone_classifier import PhoneClassifier, uuid_from_id


ROOT = Path("data/phone_candidates")
STATUS = ROOT / "status.json"
ACTIVE = Path("data/phone_use_active.json")
MIN_PER_CLASS = 20
_start_lock = threading.Lock()


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)


def status():
    try:
        info = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {"state": "idle"}
    if info.get("state") == "running" and info.get("started_at"):
        try:
            started = datetime.fromisoformat(info["started_at"])
            if (datetime.now(timezone.utc) - started).total_seconds() > 7200:
                info = {"state": "failed", "error":
                        "Training exceeded two hours or the worker exited. "
                        "See data/phone_candidates/worker.log."}
        except ValueError:
            pass
    try:
        info["active_id"] = json.loads(ACTIVE.read_text(encoding="utf-8"))["id"]
    except (OSError, ValueError, KeyError):
        info["active_id"] = None
    return info


def labeled_rows():
    rows = db.list_review_samples(limit=100000)
    return [r for r in rows if r["label"] in ("calling", "looking", "no_use")
            and Path(r["image_path"]).is_file()]


def split_rows(rows):
    """Hold out whole camera-day groups; reject insufficient/unsplittable data."""
    counts = Counter(r["label"] != "no_use" for r in rows)
    if min(counts.get(True, 0), counts.get(False, 0)) < MIN_PER_CLASS:
        raise ValueError(f"Label at least {MIN_PER_CLASS} phone-use and "
                         f"{MIN_PER_CLASS} no-use images before training")
    groups = defaultdict(list)
    for row in rows:
        groups[(row["camera_id"], row["created_at"][:10])].append(row)
    keys = list(groups)
    if len(keys) < 2:
        raise ValueError("Collect labels on at least two camera-days; "
                         "near-duplicate frames cannot validate a new model")
    rng = random.Random(42)
    for _ in range(500):
        rng.shuffle(keys)
        selected = set(keys[:max(1, round(len(keys) * .25))])
        valid = [r for k in selected for r in groups[k]]
        train = [r for k in keys if k not in selected for r in groups[k]]
        cv = Counter(r["label"] != "no_use" for r in valid)
        ct = Counter(r["label"] != "no_use" for r in train)
        if min(cv.get(True, 0), cv.get(False, 0)) >= 5 and min(
                ct.get(True, 0), ct.get(False, 0)) >= 10:
            return train, valid
    raise ValueError("Need at least five positives and negatives in a "
                     "separate camera-day validation group")


def _arrays(rows):
    images, labels = [], []
    for row in rows:
        image = cv2.imread(row["image_path"])
        if image is None:
            raise ValueError(f"Unreadable review image: {row['id']}")
        if row.get("person_box"):
            x1, y1, x2, y2 = row["person_box"]
            if (x1 < 0 or y1 < 0 or x2 > image.shape[1] or
                    y2 > image.shape[0] or x2 - x1 < 32 or y2 - y1 < 32):
                raise ValueError(f"Invalid person box in review image: {row['id']}")
            image = image[y1:y2, x1:x2]
        image = cv2.cvtColor(cv2.resize(image, (224, 224)), cv2.COLOR_BGR2RGB)
        images.append(image.astype(np.float32) / 255.)
        labels.append(float(row["label"] != "no_use"))
    return np.stack(images), np.array(labels, dtype=np.float32)


def _metrics(scores, labels):
    predicted = np.array(scores) >= PHONE_CLASSIFIER_THRESHOLD
    positive = labels >= .5
    tp = int(np.sum(predicted & positive))
    fp = int(np.sum(predicted & ~positive))
    fn = int(np.sum(~predicted & positive))
    return {"recall": round(tp / (tp + fn), 3),
            "precision": round(tp / (tp + fp), 3) if tp + fp else 0.,
            "tp": tp, "fp": fp, "fn": fn}


def _base_scores(images):
    base = PhoneClassifier(PHONE_CLASSIFIER_MODEL)
    scores = []
    with base._lock:
        for image in images:
            base._interpreter.set_tensor(base._input, image[None])
            base._interpreter.invoke()
            scores.append(float(base._interpreter.get_tensor(base._output)[0, 0]))
    return scores


def train():
    import tensorflow as tf

    train_rows, valid_rows = split_rows(labeled_rows())
    x_train, y_train = _arrays(train_rows)
    x_valid, y_valid = _arrays(valid_rows)
    base = _metrics(_base_scores(x_valid), y_valid)
    tf.keras.utils.set_random_seed(42)
    # The published .h5 is not loadable in Keras 3. Use a fresh ImageNet
    # MobileNetV2 feature extractor and train a binary head on reviewed crops.
    backbone = tf.keras.applications.MobileNetV2(
        input_shape=(224, 224, 3), include_top=False, weights="imagenet")
    backbone.trainable = False
    inp = tf.keras.Input((224, 224, 3))
    x = tf.keras.layers.Rescaling(2., offset=-1.)(inp)
    x = backbone(x, training=False)
    x = tf.keras.layers.GlobalAveragePooling2D()(x)
    x = tf.keras.layers.Dropout(.25)(x)
    out = tf.keras.layers.Dense(1, activation="sigmoid")(x)
    model = tf.keras.Model(inp, out)
    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3),
                  loss="binary_crossentropy")
    dataset = tf.data.Dataset.from_tensor_slices((x_train, y_train))
    dataset = dataset.shuffle(len(x_train), seed=42).batch(16)
    model.fit(dataset, epochs=12, verbose=0,
              callbacks=[tf.keras.callbacks.EarlyStopping(
                  monitor="loss", patience=3, restore_best_weights=True)])
    candidate = _metrics(model.predict(x_valid, verbose=0).ravel(), y_valid)
    model_id = uuid.uuid4().hex
    path = ROOT / f"{model_id}.tflite"
    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    data = converter.convert()
    path.write_bytes(data)
    try:
        # Ensure the exported graph really works with the live runtime.
        PhoneClassifier(path).predict(np.zeros((224, 224, 3), np.uint8),
                                      [(0, 0, 224, 224)])
    except Exception:
        path.unlink(missing_ok=True)
        raise
    eligible = (candidate["recall"] > base["recall"] and
                candidate["precision"] >= max(.5, base["precision"] - .05))
    return {"state": "complete", "id": model_id, "train": len(train_rows),
            "held_out": len(valid_rows), "baseline": base,
            "candidate": candidate, "eligible": eligible,
            "note": "Image-level camera-day holdout only; test event recall and "
                    "false alerts on live clips before deployment."}


def start():
    with _start_lock:
        if status()["state"] == "running":
            raise ValueError("Training is already running")
        split_rows(labeled_rows())  # fail early, before spawning a worker
        if not Path(PHONE_CLASSIFIER_MODEL).is_file():
            raise ValueError("The original TFLite classifier is missing")
        ROOT.mkdir(parents=True, exist_ok=True)
        _write_json(STATUS, {"state": "running",
                             "started_at": datetime.now(timezone.utc).isoformat()})
        try:
            with (ROOT / "worker.log").open("a") as log:
                subprocess.Popen([sys.executable, "-m", "backend.training", "worker"],
                                 cwd=Path(__file__).resolve().parent.parent,
                                 stdout=log, stderr=subprocess.STDOUT)
        except OSError:
            _write_json(STATUS, {"state": "failed", "error": "Could not start worker"})
            raise
        return status()


def deploy(model_id):
    uuid_from_id(model_id)
    info = status()
    if (info.get("state") != "complete" or info.get("id") != model_id
            or not info.get("eligible")
            or not (ROOT / f"{model_id}.tflite").is_file()):
        raise ValueError("Candidate is not validated or did not improve recall")
    _write_json(ACTIVE, {"id": model_id})
    return status()


def rollback():
    ACTIVE.unlink(missing_ok=True)
    return status()


if __name__ == "__main__" and sys.argv[1:] == ["worker"]:
    try:
        _write_json(STATUS, train())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _write_json(STATUS, {"state": "failed", "error": str(exc)})
