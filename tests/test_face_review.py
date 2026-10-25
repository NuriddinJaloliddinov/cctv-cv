"""Opt-in unknown-face capture, deduplication and reviewed enrollment."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from fastapi.testclient import TestClient

from backend import db, enroll, face_review, main, recognizer, pipeline, detection_settings

# Authentication is covered by tests/test_auth.py.
main.app.state.auth_disabled = True
from tests.test_pipeline_detect_width import FakeSource


FACE = np.random.default_rng(41).integers(0, 255, (128, 128, 3), dtype=np.uint8)
EMB = np.r_[1., np.zeros(127)].astype(np.float32)
OTHER = np.r_[0., 1., np.zeros(126)].astype(np.float32)


class FaceReviewTests(unittest.TestCase):
    def test_capture_disabled_by_default_and_global_off_blocks_camera_override(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "db.sqlite")):
            db.init_db()
            self.assertFalse(face_review.effective("a")["enabled"])
            face_review.save_policy("exclusive", True, 5, ["a"])
            self.assertFalse(face_review.effective("a")["enabled"])
            face_review.save_policy("global", True, 2)
            face_review.save_policy("exclusive", False, 5, ["a"])
            self.assertFalse(face_review.effective("a")["enabled"])
            self.assertTrue(face_review.effective("b")["enabled"])
            self.assertEqual(face_review.effective("b")["per_hour"], 2)

    def test_dedup_and_hourly_limit_survive_candidate_discard(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "db.sqlite")), \
             patch.object(face_review, "ROOT", Path(folder) / "images"), \
             patch.object(face_review, "_last_track", {}), \
             patch.object(face_review, "_last_attempt", {}), \
             patch.object(enroll, "extract_from_image",
                          side_effect=lambda data: ([(FACE, EMB)], None)):
            db.init_db()
            face_review.save_policy("global", True, 1)
            first = face_review.maybe_capture("cam", 1, FACE, EMB, now=1000)
            self.assertIsNotNone(first)
            self.assertTrue(Path(db.get_face_candidate(first)["image_path"]).is_file())
            self.assertIsNone(face_review.maybe_capture("cam", 2, FACE, OTHER, now=1010))
            self.assertTrue(face_review.discard(first))
            self.assertEqual(db.face_candidate_count(), 0)
            self.assertEqual(db.face_capture_count("cam"), 1)
            self.assertIsNone(face_review.maybe_capture("cam", 3, FACE, OTHER, now=1020))

    def test_dedupe_prevents_similar_face_from_other_camera(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "db.sqlite")), \
             patch.object(face_review, "ROOT", Path(folder) / "images"), \
             patch.object(face_review, "_last_track", {}), \
             patch.object(face_review, "_last_attempt", {}), \
             patch.object(enroll, "extract_from_image",
                          return_value=([(FACE, EMB)], None)):
            db.init_db()
            face_review.save_policy("global", True, 3)
            self.assertIsNotNone(face_review.maybe_capture("a", 1, FACE, EMB, now=1000))
            self.assertIsNone(face_review.maybe_capture("b", 1, FACE, EMB, now=1010))
            self.assertEqual(db.face_candidate_count(), 1)


class RecognizerUnknownTests(unittest.TestCase):
    def test_only_unmatched_usable_face_is_returned(self):
        face = np.array([30, 20, 90, 90, 55, 50, 100, 50, 80, 70,
                         60, 90, 90, 90, .9], dtype=np.float32)

        class Engine:
            def detect(self, crop):
                return [face]

            def embed(self, crop, detected):
                return EMB

        class Matcher:
            def match(self, embedding):
                return None

        with patch.object(recognizer, "get_engine", return_value=Engine()), \
             patch.object(recognizer, "get_matcher", return_value=Matcher()), \
             patch.object(recognizer, "face_quality_reason", return_value=None):
            result, unknown = recognizer.identify(
                np.zeros((400, 400, 3), np.uint8),
                [(80, 20, 250, 350, .9)], with_unknown=True)
            self.assertEqual(result, [None])
            self.assertEqual(len(unknown), 1)
            self.assertEqual(unknown[0][0], 0)
            self.assertGreater(unknown[0][1].shape[0], 64)


class CandidateApiTests(unittest.TestCase):
    def test_global_and_exclusive_api_respects_person_switch(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "db.sqlite")):
            db.init_db()
            cam = db.create_camera({"name": "A", "address": "rtsp://a"})
            other = db.create_camera({"name": "B", "address": "rtsp://b"})
            client = TestClient(main.app)
            path = "/api/enrollment/review/settings"
            self.assertFalse(client.get(path).json()["enabled"])
            self.assertEqual(client.put(path, json={
                "scope": "exclusive", "camera_ids": ["missing"],
                "enabled": True, "per_hour": 2}).status_code, 404)
            self.assertEqual(client.put(path, json={
                "scope": "global", "enabled": True, "per_hour": 0}).status_code, 422)
            self.assertEqual(client.put(path, json={
                "scope": "global", "enabled": True, "per_hour": 2}).status_code, 200)
            policy = client.put(path, json={
                "scope": "exclusive", "camera_ids": [cam.id],
                "enabled": False, "per_hour": 5}).json()
            self.assertFalse(policy["effective"][cam.id]["enabled"])
            self.assertTrue(policy["effective"][other.id]["enabled"])
            detection_settings.save("exclusive", False, True, [other.id])
            self.assertFalse(client.get(path).json()["effective"][other.id]["enabled"])

    def test_assign_existing_or_new_requires_explicit_identity(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "db.sqlite")), \
             patch.object(face_review, "ROOT", Path(folder) / "images"), \
             patch.object(main, "PERSONS_DIR", Path(folder) / "people"), \
             patch.object(main, "_require_face_engine"), \
             patch.object(enroll, "extract_from_image",
                          return_value=([(FACE, EMB)], None)):
            db.init_db()
            cam = db.create_camera({"name": "Gate", "address": "rtsp://gate"})
            person = db.create_person("alice", "Alice", "", None)
            face_review.ROOT.mkdir(parents=True, exist_ok=True)
            client = TestClient(main.app)

            def candidate(id_value):
                path = face_review.ROOT / f"{id_value}.jpg"
                cv2.imwrite(str(path), FACE)
                db.add_face_candidate(id_value, cam.id, str(path), EMB)
                return path

            image = candidate("existing")
            self.assertEqual(client.post(
                "/api/enrollment/review/candidates/existing/assign",
                json={"person_id": "alice", "name": "Wrong"}).status_code, 400)
            self.assertEqual(client.post(
                "/api/enrollment/review/candidates/existing/assign",
                json={"person_id": "missing"}).status_code, 404)
            self.assertTrue(image.is_file())
            result = client.post(
                "/api/enrollment/review/candidates/existing/assign",
                json={"person_id": "alice"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(db.count_embeddings("alice"), 1)
            self.assertFalse(image.exists())
            self.assertEqual(client.get(
                "/api/enrollment/review/candidates/existing/image").status_code, 404)

            candidate("new")
            result = client.post(
                "/api/enrollment/review/candidates/new/assign",
                json={"name": "Bob"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(db.get_person(result.json()["person_id"])["name"], "Bob")
            self.assertEqual(db.count_embeddings(result.json()["person_id"]), 1)


class FaceReviewPipelineTests(unittest.TestCase):
    def test_live_unmatched_face_is_queued_but_person_off_stops_it(self):
        class Detector:
            def detect_objects(self, frame):
                return [(80, 20, 240, 440, .95)], []

        def identify(frame, boxes, with_unknown=False):
            self.assertTrue(with_unknown)
            return [None], [(0, FACE, EMB)]

        with (patch.object(pipeline, "FrameSource", FakeSource),
              patch.object(pipeline, "get_detector", return_value=Detector()),
              patch.object(pipeline, "active_model_path", return_value="unused"),
              patch.object(pipeline.Path, "is_file", return_value=False),
              patch.object(pipeline.face_review, "effective", return_value={"enabled": True}),
              patch.object(pipeline.face_review, "maybe_capture") as capture,
              patch.object(pipeline, "identify", side_effect=identify),
              patch.object(pipeline, "FACE_INTERVAL", .05),
              patch.object(pipeline, "DETECT_INTERVAL", .05),
              patch.object(pipeline, "PHONE_USE_INTERVAL", 999)):
            camera = pipeline.Pipeline("cam", "Camera", "unused", "cpu", 10)
            camera.start()
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not capture.called:
                    time.sleep(.02)
                self.assertTrue(capture.called)
                self.assertEqual(capture.call_args.args[0], "cam")
                camera.set_detection(False, False)
                time.sleep(.12)
                count = capture.call_count
                time.sleep(.15)
                self.assertEqual(capture.call_count, count)
            finally:
                camera.stop()


if __name__ == "__main__":
    unittest.main()
