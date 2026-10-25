"""Unified video source: RTSP, MJPEG-over-HTTP, or plain HTTP/file.

The reader always drains the stream as fast as it arrives and keeps only the
newest frame. Consuming one frame per processed frame (the old behaviour)
lets the decoder buffer grow without bound whenever inference is slower than
the camera, which is what makes a live feed drift seconds behind real time.
"""
import os
import threading
import time

import cv2
import numpy as np
import requests

from .config import RTSP_LOW_LATENCY, RTSP_TRANSPORT


def _ffmpeg_options() -> str:
    """Build OPENCV_FFMPEG_CAPTURE_OPTIONS for a low-delay live stream."""
    pairs: list[tuple[str, str]] = []
    if RTSP_TRANSPORT in ("tcp", "udp"):
        pairs.append(("rtsp_transport", RTSP_TRANSPORT))
    if RTSP_LOW_LATENCY:
        pairs.extend([
            ("fflags", "nobuffer"),      # don't fill the demuxer queue
            ("flags", "low_delay"),      # decode without frame reordering delay
            ("max_delay", "500000"),     # cap demux delay at 0.5 s (µs)
            ("stimeout", "5000000"),     # fail a dead RTSP socket after 5 s
        ])
    return "|".join(f"{key};{value}" for key, value in pairs)


# Respect an explicitly configured value, otherwise use our low-delay defaults.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", _ffmpeg_options())


class FrameSource:
    """Continuously read a stream and expose only its newest frame.

    ``latest()`` is cheap and thread-safe. The sequence number lets a consumer
    tell whether it has already handled the current frame.
    """

    def __init__(self, url: str, reconnect_delay: float = 2.0):
        self.url = url
        self.reconnect_delay = reconnect_delay
        self.connected = False
        self.frames_read = 0
        self.source_fps = 0.0

        self._frame: np.ndarray | None = None
        self._seq = 0
        self._frame_ts = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cap: cv2.VideoCapture | None = None

        self._fps_started = time.monotonic()
        self._fps_count = 0

    # ---------- lifecycle ----------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"source-{self._seq}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()          # unblocks a pending read()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)

    # ---------- consumer API ----------

    def latest(self) -> tuple[int, np.ndarray | None]:
        """Return ``(sequence, frame)``; frame is None before the first read."""
        with self._lock:
            return self._seq, self._frame

    def status(self) -> dict:
        with self._lock:
            has_frame = self._frame is not None
            age_ms = (time.monotonic() - self._frame_ts) * 1000 if has_frame else None
            seq = self._seq
        return {
            "connected": self.connected,
            "source_fps": round(self.source_fps, 1),
            "frames_read": self.frames_read,
            "frame_seq": seq,
            "capture_age_ms": round(age_ms, 1) if age_ms is not None else None,
        }

    # ---------- internals ----------

    def _publish(self, frame: np.ndarray) -> None:
        now = time.monotonic()
        with self._lock:
            self._frame = frame
            self._seq += 1
            self._frame_ts = now
            self.frames_read += 1
            self._fps_count += 1
            elapsed = now - self._fps_started
            if elapsed >= 1.0:
                self.source_fps = self._fps_count / elapsed
                self._fps_started = now
                self._fps_count = 0

    def _run(self) -> None:
        retry = self.url.startswith(("rtsp://", "http://", "https://"))
        while not self._stop.is_set():
            try:
                if self.url.startswith("rtsp://"):
                    self._read_opencv()
                elif self.url.startswith("http") and _is_mjpeg(self.url):
                    self._read_mjpeg()
                else:
                    self._read_opencv()
            except Exception as e:
                print(f"[source] {self.url}: {e}")

            self.connected = False
            if self._stop.is_set() or not retry:
                break
            print(f"[source] Reconnecting in {self.reconnect_delay:.0f}s: {self.url}")
            if self._stop.wait(self.reconnect_delay):
                break

    def _read_opencv(self) -> None:
        print(f"[source] Opening stream: {self.url}")
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            print("[source] ERROR: could not open stream")
            return

        # Honoured by some backends; FFmpeg usually ignores it, but the
        # continuous drain below is what actually prevents a backlog.
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        with self._lock:
            self._cap = cap
        self.connected = True
        fails = 0
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails > 10:
                        print("[source] Stream disconnected")
                        return
                    time.sleep(0.05)
                    continue
                fails = 0
                self._publish(frame)
        finally:
            with self._lock:
                if self._cap is cap:
                    self._cap = None
            cap.release()

    def _read_mjpeg(self) -> None:
        print(f"[source] Opening MJPEG stream: {self.url}")
        r = requests.get(self.url, stream=True, timeout=(5, 10))
        r.raise_for_status()
        self.connected = True
        buf = b""
        try:
            for chunk in r.iter_content(chunk_size=16384):
                if self._stop.is_set():
                    break
                buf += chunk
                newest = None
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start == -1:
                        # Keep a possible split marker, discard stale bytes.
                        if len(buf) > 1 << 20:
                            buf = buf[-2:]
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        buf = buf[start:]
                        break
                    newest = buf[start:end + 2]
                    buf = buf[end + 2:]

                # Decode only the newest complete JPEG, so a slow consumer
                # cannot make the feed progressively older.
                if newest is not None:
                    frame = cv2.imdecode(
                        np.frombuffer(newest, np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        self._publish(frame)
        finally:
            r.close()


def _is_mjpeg(url: str) -> bool:
    try:
        head = requests.head(url, timeout=5, allow_redirects=True)
        return "multipart/x-mixed-replace" in head.headers.get("Content-Type", "")
    except Exception:
        return False
