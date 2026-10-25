"""Inference/display smoke test with a generated stream and fake detector."""
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from backend import pipeline


class FakeSource:
    def __init__(self, url):
        self.frame = np.zeros((280, 320, 3), dtype=np.uint8)
        self.frame[90:240, 60:100] = (0, 0, 220)
        self.seq = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def start(self):
        def feed():
            while not self.stop_event.wait(.04):
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
        return [(50, 40, 120, 260, .9)]


class PipelineTests(unittest.TestCase):
    def test_face_anchor_persists_and_display_keeps_publishing(self):
        calls = []

        def face_match(frame, boxes):
            calls.append(len(boxes))
            return [('Alice', .8)] if len(calls) == 1 else [None]

        with (patch.object(pipeline, 'FrameSource', FakeSource),
              patch.object(pipeline, 'get_detector', return_value=FakeDetector()),
              patch.object(pipeline.review, 'maybe_capture'),
              patch.object(pipeline.face_review, 'effective', return_value={'enabled': False}),
              patch.object(pipeline, 'identify', side_effect=face_match),
              patch.object(pipeline, 'FACE_INTERVAL', .05),
              patch.object(pipeline, 'DETECT_INTERVAL', .05)):
            p = pipeline.Pipeline('synthetic', 'synthetic', 'unused', 'cpu', 10)
            p.start()
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with p._box_lock:
                        labels = list(p._identities)
                    if len(calls) > 1 and labels and labels[0] == ('Alice', .8, 'track'):
                        break
                    time.sleep(.02)
                else:
                    self.fail('Face identity was not preserved on the track')

                time.sleep(.35)
                self.assertEqual(p.get_latest_frame().shape, (280, 320, 3))
                self.assertGreaterEqual(p._frame_count, 2)
            finally:
                p.stop()


if __name__ == '__main__':
    unittest.main()
