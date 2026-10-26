"""Server assessments must be honest about evidence and protect measurements."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend import db, main, server_capacity


def plan(**changes):
    return server_capacity.CapacityPlan(**{
        "cameras": 1, "fps": 10, "width": 1920, "height": 1080,
        "people_per_frame": 0, "phone_detection": True,
        "face_recognition": False, "viewers": 0,
        **changes,
    })


HARDWARE = {"logical_cores": 8, "physical_cores": 4, "ram_gb": 16,
            "available_ram_gb": 8, "disk_free_gb": 100,
            "providers": [], "gpus": []}


class CapacityAssessmentTests(unittest.TestCase):
    def test_no_cameras_cannot_assert_sufficiency_or_invent_cpu_spec(self):
        result = server_capacity.assess(plan(), [], [], 10, 15, HARDWARE, 1)
        self.assertEqual(result["verdict"], "unverified")
        self.assertIsNone(result["recommendation"]["cpu_logical_cores_estimate"])

    def test_underperforming_camera_and_disk_are_reported(self):
        camera = {"camera_id": "one", "name": "Door", "connected": True,
                  "source_width": 1920, "source_height": 1080,
                  "configured_fps": 10, "phone_use_detection_enabled": True,
                  "person_detection_enabled": True, "device": "cpu",
                  "inference_count": 1, "source_fps": 10}
        result = server_capacity.assess(
            plan(snapshot_mb_per_day=5000, retention_days=30), [camera],
            [{**camera, "inference_count": 5}], 92, 15, HARDWARE, 1)
        self.assertEqual(result["verdict"], "insufficient")
        self.assertTrue(any("inference" in item for item in result["issues"]))
        self.assertTrue(any("disk" in item for item in result["issues"]))

    def test_nonmatching_workload_stays_unverified(self):
        camera = {"camera_id": "one", "name": "Door", "connected": True,
                  "source_width": 1280, "source_height": 720,
                  "configured_fps": 10, "phone_use_detection_enabled": True,
                  "person_detection_enabled": True, "device": "cpu",
                  "inference_count": 0, "source_fps": 10}
        result = server_capacity.assess(
            plan(width=1920), [camera], [{**camera, "inference_count": 75}],
            25, 15, HARDWARE, 1)
        self.assertEqual(result["verdict"], "unverified")

    def test_matching_cpu_workload_can_be_reported_sufficient_with_caveats(self):
        camera = {"camera_id": "one", "name": "Door", "connected": True,
                  "source_width": 1920, "source_height": 1080,
                  "configured_fps": 10, "phone_use_detection_enabled": True,
                  "person_detection_enabled": True, "device": "cpu",
                  "inference_count": 0, "source_fps": 10}
        result = server_capacity.assess(
            plan(), [camera], [{**camera, "inference_count": 75}],
            30, 15, HARDWARE, 1)
        self.assertEqual(result["verdict"], "sufficient")
        self.assertTrue(result["caveats"])


class CapacityApiTests(unittest.TestCase):
    def test_check_is_admin_only_and_validates_inputs(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(db, "DB_PATH", str(Path(directory) / "test.db")):
            db.init_db()
            previous = getattr(main.app.state, "auth_disabled", False)
            main.app.state.auth_disabled = False
            try:
                with TestClient(main.app) as admin, TestClient(main.app) as reader:
                    payload = plan().model_dump()
                    self.assertEqual(reader.post("/api/server/check", json=payload).status_code, 401)
                    self.assertEqual(admin.post("/api/auth/login", json={
                        "username": "admin", "password": "12345"}).status_code, 200)
                    admin.post("/api/users", json={"username": "reader",
                                                   "password": "readerpass", "role": "reader"})
                    reader.post("/api/auth/login", json={
                        "username": "reader", "password": "readerpass"})
                    self.assertEqual(reader.post("/api/server/check", json=payload).status_code, 403)
                    self.assertEqual(admin.post("/api/server/check", json={
                        **payload, "cameras": 0}).status_code, 422)
                    with patch.object(server_capacity, "measure", new_callable=AsyncMock,
                                      return_value={"verdict": "unverified"}) as measure:
                        response = admin.post("/api/server/check", json=payload)
                    self.assertEqual(response.status_code, 200, response.text)
                    measure.assert_awaited_once()
            finally:
                main.app.state.auth_disabled = previous


if __name__ == "__main__":
    unittest.main()
