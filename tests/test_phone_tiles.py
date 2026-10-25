"""The tiled phone scan maps boxes back and bounds its own work."""
import unittest

import numpy as np

from backend.detector import Detector


def tile_detector(grid=2, tile_max=2, conf=0.2, overlap=0.15):
    """Build a Detector without loading ONNX; only tile selection is tested."""
    detector = object.__new__(Detector)
    detector.phone_tile_grid = grid
    detector.phone_tile_max = tile_max
    detector.phone_tile_overlap = overlap
    detector.phone_tile_conf = conf
    calls = []

    def fake_detect(crop, class_id, confidence):
        calls.append((crop.shape[:2], class_id, confidence))
        return [(5, 5, 25, 40, 0.7)]

    detector._detect_class = fake_detect
    return detector, calls


class PhoneTileTests(unittest.TestCase):
    def test_scans_only_person_tiles_and_maps_boxes_back(self):
        detector, calls = tile_detector()
        frame = np.zeros((400, 400, 3), dtype=np.uint8)

        found = detector.detect_phone_tiles(frame, [(10, 10, 60, 120, 0.9)])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:], (67, 0.2))
        # Cell (0, 0) padded by 15% of 200 px, clipped at the frame edge, so
        # the crop starts at (0, 0) and the reported box is unchanged.
        self.assertEqual(found, [(5, 5, 25, 40, 0.7)])

    def test_edge_tile_is_clipped_to_the_frame(self):
        detector, _ = tile_detector()
        frame = np.zeros((400, 400, 3), dtype=np.uint8)

        found = detector.detect_phone_tiles(frame, [(330, 330, 380, 395, 0.9)])

        # Cell (1, 1) is padded but cannot extend past the frame, so its crop
        # starts at (170, 170) and the fake box is offset by that.
        self.assertEqual(found, [(175, 175, 195, 210, 0.7)])

    def test_crowded_tiles_come_first_and_the_scan_is_capped(self):
        frame = np.zeros((400, 400, 3), dtype=np.uint8)
        people = [
            (10, 10, 60, 120, 0.9),      # cell (0, 0)
            (80, 20, 130, 130, 0.9),     # cell (0, 0)
            (240, 240, 290, 330, 0.9),   # cell (1, 1)
        ]
        detector, calls = tile_detector(tile_max=1)

        found = detector.detect_phone_tiles(frame, people)

        self.assertEqual(len(calls), 1)
        # The two-person cell wins, and its crop starts at (0, 0).
        self.assertEqual(found, [(5, 5, 25, 40, 0.7)])

        detector, calls = tile_detector(tile_max=2)
        detector.detect_phone_tiles(frame, people)
        self.assertEqual(len(calls), 2)

    def test_one_person_filling_the_cell_is_left_to_the_crop_pass(self):
        detector, calls = tile_detector()
        frame = np.zeros((400, 400, 3), dtype=np.uint8)

        # 160 x 160 = 25600 px, over 60% of the 200 x 200 cell.
        self.assertEqual(
            detector.detect_phone_tiles(frame, [(20, 20, 180, 180, 0.9)]), [])
        self.assertEqual(calls, [])

    def test_tiling_can_be_disabled(self):
        detector, calls = tile_detector(grid=0)
        frame = np.zeros((400, 400, 3), dtype=np.uint8)

        self.assertEqual(
            detector.detect_phone_tiles(frame, [(10, 10, 60, 120, 0.9)]), [])
        self.assertEqual(calls, [])

    def test_no_people_means_no_inference(self):
        detector, calls = tile_detector()
        frame = np.zeros((400, 400, 3), dtype=np.uint8)

        self.assertEqual(detector.detect_phone_tiles(frame, []), [])
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
