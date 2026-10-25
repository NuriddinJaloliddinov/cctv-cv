"""Inference resolution stays independent of the streamed frame size."""
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from backend import pipeline

RED = (0, 0, 220)


class FakeSource:
    def __init__(self, url):
        self.frame = np.zeros((480, 640, 3), dtype=np.uint8)
        self.seq = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def start(self):
        def feed():
            while not self.stop_event.wait(.03):
                with self.lock:
                    self.seq += 1

        self.thread = threading.Thread(target=feed, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)

    def latest(self):
        with self.lock:
            return self.seq, self.frame if self.seq else None

    def status(self):
        return {'connected': not self.stop_event.is_set(), 'source_fps': 25}


class FakeDetector:
    def detect(self, frame):
        # Centre of the 640-wide inference frame.
        return [(320, 120, 400, 460, .9)]


class DetectWidthTests(unittest.TestCase):
    def test_boxes_are_drawn_in_the_stream_coordinate_space(self):
        with (patch.object(pipeline, 'FrameSource', FakeSource),
              patch.object(pipeline, 'get_detector', return_value=FakeDetector()),
              patch.object(pipeline.review, 'maybe_capture'),
              patch.object(pipeline.face_review, 'effective', return_value={'enabled': False}),
              patch.object(pipeline, 'identify', return_value=[None]),
              patch.object(pipeline, 'DETECT_INTERVAL', .05),
              patch.object(pipeline, 'STREAM_MAX_WIDTH', 320),
              patch.object(pipeline, 'DETECT_MAX_WIDTH', 0)):
            p = pipeline.Pipeline('synthetic', 'synthetic', 'unused', 'cpu', 10)
            p.start()
            try:
                deadline = time.monotonic() + 3
                frame = None
                while time.monotonic() < deadline:
                    frame = p.get_latest_frame()
                    with p._box_lock:
                        detected = bool(p._boxes)
                    if detected and frame is not None and frame.shape[:2] == (240, 320):
                        break
                    time.sleep(.02)
                else:
                    self.fail('pipeline did not publish a downscaled frame')

                with p._box_lock:
                    self.assertEqual(p._box_shape, (480, 640))
                self.assertEqual(frame.shape[:2], (240, 320))
                # (320, 120, 400, 460) at half scale is (160, 60, 200, 230):
                # the drawn border must land there, not at the inference pixel.
                self.assertEqual(tuple(frame[60, 160]), RED)
                self.assertEqual(tuple(frame[60, 5]), (0, 0, 0))
            finally:
                p.stop()


if __name__ == '__main__':
    unittest.main()
