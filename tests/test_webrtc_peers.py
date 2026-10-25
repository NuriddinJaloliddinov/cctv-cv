"""Several browsers must be able to watch the same camera at once."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import main


class FakePeer:
    """Minimal stand-in for aiortc.RTCPeerConnection."""

    instances = []
    fail_negotiation = False

    def __init__(self):
        self.connectionState = "new"
        self.localDescription = None
        self.handlers = {}
        self.closed = False
        FakePeer.instances.append(self)

    def addTrack(self, track):
        pass

    def on(self, event):
        def register(handler):
            self.handlers[event] = handler
            return handler
        return register

    async def setRemoteDescription(self, description):
        if FakePeer.fail_negotiation:
            raise RuntimeError("bad offer")

    async def createAnswer(self):
        return SimpleNamespace(sdp="answer-sdp", type="answer")

    async def setLocalDescription(self, answer):
        self.localDescription = answer

    async def close(self):
        self.closed = True
        self.connectionState = "closed"


class FakePipeline:
    fps = 10

    def get_latest_frame(self):
        return None


class MultiViewerTests(unittest.TestCase):
    def setUp(self):
        self.previous_auth_disabled = getattr(
            main.app.state, "auth_disabled", False)
        main.app.state.auth_disabled = True
        self.addCleanup(self._restore_auth)

        FakePeer.instances = []
        FakePeer.fail_negotiation = False
        main._peers.clear()
        self.addCleanup(main._peers.clear)

        self.patches = [
            patch.object(main.pipeline, "get_pipeline",
                         return_value=FakePipeline()),
            patch.object(main, "RTCPeerConnection", FakePeer),
            patch.object(main, "FrameTrack", lambda *args, **kwargs: None),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def _restore_auth(self):
        main.app.state.auth_disabled = self.previous_auth_disabled

    def offer(self):
        response = self.client.post(
            "/api/webrtc/offer/cam1",
            json={"sdp": "offer-sdp", "type": "offer"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sdp"], "answer-sdp")
        return response

    def test_second_viewer_does_not_disconnect_the_first(self):
        self.offer()
        first = FakePeer.instances[0]
        self.assertFalse(first.closed)
        self.assertEqual(len(main._peers["cam1"]), 1)
        self.assertEqual(main._peers["cam1"], {first})

        self.offer()
        second = FakePeer.instances[1]
        self.assertFalse(first.closed, "existing viewer was disconnected")
        self.assertFalse(second.closed)
        self.assertEqual(main._peers["cam1"], {first, second})

        self.offer()
        third = FakePeer.instances[2]
        self.assertFalse(first.closed)
        self.assertFalse(second.closed)
        self.assertEqual(len(main._peers["cam1"]), 3)
        self.assertEqual(main._peers["cam1"], {first, second, third})

    def test_closing_one_viewer_keeps_the_others(self):
        self.offer()
        self.offer()
        first, second = FakePeer.instances
        asyncio.run(main._close_peer("cam1", first))
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assertEqual(main._peers["cam1"], {second})

        asyncio.run(main._close_peer("cam1", second))
        self.assertTrue(second.closed)
        self.assertNotIn("cam1", main._peers)

    def test_invalid_offer_is_rejected_without_creating_a_viewer(self):
        response = self.client.post(
            "/api/webrtc/offer/cam1", json={"sdp": "", "type": "offer"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("cam1", main._peers)
        self.assertEqual(FakePeer.instances, [])

    def test_failed_negotiation_does_not_leave_a_viewer_attached(self):
        FakePeer.fail_negotiation = True
        response = self.client.post(
            "/api/webrtc/offer/cam1",
            json={"sdp": "offer-sdp", "type": "offer"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("cam1", main._peers)
        self.assertTrue(FakePeer.instances[0].closed)

    def test_stopping_the_pipeline_closes_every_viewer(self):
        self.offer()
        self.offer()
        first, second = FakePeer.instances
        response = self.client.post("/api/stop/cam1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertNotIn("cam1", main._peers)


if __name__ == "__main__":
    unittest.main()
