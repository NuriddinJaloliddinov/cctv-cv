"""Global/exclusive switches and live inference dependencies."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient

from backend import db, detection_settings, pipeline
from backend.main import app

# Authentication is covered by tests/test_auth.py.
app.state.auth_disabled = True
from tests.test_pipeline_detect_width import FakeSource


class DetectionPolicyTests(unittest.TestCase):
    def test_defaults_and_override_precedence(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "db.sqlite")):
            db.init_db()
            self.assertEqual(detection_settings.effective("a"),
                             {"person": True, "phone": True})
            detection_settings.save("exclusive", True, False, ["a"])
            self.assertEqual(detection_settings.effective("a"),
                             {"person": True, "phone": False})
            self.assertEqual(detection_settings.effective("b"),
                             {"person": True, "phone": True})
            detection_settings.save("global", False, True)
            self.assertEqual(detection_settings.policy()["overrides"], {})
            detection_settings.save("exclusive", True, True, ["a"])
            self.assertEqual(detection_settings.effective("a"),
                             {"person": False, "phone": False})
            detection_settings.save("global", True, True)
            detection_settings.save("exclusive", False, True, ["b"])
            self.assertEqual(detection_settings.effective("b"),
                             {"person": False, "phone": False})

    def test_api_rejects_unknown_camera_and_applies_live_change(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "db.sqlite")):
            db.init_db()
            a = db.create_camera({"name": "A", "address": "rtsp://a"})
            b = db.create_camera({"name": "B", "address": "rtsp://b"})
            running = Mock()
            client = TestClient(app)
            with patch.object(pipeline, "get_pipeline", side_effect=lambda cid:
                              running if cid == a.id else None):
                bad = client.put("/api/detection/settings", json={
                    "scope": "exclusive", "person": False, "phone": False,
                    "camera_ids": ["missing"]})
                self.assertEqual(bad.status_code, 404)
                self.assertTrue(detection_settings.effective(a.id)["person"])
                result = client.put("/api/detection/settings", json={
                    "scope": "exclusive", "person": True, "phone": False,
                    "camera_ids": [a.id]})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["effective"][a.id],
                                 {"person": True, "phone": False})
                self.assertEqual(result.json()["effective"][b.id],
                                 {"person": True, "phone": True})
                running.set_detection.assert_called_with(True, False)
                result = client.put("/api/detection/settings", json={
                    "scope": "global", "person": False, "phone": True})
                self.assertEqual(result.json()["overrides"], {})
                running.set_detection.assert_called_with(False, False)

    def test_start_uses_persisted_flags_and_camera_deletion_cleans_override(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "db.sqlite")):
            db.init_db()
            cam = db.create_camera({"name": "A", "address": "rtsp://a",
                                    "rtsp_url": "rtsp://a/live"})
            detection_settings.save("exclusive", True, False, [cam.id])
            client = TestClient(app)
            with patch.object(pipeline, "start_pipeline") as start:
                self.assertEqual(client.post("/api/start", json={
                    "camera_id": cam.id}).status_code, 200)
                self.assertTrue(start.call_args.kwargs["person_detection"])
                self.assertFalse(start.call_args.kwargs["phone_use_detection"])
            with patch.object(pipeline, "stop_pipeline"):
                self.assertEqual(client.delete(f"/api/cameras/{cam.id}").status_code, 200)
            self.assertEqual(detection_settings.policy()["overrides"], {})


class LiveDetectionTests(unittest.TestCase):
    def test_turning_person_off_keeps_video_but_stops_inference(self):
        detector = Mock()
        detector.detect_objects.return_value = ([(90, 100, 210, 430, .9)], [])
        detector.detect_phone_crops = None
        detector.detect_phone_tiles = None
        with (patch.object(pipeline, "FrameSource", FakeSource),
              patch.object(pipeline, "get_detector", return_value=detector),
              patch.object(pipeline.face_review, "effective", return_value={"enabled": False}),
              patch.object(pipeline, "active_model_path", return_value="unused"),
              patch.object(pipeline.Path, "is_file", return_value=False),
              patch.object(pipeline, "identify", return_value=[None]) as identify,
              patch.object(pipeline.review, "maybe_capture"),
              patch.object(pipeline, "DETECT_INTERVAL", .05),
              patch.object(pipeline, "PHONE_USE_INTERVAL", .05),
              patch.object(pipeline.phone_use_mod, "analyze", return_value=[None]) as analyze):
            camera = pipeline.Pipeline("synthetic", "Synthetic", "unused", "cpu", 10,
                                       person_detection=False)
            camera.start()
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and camera.get_latest_frame() is None:
                    time.sleep(.02)
                self.assertIsNotNone(camera.get_latest_frame())
                self.assertEqual(detector.detect_objects.call_count, 0)
                self.assertEqual(camera.status()["phone_use_detection_enabled"], False)

                camera.set_detection(True, False)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and detector.detect_objects.call_count < 2:
                    time.sleep(.02)
                self.assertGreaterEqual(detector.detect_objects.call_count, 2)
                self.assertTrue(identify.called)
                analyze.assert_not_called()
                self.assertEqual(camera.status()["phone_use_detection_enabled"], False)

                monitor = camera._crossing
                next_track_id = camera._tracker.next_id
                camera.set_detection(True, True)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not analyze.called:
                    time.sleep(.02)
                self.assertTrue(analyze.called)
                self.assertIs(camera._crossing, monitor)
                self.assertEqual(camera._tracker.next_id, next_track_id)

                camera.set_detection(False, True)
                self.assertFalse(camera.status()["person_detection_enabled"])
                self.assertFalse(camera.status()["phone_use_detection_enabled"])
                self.assertEqual(camera._boxes, [])
            finally:
                camera.stop()


if __name__ == "__main__":
    unittest.main()
