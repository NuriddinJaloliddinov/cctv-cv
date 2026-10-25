"""The per-person phone crop pass maps boxes back to frame coordinates."""
import unittest

import numpy as np

from backend.detector import Detector


class PhoneCropTests(unittest.TestCase):
    def _detector(self, max_people=2):
        # Build the object without loading ONNX; only the crop logic is tested.
        detector = object.__new__(Detector)
        detector.phone_crop_conf = 0.15
        detector.phone_crop_max_people = max_people
        self.calls = []

        def fake_detect(crop, class_id, conf):
            self.calls.append((crop.shape[:2], class_id, conf))
            return [(10, 20, 50, 90, 0.8)]

        detector._detect_class = fake_detect
        return detector

    def test_crops_are_mapped_back_and_capped_to_the_largest_people(self):
        frame = np.zeros((1000, 1000, 3), dtype=np.uint8)
        people = [
            (100, 100, 300, 600, 0.9),   # 200 x 500
            (10, 10, 50, 100, 0.9),      # small background person
            (400, 400, 700, 900, 0.9),   # 300 x 500
        ]

        found = self._detector(max_people=2).detect_phone_crops(frame, people)

        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(call[1] == 67 for call in self.calls))
        self.assertTrue(all(call[2] == 0.15 for call in self.calls))
        # Largest person first: the 300 x 500 box starts at (400, 400). With
        # 15% side and 5% top padding and a crop that reaches 95% of the
        # person's height, the crop starts at (355, 375), so the fake box
        # becomes (365, 395, 405, 465).
        # The 200 x 500 box starts at (100, 100); its crop starts at (70, 75)
        # and the fake box becomes (80, 95, 120, 165).
        self.assertEqual(found[0], (365, 395, 405, 465, 0.8))
        self.assertEqual(found[1], (80, 95, 120, 165, 0.8))

    def test_no_people_means_no_inference(self):
        frame = np.zeros((200, 200, 3), dtype=np.uint8)
        self.assertEqual(self._detector().detect_phone_crops(frame, []), [])
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
