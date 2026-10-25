"""Constant-velocity prediction keeps a moving person's track id."""
import unittest

import cv2
import numpy as np

from backend.tracker import PersonTracker

RED = (0, 0, 220)
BLACK = (32, 32, 32)


def scene(boxes, size=(320, 480)):
    frame = np.full((*size, 3), 24, np.uint8)
    detections = []
    for x1, y1, x2, y2 in boxes:
        detections.append((x1, y1, x2, y2, .9))
        h = y2 - y1
        cv2.rectangle(frame, (x1, y1 + int(.20 * h)),
                      (x2, y1 + int(.55 * h)), RED, -1)
        cv2.rectangle(frame, (x1, y1 + int(.55 * h)),
                      (x2, y1 + int(.92 * h)), BLACK, -1)
    return frame, detections


class PredictionTests(unittest.TestCase):
    def setUp(self):
        self.tracker = PersonTracker(max_gap=2, identity_ttl=10)

    def update(self, boxes, now, faces=None):
        frame, detections = scene(boxes)
        return self.tracker.update(frame, detections, faces, now)

    def test_moving_person_is_reconnected_after_a_gap(self):
        self.update([(20, 60, 100, 260)], now=1, faces=[("Alice", .76)])
        # 60 px in 0.2 s => 300 px/s.
        self.update([(80, 60, 160, 260)], now=1.2)
        # 0.6 s later the person is 180 px further on: a comparison against the
        # last observed box would reject this, the prediction lands on it.
        self.assertEqual(
            self.update([(260, 60, 340, 260)], now=1.8),
            [("Alice", .76, "track")])

    def test_the_same_jump_for_a_stationary_person_is_rejected(self):
        box = (20, 60, 100, 260)
        self.update([box], now=1, faces=[("Alice", .76)])
        self.update([box], now=1.2)

        self.assertEqual(self.update([(260, 60, 340, 260)], now=1.8), [None])

    def test_velocity_does_not_survive_a_long_disappearance(self):
        self.update([(20, 60, 100, 260)], now=1)
        self.update([(80, 60, 160, 260)], now=1.2)

        # Well past TRACK_MAX_GAP: the old track and its velocity are gone.
        self.assertEqual(self.update([(300, 60, 380, 260)], now=4.5), [None])


if __name__ == '__main__':
    unittest.main()
