"""Deterministic tracking tests; no camera, models or database required."""
import unittest

import cv2
import numpy as np

from backend.tracker import PersonTracker


def scene(people, size=(320, 480)):
    frame = np.full((*size, 3), 24, np.uint8)
    boxes = []
    for (x1, y1, x2, y2), upper, lower in people:
        boxes.append((x1, y1, x2, y2, .9))
        h, w = y2 - y1, x2 - x1
        cv2.rectangle(frame, (x1, y1 + int(.20 * h)),
                      (x2, y1 + int(.55 * h)), upper, -1)
        cv2.rectangle(frame, (x1, y1 + int(.55 * h)),
                      (x2, y1 + int(.92 * h)), lower, -1)
    return frame, boxes


RED_BLUE = ((20, 60, 100, 260), (0, 0, 220), (220, 0, 0))
GREEN_BLACK = ((300, 60, 380, 260), (0, 220, 0), (32, 32, 32))


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = PersonTracker(max_gap=2, identity_ttl=10,
                                     visual_threshold=.88, visual_margin=.08)

    def update(self, people, faces=None, now=0):
        frame, boxes = scene(people)
        return self.tracker.update(frame, boxes, faces, now)

    def test_face_anchor_and_track_continuity(self):
        self.assertEqual(self.update([RED_BLUE], [('Alice', .76)], now=1),
                         [('Alice', .76, 'face')])
        moved = ((42, 60, 122, 260), *RED_BLUE[1:])
        self.assertEqual(self.update([moved], now=1.2),
                         [('Alice', .76, 'track')])
        self.assertEqual(self.update([moved], [None], now=1.4),
                         [('Alice', .76, 'track')])

    def test_unseen_person_is_not_identified_by_clothing(self):
        self.assertEqual(self.update([RED_BLUE], now=1), [None])
        self.assertEqual(self.update([RED_BLUE], now=1.2), [None])

    def test_visual_reconnect_short_gap_with_spatial_gate(self):
        self.update([RED_BLUE], [('Alice', .76)], now=1)
        self.update([], now=1.2)
        moved = ((126, 60, 206, 260), *RED_BLUE[1:])
        self.assertEqual(self.update([moved], now=2),
                         [('Alice', .76, 'visual')])
        self.assertEqual(self.update([moved], now=2.2),
                         [('Alice', .76, 'visual')])

    def test_different_outfit_or_far_away_not_reconnected(self):
        self.update([RED_BLUE], [('Alice', .76)], now=1)
        other = ((126, 60, 206, 260), *GREEN_BLACK[1:])
        self.assertEqual(self.update([other], now=2), [None])
        # Even an overlapping box must pass the strict visual gate after a gap.
        self.tracker.reset()
        self.update([RED_BLUE], [('Alice', .76)], now=1)
        changed = (RED_BLUE[0], *GREEN_BLACK[1:])
        self.assertEqual(self.update([changed], now=2), [None])
        self.tracker.reset()
        self.update([RED_BLUE], [('Alice', .76)], now=1)
        distant = ((350, 60, 430, 260), *RED_BLUE[1:])
        self.assertEqual(self.update([distant], now=2), [None])

    def test_competing_lookalikes_stay_unknown(self):
        self.update([RED_BLUE, ((180, 60, 260, 260), *RED_BLUE[1:])],
                    [('Alice', .76), ('Bob', .74)], now=1)
        middle = ((104, 60, 184, 260), *RED_BLUE[1:])
        self.assertEqual(self.update([middle], now=2), [None])

    def test_overlapping_people_are_not_misidentified(self):
        left = ((40, 60, 120, 260), *RED_BLUE[1:])
        right = ((180, 60, 260, 260), *GREEN_BLACK[1:])
        self.update([left, right], [('Alice', .76), ('Bob', .74)], now=1)
        # With two heavily overlapping people the appearance is obscured;
        # Unknown is safer than swapping their names.
        alice = ((110, 60, 190, 260), *RED_BLUE[1:])
        bob = ((120, 60, 200, 260), *GREEN_BLACK[1:])
        names = [x[0] if x else None for x in self.update([bob, alice], now=1.2)]
        self.assertIn(names[0], (None, 'Bob'))
        self.assertIn(names[1], (None, 'Alice'))

    def test_tracks_are_scoped_to_each_camera(self):
        self.update([RED_BLUE], [('Alice', .76)], now=1)
        other_camera = PersonTracker(max_gap=2)
        frame, boxes = scene([RED_BLUE])
        self.assertEqual(other_camera.update(frame, boxes, None, 1.2), [None])

    def test_identity_expires_and_face_can_correct_it(self):
        self.update([RED_BLUE], [('Alice', .76)], now=1)
        self.assertEqual(self.update([RED_BLUE], [('Bob', .91)], now=1.3),
                         [('Bob', .91, 'face')])
        self.assertEqual(self.update([RED_BLUE], now=12), [None])

    def test_no_reconnect_after_max_gap_or_reset(self):
        self.update([RED_BLUE], [('Alice', .76)], now=1)
        self.assertEqual(self.update([RED_BLUE], now=4), [None])
        self.update([RED_BLUE], [('Alice', .76)], now=4.2)
        self.tracker.reset()
        self.assertEqual(self.update([RED_BLUE], now=4.4), [None])

    def test_invalid_face_results_fail_loudly(self):
        with self.assertRaises(ValueError):
            self.update([RED_BLUE], [], now=1)


if __name__ == '__main__':
    unittest.main()
