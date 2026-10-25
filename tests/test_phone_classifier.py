"""Optional model preprocessing, fallback, and classifier-only alerts."""
import unittest
from unittest.mock import patch
import time

import numpy as np

from backend import pipeline
from backend.phone_classifier import PhoneClassifier
from backend.phone_use import PhoneUse
from tests.test_pipeline_detect_width import FakeSource


class FakeInterpreter:
    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [{"shape": [1, 224, 224, 3], "dtype": np.float32,
                 "index": 1}]

    def get_output_details(self):
        return [{"shape": [1, 1], "dtype": np.float32, "index": 2}]

    def set_tensor(self, index, value):
        self.image = value

    def invoke(self):
        pass

    def get_tensor(self, index):
        return np.array([[0.91]], dtype=np.float32)


class ClassifierTests(unittest.TestCase):
    def test_live_pipeline_can_detect_use_without_a_phone_box(self):
        class FakeDetector:
            def detect_objects(self, frame):
                return [(10, 10, 180, 450, .9)], []

        class FakeClassifier:
            def predict(self, frame, people, max_people):
                return [.95]

        with (patch.object(pipeline, "FrameSource", FakeSource),
              patch.object(pipeline, "get_detector", return_value=FakeDetector()),
              patch.object(pipeline.review, "maybe_capture"),
              patch.object(pipeline.face_review, "effective", return_value={"enabled": False}),
              patch.object(pipeline, "get_classifier", return_value=FakeClassifier()),
              patch.object(pipeline.Path, "is_file", return_value=True),
              patch.object(pipeline, "identify", return_value=[None]),
              patch.object(pipeline, "DETECT_INTERVAL", .05),
              patch.object(pipeline, "PHONE_USE_INTERVAL", .05),
              patch.object(pipeline.phone_use_mod, "analyze", return_value=[None])):
            p = pipeline.Pipeline("cam", "Cam", "unused", "cpu", 10)
            p.start()
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if p.status()["phone_use"] == 1:
                        break
                    time.sleep(.02)
                else:
                    self.fail("classifier did not produce a live phone-use label")
                self.assertEqual(p.status()["phone_classifier"], "active")
                self.assertEqual(p.status()["phones"], 0)
                self.assertEqual(p._phone_use, ["using"])
            finally:
                p.stop()

    def test_crop_is_rgb_normalized_and_keeps_person_index(self):
        classifier = PhoneClassifier.__new__(PhoneClassifier)
        import threading
        classifier._lock = threading.Lock()
        classifier._interpreter = FakeInterpreter()
        classifier._input = 1
        classifier._output = 2
        frame = np.zeros((300, 300, 3), dtype=np.uint8)
        frame[:, :] = [0, 0, 255]  # BGR red
        scores = classifier.predict(frame, [(10, 10, 200, 290),
                                             (1, 1, 10, 10)], 2)
        self.assertAlmostEqual(scores[0], .91, places=5)
        self.assertIsNone(scores[1])
        self.assertEqual(classifier._interpreter.image.shape, (1, 224, 224, 3))
        np.testing.assert_allclose(classifier._interpreter.image[0, 0, 0],
                                   [1, 0, 0])

    def test_classifier_only_alert_is_unspecified_and_has_no_phone_box(self):
        p = pipeline.Pipeline("cam", "Cam", "unused", "cpu", 10)
        person = (10, 10, 150, 290, .9)
        frame = np.zeros((300, 200, 3), dtype=np.uint8)
        with patch.object(pipeline.alerts, "report_phone_use") as report:
            for t in (10., 10.5, 11., 11.5, 12.):
                p._update_phone_states([1], [PhoneUse("using", .9, None)],
                                       [person], [None], frame, t)
        report.assert_called_once()
        self.assertEqual(report.call_args.kwargs["kind"], "using")
        self.assertIsNone(report.call_args.kwargs["phone_box"])
        self.assertEqual(p._phone_active(1, 12.), "using")

    def test_geometry_result_upgrades_generic_label_not_reverse(self):
        p = pipeline.Pipeline("cam", "Cam", "unused", "cpu", 10)
        person = (10, 10, 150, 290, .9)
        frame = np.zeros((300, 200, 3), dtype=np.uint8)
        for t, result in [(10., PhoneUse("using", .9, None)),
                          (10.5, PhoneUse("looking", .3, (50, 80, 70, 110))),
                          (11., PhoneUse("using", .99, None))]:
            p._update_phone_states([1], [result], [person], [None], frame, t)
        self.assertEqual(p._phone_active(1, 11.), "looking")
        self.assertEqual(p._phone_tracks[1]["phone_box"], (50, 80, 70, 110))


if __name__ == "__main__":
    unittest.main()
