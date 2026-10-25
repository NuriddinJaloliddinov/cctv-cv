"""Directional entrance/exit configuration, tracking and alert contracts."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from backend import alerts, crossing, db, pipeline
from backend.main import app
from backend import main
from tests.test_pipeline_detect_width import FakeSource

# Authentication is covered by tests/test_auth.py.
app.state.auth_disabled = True


def person(y, x=.5):
    return (int(x * 200 - 20), int(y * 200 - 90),
            int(x * 200 + 20), int(y * 200), .85)


def scan(monitor, ys, xs=None, tid=1):
    hits = []
    for i, y in enumerate(ys):
        hits.extend(monitor.update([tid], [person(y, xs[i] if xs else .5)],
                                   (200, 200, 3), float(i) * .2))
    return hits


class CrossingTests(unittest.TestCase):
    def monitor(self, direction="positive_to_negative"):
        return crossing.CrossingMonitor("entrance", [.25, .5, .75, .5],
                                        direction)

    def test_requires_stable_crossing_and_only_the_selected_direction(self):
        self.assertEqual(scan(self.monitor(), [.7, .68, .49, .51, .48, .46]),
                         [(0, 1)])
        self.assertEqual(scan(self.monitor(), [.3, .32, .7, .68]), [])
        self.assertEqual(scan(self.monitor("negative_to_positive"),
                              [.3, .32, .64, .68]), [(0, 1)])

    def test_new_track_jitter_and_line_extension_do_not_alert(self):
        self.assertEqual(scan(self.monitor(), [.3, .32]), [])
        self.assertEqual(scan(self.monitor(), [.7, .69, .49, .51, .5, .49]), [])
        self.assertEqual(scan(self.monitor(), [.7, .69, .36, .3],
                              xs=[.95] * 4), [])

    def test_gap_and_cooldown_suppress_false_second_hit(self):
        monitor = self.monitor()
        scan(monitor, [.7, .68])
        self.assertEqual(monitor.update([1], [person(.3)], (200, 200, 3), 4.), [])
        self.assertEqual(scan(self.monitor(),
                              [.7, .68, .36, .3, .7, .68, .36, .3]), [(0, 1)])

    def test_invalid_line_or_role_rejected(self):
        for role, line, direction in [
                ("anything", [.1, .2, .8, .2], "positive_to_negative"),
                ("exit", [.1, .1, .11, .11], "positive_to_negative"),
                ("exit", [-.1, .1, .8, .1], "positive_to_negative"),
                ("entrance", [.1, .1, .9, .1], "sideways"),
                (None, [.1, .1, .9, .1], None)]:
            with self.assertRaises(ValueError):
                crossing.validate(role, line, direction)

    def test_normalized_line_works_at_another_resolution(self):
        monitor = self.monitor()
        hits = []
        for i, y in enumerate((.7, .68, .36, .3)):
            box = tuple(v * 2 if j < 4 else v for j, v in enumerate(person(y)))
            hits += monitor.update([1], [box], (400, 400, 3), i * .2)
        self.assertEqual(hits, [(0, 1)])


class CameraRoleApiTests(unittest.TestCase):
    def test_live_camera_updates_monitor_without_restart(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")):
            db.init_db()
            camera = db.create_camera({"name": "Door", "address": "rtsp://door"})
            with patch.object(pipeline, "get_pipeline") as running:
                response = TestClient(app).put(f"/api/cameras/{camera.id}", json={
                    "role": "entrance", "tripwire": [.2, .5, .8, .5],
                    "tripwire_direction": "positive_to_negative"})
                self.assertEqual(response.status_code, 200)
                running.return_value.set_crossing.assert_called_once_with(
                    "entrance", [.2, .5, .8, .5], "positive_to_negative")

    def test_role_persists_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")):
            db.init_db()
            client = TestClient(app)
            camera = db.create_camera({"name": "Door", "address": "rtsp://door"})
            path = f"/api/cameras/{camera.id}"
            self.assertEqual(client.put(path, json={"role": "entrance"}).status_code, 400)
            changed = client.put(path, json={
                "role": "exit", "tripwire": [.1, .5, .9, .5],
                "tripwire_direction": "negative_to_positive"})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(client.get(path).json()["tripwire"], [.1, .5, .9, .5])
            self.assertEqual(client.put(path, json={"role": None}).status_code, 200)
            self.assertIsNone(db.get_camera(camera.id).tripwire)
            self.assertIsNone(db.get_camera(camera.id).tripwire_direction)

    def test_frame_for_line_uses_running_camera_source(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")):
            db.init_db()
            camera = db.create_camera({"name": "Door", "address": "rtsp://door",
                                       "rtsp_url": "rtsp://door/live"})

            class Source:
                def status(self):
                    return {"capture_age_ms": 50}

                def latest(self):
                    return 1, np.zeros((200, 300, 3), np.uint8)

            running = type("FakeRunning", (), {"_source": Source()})()
            with patch.object(pipeline, "get_pipeline", return_value=running):
                response = TestClient(app).get(
                    f"/api/cameras/{camera.id}/snapshot")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/jpeg")
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_frame_for_line_starts_temporary_reader_when_camera_stopped(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")):
            db.init_db()
            camera = db.create_camera({"name": "Door", "address": "rtsp://door",
                                       "rtsp_url": "rtsp://door/live"})

            class Reader:
                instances = []

                def __init__(self, url):
                    self.url = url
                    self.instances.append(self)

                def start(self):
                    self.started = True

                def latest(self):
                    return 1, np.zeros((60, 80, 3), np.uint8)

                def stop(self):
                    self.stopped = True

            with patch.object(pipeline, "get_pipeline", return_value=None), \
                 patch.object(main, "FrameSource", Reader):
                response = TestClient(app).get(
                    f"/api/cameras/{camera.id}/snapshot")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(Reader.instances[0].url, camera.rtsp_url)
            self.assertTrue(Reader.instances[0].started)
            self.assertTrue(Reader.instances[0].stopped)


class CrossingPipelineTests(unittest.TestCase):
    def test_running_pipeline_reports_named_crossing_once(self):
        class Detector:
            def __init__(self):
                self.i = 0

            def detect_objects(self, frame):
                ys = (.68, .66, .63, .57, .45, .42)
                y = ys[min(self.i, len(ys) - 1)]
                self.i += 1
                bottom = round(frame.shape[0] * y)
                return [(280, bottom - 160, 360, bottom, .9)], []

        with (patch.object(pipeline, "FrameSource", FakeSource),
              patch.object(pipeline, "get_detector", return_value=Detector()),
              patch.object(pipeline.face_review, "effective", return_value={"enabled": False}),
              patch.object(pipeline, "identify", return_value=[("Alice", .9)]),
              patch.object(pipeline, "DETECT_INTERVAL", .05),
              patch.object(pipeline, "PHONE_USE_INTERVAL", 999),
              patch.object(pipeline, "active_model_path", return_value="unused"),
              patch.object(pipeline.Path, "is_file", return_value=False),
              patch.object(pipeline.alerts, "report_crossing") as report):
            p = pipeline.Pipeline("cam", "Door", "unused", "cpu", 10,
                                  role="entrance", tripwire=[.2, .5, .8, .5],
                                  tripwire_direction="positive_to_negative",
                                  phone_use_detection=False)
            p.start()
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and report.call_count < 1:
                    time.sleep(.02)
                self.assertEqual(report.call_count, 1)
                self.assertEqual(report.call_args.kwargs["person_name"], "Alice")
                self.assertEqual(report.call_args.kwargs["role"], "entrance")
                time.sleep(.15)
                self.assertEqual(report.call_count, 1)
            finally:
                p.stop()


class CrossingAlertTests(unittest.TestCase):
    def test_unknown_crossing_saves_snapshot_and_caption(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(alerts, "EVENTS_DIR", Path(folder)), \
             patch.object(alerts.telegram_bot.service, "is_configured",
                          return_value=True), \
             patch.object(alerts.telegram_bot.service, "notify") as notify:
            db.init_db()
            event = alerts.report_crossing(
                "cam", "Front", 2, "Unknown", "entrance", .8,
                np.zeros((200, 200, 3), np.uint8), (50, 50, 120, 170),
                (.2, .8, .8, .8), "positive_to_negative")
            self.assertEqual(event["kind"], "entered")
            self.assertIn("Unknown entered", event["caption"])
            self.assertTrue(Path(event["snapshot_path"]).is_file())
            notify.assert_called_once()

    def test_exit_caption_escapes_recognized_name(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(alerts, "EVENTS_DIR", Path(folder)), \
             patch.object(alerts.telegram_bot.service, "is_configured",
                          return_value=True), \
             patch.object(alerts.telegram_bot.service, "notify"):
            db.init_db()
            event = alerts.report_crossing(
                "cam", "Back", 5, "A <B>", "exit", .9,
                np.zeros((100, 100, 3), np.uint8), (10, 10, 70, 90),
                (.2, .5, .8, .5), "negative_to_positive")
            self.assertEqual(event["kind"], "left")
            self.assertIn("A &lt;B&gt; left", event["caption"])


if __name__ == "__main__":
    unittest.main()
