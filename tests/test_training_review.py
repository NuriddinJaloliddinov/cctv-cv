"""Review persistence, negative sampling and leakage-safe training splits."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import cv2
from fastapi.testclient import TestClient

from backend import db, review, training
from backend.main import app

# Authentication is covered by tests/test_auth.py.
app.state.auth_disabled = True


class ReviewTests(unittest.TestCase):
    def test_dashboard_capture_setting_overrides_env_without_restart(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(review, "REVIEW_CAPTURE", False):
            db.init_db()
            self.assertFalse(review.capture_enabled())
            review.set_capture_enabled(True)
            self.assertTrue(review.capture_enabled())
            review.set_capture_enabled(False)
            self.assertFalse(review.capture_enabled())

    def test_global_off_overrides_exclusive_and_global_change_clears_overrides(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(review, "REVIEW_CAPTURE", False):
            db.init_db()
            review.set_capture_policy("global", True, 2)
            review.set_capture_policy("exclusive", False, 5, ["cam1"])
            self.assertEqual(review.capture_for_camera("cam1"), (False, 5))
            self.assertEqual(review.capture_for_camera("cam2"), (True, 2))
            review.set_capture_policy("global", False, 1)
            self.assertEqual(review.capture_for_camera("cam2"), (False, 1))
            self.assertEqual(review.capture_policy()["overrides"], {})
            review.set_capture_policy("exclusive", True, 3, ["cam1"])
            self.assertEqual(review.capture_for_camera("cam1"), (False, 3))

    def test_hourly_quota_is_per_camera_and_survives_sample_deletion(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(review, "ROOT", Path(folder) / "images"), \
             patch.object(review, "_last_capture", {}):
            db.init_db()
            review.set_capture_policy("global", True, 1)
            frame = np.zeros((240, 240, 3), np.uint8)
            args = (frame, [(10, 10, 150, 200)], [.1], [7])
            review.maybe_capture("cam1", *args)
            first = db.list_review_samples("cam1")[0]
            self.assertEqual(db.review_captures_last_hour("cam1"), 1)
            Path(db.delete_review_sample(first["id"])).unlink()
            review.maybe_capture("cam1", *args)
            self.assertEqual(len(db.list_review_samples("cam1")), 0)
            review.maybe_capture("cam2", *args)
            self.assertEqual(len(db.list_review_samples("cam2")), 1)

    def test_add_label_filter_and_delete_image(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(review, "ROOT", Path(folder) / "images"):
            db.init_db()
            frame = np.zeros((100, 100, 3), dtype=np.uint8)
            sample_id = review.save_image("cam1", frame, "upload")
            saved = db.get_review_sample(sample_id)
            self.assertTrue(Path(saved["image_path"]).is_file())
            self.assertEqual(saved["label"], "unreviewed")
            self.assertTrue(db.label_review_sample(sample_id, "calling"))
            self.assertEqual(len(db.list_review_samples("cam1", "calling")), 1)
            self.assertEqual(len(db.list_review_samples("cam2")), 0)
            Path(db.delete_review_sample(sample_id)).unlink()
            self.assertIsNone(db.get_review_sample(sample_id))

    def test_distant_low_score_can_be_sampled(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(review, "ROOT", Path(folder) / "images"), \
             patch.object(review, "REVIEW_CAPTURE", True), \
             patch.object(review, "_last_capture", {}):
            db.init_db()
            review.maybe_capture("cam1", np.zeros((240, 240, 3), np.uint8),
                                 [(10, 10, 150, 200)], [.1], [7])
            self.assertEqual(db.list_review_samples()[0]["source"], "low-score")


class TrainSplitTests(unittest.TestCase):
    def test_training_respects_person_box_instead_of_full_scene(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scene.jpg"
            image = np.zeros((100, 200, 3), dtype=np.uint8)
            image[:, 100:] = [0, 0, 255]
            cv2.imwrite(str(path), image)
            rows = [{"id": "one", "image_path": str(path),
                     "person_box": [100, 0, 200, 100], "label": "calling"}]
            crops, labels = training._arrays(rows)
            np.testing.assert_allclose(crops[0, 100, 100], [1, 0, 0], atol=.01)
            self.assertEqual(labels[0], 1)

    @staticmethod
    def rows():
        return [{"id": f"{day}-{label}-{i}", "camera_id": "cam1",
                 "created_at": f"2026-10-{day:02d}T10:00:00",
                 "label": label}
                for day in range(1, 4)
                for label in ("calling", "no_use") for i in range(12)]

    def test_holdout_is_by_camera_day_and_contains_both_classes(self):
        train, valid = training.split_rows(self.rows())
        key = lambda r: (r["camera_id"], r["created_at"][:10])
        self.assertFalse({key(r) for r in train} & {key(r) for r in valid})
        self.assertEqual({r["label"] for r in valid}, {"calling", "no_use"})

    def test_single_day_is_not_sufficient_validation(self):
        rows = [r for r in self.rows() if r["created_at"].startswith("2026-10-01")]
        with self.assertRaises(ValueError):
            training.split_rows(rows)

    def test_no_person_is_not_used_as_binary_negative(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(review, "ROOT", Path(folder) / "images"):
            db.init_db()
            sample_id = review.save_image("cam1", np.zeros((100, 100, 3), np.uint8))
            db.label_review_sample(sample_id, "no_person")
            self.assertEqual(training.labeled_rows(), [])

    def test_deploy_requires_validated_improvement(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(training, "ROOT", Path(folder)), \
             patch.object(training, "STATUS", Path(folder) / "status.json"), \
             patch.object(training, "ACTIVE", Path(folder) / "active.json"):
            model_id = "a" * 32
            (Path(folder) / (model_id + ".tflite")).write_bytes(b"fake")
            training._write_json(training.STATUS, {
                "state": "complete", "id": model_id, "eligible": False})
            with self.assertRaises(ValueError):
                training.deploy(model_id)
            training._write_json(training.STATUS, {
                "state": "complete", "id": model_id, "eligible": True})
            self.assertEqual(training.deploy(model_id)["active_id"], model_id)
            self.assertIsNone(training.rollback()["active_id"])

class ReviewApiTests(unittest.TestCase):
    def test_upload_label_image_and_delete(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(review, "ROOT", Path(folder) / "images"):
            db.init_db()
            camera = db.create_camera({"name": "Cam", "address": "rtsp://cam"})
            image = np.zeros((100, 100, 3), dtype=np.uint8)
            encoded = cv2.imencode(".jpg", image)[1].tobytes()
            client = TestClient(app)
            bad = client.post("/api/training/samples", data={"camera_id": "unknown"},
                              files={"file": ("one.jpg", encoded, "image/jpeg")})
            self.assertEqual(bad.status_code, 404)
            response = client.post("/api/training/samples",
                                   data={"camera_id": camera.id},
                                   files={"file": ("one.jpg", encoded, "image/jpeg")})
            self.assertEqual(response.status_code, 201)
            sample_id = response.json()["ids"][0]
            self.assertEqual(client.get(f"/api/training/samples/{sample_id}/image").status_code, 200)
            self.assertEqual(client.put(f"/api/training/samples/{sample_id}/box",
                                        json={"box": [-1, 0, 50, 50]}).status_code, 400)
            self.assertEqual(client.put(f"/api/training/samples/{sample_id}/box",
                                        json={"box": [10, 10, 90, 90]}).status_code, 200)
            self.assertEqual(db.get_review_sample(sample_id)["person_box"],
                             [10, 10, 90, 90])
            self.assertEqual(client.put(f"/api/training/samples/{sample_id}/box",
                                        json={"box": None}).status_code, 200)
            self.assertIsNone(db.get_review_sample(sample_id)["person_box"])
            self.assertEqual(client.put(f"/api/training/samples/{sample_id}",
                                        json={"label": "maybe"}).status_code, 400)
            self.assertEqual(client.put(f"/api/training/samples/{sample_id}",
                                        json={"label": "no_person"}).status_code, 200)
            self.assertEqual(client.get("/api/training/samples",
                                        params={"label": "no_person"}).json()["samples"][0]["id"],
                             sample_id)
            self.assertEqual(client.put(f"/api/training/samples/{sample_id}",
                                        json={"label": "no_use"}).status_code, 200)
            self.assertEqual(client.get("/api/training/samples",
                                        params={"label": "no_use"}).json()["samples"][0]["id"],
                             sample_id)
            self.assertEqual(client.delete(f"/api/training/samples/{sample_id}").status_code, 200)
            self.assertEqual(client.get(f"/api/training/samples/{sample_id}/image").status_code, 404)

    def test_capture_toggle_api_is_persistent(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(review, "REVIEW_CAPTURE", False):
            db.init_db()
            client = TestClient(app)
            self.assertFalse(client.get("/api/training/capture").json()["enabled"])
            self.assertTrue(client.put("/api/training/capture",
                                       json={"enabled": True}).json()["enabled"])
            self.assertTrue(client.get("/api/training/capture").json()["enabled"])
            self.assertEqual(client.put("/api/training/capture", json={
                "scope": "exclusive", "camera_ids": ["missing"], "enabled": True,
                "max_per_hour": 5}).status_code, 404)
            self.assertEqual(client.put("/api/training/capture", json={
                "scope": "global", "enabled": True,
                "max_per_hour": 0}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
