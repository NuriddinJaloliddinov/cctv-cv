"""WebRTC frame track backed by the pipeline's newest annotated frame."""
import asyncio
import fractions
import time

import av
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError


class FrameTrack(VideoStreamTrack):
    """Pushes the latest annotated frame to the browser at the pipeline fps.

    aiortc's default ``next_timestamp`` paces every video track at 30 fps.
    Sending duplicate frames at 30 fps wasted CPU on encoding and could add
    queueing delay, so this track paces itself at the rate the pipeline
    actually produces new frames.
    """

    def __init__(self, pipeline, fps: int = 10):
        super().__init__()
        self.pipeline = pipeline
        self._fps = max(1, min(int(fps or 10), 60))
        self._interval = 1.0 / self._fps
        self._start = time.monotonic()
        self._next = self._start
        self._last_pts = -1

    async def recv(self) -> av.VideoFrame:
        if self.readyState != "live":
            raise MediaStreamError

        now = time.monotonic()
        if now < self._next:
            await asyncio.sleep(self._next - now)
            now = time.monotonic()
        # Never burst to catch up after a stall; drop the missed slots instead.
        self._next = max(self._next + self._interval, now)

        frame = self.pipeline.get_latest_frame()
        if frame is None:
            img = av.VideoFrame(640, 480, "rgb24")
        else:
            img = av.VideoFrame.from_ndarray(frame, format="bgr24")

        pts = int((now - self._start) * 90000)
        self._last_pts = max(self._last_pts + 1, pts)
        img.pts = self._last_pts
        img.time_base = fractions.Fraction(1, 90000)
        return img
