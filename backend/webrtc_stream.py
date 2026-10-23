"""WebRTC frame track backed by pipeline.latest_frame."""
import av
from aiortc import VideoStreamTrack


class FrameTrack(VideoStreamTrack):
    """Pushes the latest annotated frame to the browser."""

    def __init__(self, pipeline):
        super().__init__()
        self.pipeline = pipeline

    async def recv(self) -> av.VideoFrame:
        pts, time_base = await self.next_timestamp()
        frame = self.pipeline.latest_frame
        if frame is None:
            img = av.VideoFrame(640, 480, "rgb24")
        else:
            img = av.VideoFrame.from_ndarray(frame, format="bgr24")
        img.pts = pts
        img.time_base = time_base
        return img