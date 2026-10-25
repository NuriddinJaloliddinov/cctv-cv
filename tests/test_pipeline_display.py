"""Overlay boxes are mapped from the inference frame to the output frame."""
import unittest

from backend import pipeline


class DisplayScalingTests(unittest.TestCase):
    def test_boxes_scale_when_inference_is_larger(self):
        boxes, identities, phone_use, phone_boxes, active = (
            pipeline._inference_to_display(
                (540, 960), (1080, 1920),
                [(100, 200, 300, 500, .9)],
                [("Alice", .8, "face")],
                ["looking"],
                [(150, 250, 200, 300, .6)],
                [(150, 250, 200, 300, .6)]))

        self.assertEqual(boxes, [(50, 100, 150, 250, .9)])
        self.assertEqual(identities, [("Alice", .8, "face")])
        self.assertEqual(phone_use, ["looking"])
        self.assertEqual(phone_boxes, [(75, 125, 100, 150, .6)])
        self.assertEqual(active, [(75, 125, 100, 150, .6)])

    def test_equal_shapes_pass_through_unchanged(self):
        boxes = [(1, 2, 3, 4, .5)]

        self.assertEqual(
            pipeline._inference_to_display(
                (10, 20), (10, 20), boxes, ["i"], [None], [], [None]),
            (boxes, ["i"], [None], [], [None]))

    def test_missing_phone_box_stays_missing(self):
        _, _, _, _, active = pipeline._inference_to_display(
            (240, 480), (480, 960), [], [], [], [], [None])

        self.assertEqual(active, [None])

    def test_changed_aspect_drops_stale_boxes(self):
        # 16:9 inference frame, 4:3 output frame: the boxes no longer line up.
        self.assertEqual(
            pipeline._inference_to_display(
                (480, 640), (1080, 1920), [(0, 0, 10, 10, .5)], ["i"],
                [None], [], [None]),
            ([], [], [], [], []))

    def test_no_boxes_yet_means_no_overlay(self):
        self.assertEqual(
            pipeline._inference_to_display(
                (480, 640), None, [(0, 0, 10, 10, .5)], ["i"], [None], [],
                [None]),
            ([], [], [], [], []))


if __name__ == '__main__':
    unittest.main()
