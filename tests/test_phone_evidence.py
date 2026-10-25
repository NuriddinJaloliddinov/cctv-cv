"""Phone-use episodes survive dropouts but still need sustained evidence."""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np

from backend import pipeline
from backend.phone_use import PhoneUse

PERSON = (100, 100, 300, 600, 0.9)      # w=200, h=500
PHONE = (180, 260, 220, 330)            # centre (200, 295)
FAR_PHONE = (110, 500, 150, 570)        # centre (130, 535): at thigh height
FRAME = np.zeros((480, 640, 3), dtype=np.uint8)
IDENT = ("Alice", 0.9, "face")


class PhoneEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = pipeline.Pipeline(
            "cam1", "Front door", "unused", "cpu", 10)

    def _tick(self, now, kind, phone_box=PHONE):
        result = None if kind is None else PhoneUse(kind, 0.8, phone_box)
        self.pipeline._update_phone_states(
            [7], [result], [PERSON], [IDENT], FRAME, now)

    def _patched(self, **overrides):
        settings = {
            "PHONE_USE_MIN_SECONDS": 2.0,
            "PHONE_USE_INTERVAL": 0.5,
            "PHONE_USE_GRACE_SECONDS": 1.0,
            "PHONE_USE_EPISODE_SECONDS": 3.0,
            "PHONE_USE_EVIDENCE": 0.5,
        }
        settings.update(overrides)
        stack = ExitStack()
        for name, value in settings.items():
            stack.enter_context(patch.object(pipeline, name, value))
        report = stack.enter_context(
            patch.object(pipeline.alerts, "report_phone_use"))
        return stack, report

    def test_short_dropouts_do_not_restart_the_episode(self):
        stack, report = self._patched()
        with stack:
            for now in (10.0, 10.5, 11.0):
                self._tick(now, "looking")
            # The phone is lost for 1.5 s while the person keeps using it.
            for now in (11.5, 12.0, 12.5):
                self._tick(now, None)
            report.assert_not_called()

            # The episode kept its start time, so the 2 s requirement is
            # already met the moment the phone is seen again.
            self._tick(13.0, "looking")
            report.assert_called_once()

    def test_a_long_absence_ends_the_episode(self):
        stack, report = self._patched()
        with stack:
            for now in (10.0, 10.5, 11.0):
                self._tick(now, "looking")
            for now in (11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.5):
                self._tick(now, None)

            # Past PHONE_USE_EPISODE_SECONDS the old timer is gone.
            self._tick(15.0, "looking")
            self._tick(16.5, "looking")
            report.assert_not_called()
            self._tick(17.0, "looking")
            report.assert_called_once()

    def test_scattered_hits_do_not_alert(self):
        stack, report = self._patched(PHONE_USE_EVIDENCE=0.7)
        with stack:
            for index, now in enumerate((10.0, 10.5, 11.0, 11.5, 12.0)):
                self._tick(now, "looking" if index % 2 == 0 else None)

            # Only half of the analyses show a phone: not enough to alert.
            report.assert_not_called()

    def test_a_jumping_phone_box_restarts_the_episode(self):
        stack, report = self._patched()
        with stack:
            for now in (10.0, 10.5, 11.0):
                self._tick(now, "looking")
            # Same person and kind, but the box moved to a different part of
            # the body: that is a new detection, not continued evidence.
            self._tick(11.5, "looking", FAR_PHONE)
            for now in (12.0, 12.5, 13.0):
                self._tick(now, "looking", FAR_PHONE)
            report.assert_not_called()

            self._tick(13.5, "looking", FAR_PHONE)
            report.assert_called_once()


if __name__ == '__main__':
    unittest.main()
