"""Unified video source: RTSP, MJPEG-over-HTTP, or plain HTTP/file."""
import os
import time
from typing import Iterator

import cv2
import numpy as np
import requests

# Force FFmpeg to use TCP for RTSP (fixes many black-screen and packet-drop issues)
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"


def open_stream(url: str) -> Iterator[np.ndarray]:
    if url.startswith("rtsp://"):
        yield from _opencv_rtsp(url)
    elif url.startswith("http"):
        if _is_mjpeg(url):
            yield from _mjpeg(url)
        else:
            yield from _opencv(url)
    else:
        yield from _opencv(url)


def _is_mjpeg(url: str) -> bool:
    try:
        head = requests.head(url, timeout=5, allow_redirects=True)
        return "multipart/x-mixed-replace" in head.headers.get("Content-Type", "")
    except Exception:
        return False


def _mjpeg(url: str) -> Iterator[np.ndarray]:
    r = requests.get(url, stream=True, timeout=10)
    buf = b""
    for chunk in r.iter_content(chunk_size=8192):
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            end = buf.find(b"\xff\xd9", start + 2) if start != -1 else -1
            if start == -1 or end == -1:
                break
            frame = cv2.imdecode(
                np.frombuffer(buf[start:end + 2], np.uint8), cv2.IMREAD_COLOR
            )
            buf = buf[end + 2:]
            if frame is not None:
                yield frame


def _opencv_rtsp(url: str) -> Iterator[np.ndarray]:
    """Loops forever, reconnecting if the stream drops (e.g., H.265 packet loss)."""
    while True:
        print(f"[source] Connecting to RTSP: {url}")
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        
        if not cap.isOpened():
            print(f"[source] ERROR: Could not open stream. Retrying in 5s...")
            time.sleep(5)
            continue

        fail_count = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    fail_count += 1
                    if fail_count > 10:  # 10 consecutive fails = stream is dead
                        print(f"[source] Stream disconnected. Reconnecting in 3s...")
                        break
                    time.sleep(0.1)
                    continue
                
                fail_count = 0  # reset on successful read
                yield frame
        finally:
            cap.release()
        
        time.sleep(3)  # Wait before reconnecting


def _opencv(url: str) -> Iterator[np.ndarray]:
    print(f"[source] Opening stream: {url}")
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print(f"[source] ERROR: Could not open stream.")
        return
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print(f"[source] Stream disconnected.")
                break
            yield frame
    finally:
        cap.release()