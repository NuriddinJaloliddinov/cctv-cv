"""Temporal confirmation and cooldown for phone-use alerts."""
import unittest
from unittest.mock import patch

import numpy as np

from backend import pipeline
from backend.phone_use import PhoneUse

PERSON = (100, 100, 300, 600, 0.9)
PHONE = (180, 260, 220, 330)
FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


class PhoneStateTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = pipeline.Pipeline(
            "cam1", "Front door", "unused", "cpu", 10)

    def _tick(self, now, result):
        self.pipeline._update_phone_states(
            [7], [result], [PERSON], [("Alice", 0.9, "face")],
            FRAME, now)

    def test_alert_requires_persistence_and_fires_once(self):
        result = PhoneUse("looking", 0.8, PHONE)
        with patch.object(pipeline, "PHONE_USE_MIN_SECONDS", 2.0), \
             patch.object(pipeline, "PHONE_USE_GRACE_SECONDS", 1.0), \
             patch.object(pipeline, "PHONE_USE_INTERVAL", 0.5), \
             patch.object(pipeline, "PHONE_ALERT_COOLDOWN", 120.0), \
             patch.object(pipeline.alerts, "report_phone_use") as report:
            for now in (10.0, 10.5, 11.0, 11.5):
                self._tick(now, result)
            report.assert_not_called()

            self._tick(12.0, result)
            report.assert_called_once()
            args = report.call_args.kwargs
            self.assertEqual(args["kind"], "looking")
            self.assertEqual(args["person_name"], "Alice")
            self.assertEqual(args["track_id"], 7)

            # Continued use does not produce another alert in the cooldown.
            for now in (12.5, 13.0, 14.0):
                self._tick(now, result)
            report.assert_called_once()

    def test_state_clears_after_evidence_disappears(self):
        result = PhoneUse("calling", 0.8, PHONE)
        with patch.object(pipeline, "PHONE_USE_INTERVAL", 0.5), \
             patch.object(pipeline, "PHONE_USE_GRACE_SECONDS", 1.0):
            self._tick(10.0, result)
            self.assertEqual(
                self.pipeline._phone_active(7, 10.2), "calling")
            self.assertIsNone(self.pipeline._phone_active(7, 12.0))

    def test_overlay_draws_detected_and_active_phones(self):
        frame = np.zeros((220, 220, 3), dtype=np.uint8)
        detected = (10, 10, 50, 60, 0.6)
        active = (100, 100, 150, 170, 0.9)
        pipeline._draw(frame, [], [], [], [detected], [active])

        self.assertEqual(tuple(frame[10, 10]), pipeline.PHONE_BOX_COLOR)
        self.assertEqual(tuple(frame[100, 100]), pipeline.ACTIVE_PHONE_COLOR)


if __name__ == "__main__":
    unittest.main()
