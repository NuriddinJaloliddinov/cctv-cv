"""Customer tagging: camera flag, summaries, APIs, and the Telegram toggle."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import db, main, telegram_bot
from backend.main import app

# Authentication is covered by tests/test_auth.py.
app.state.auth_disabled = True


def _event(event_id, kind="entered", customer=False, name="Unknown"):
    return db.create_event(
        event_id, "cam", "Door", 1, name, kind, .9, None, "caption",
        customer=customer)


class CustomerDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / "test.db")
        db.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self.old_path

    def test_camera_flag_persists_and_clears_without_a_crossing_role(self):
        client = TestClient(app)
        camera = db.create_camera({"name": "Door", "address": "rtsp://door"})
        path = f"/api/cameras/{camera.id}"

        changed = client.put(path, json={
            "role": "entrance", "tripwire": [.1, .5, .9, .5],
            "tripwire_direction": "positive_to_negative",
            "count_customer": True})
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["count_customer"])
        self.assertTrue(db.get_camera(camera.id).count_customer)

        cleared = client.put(path, json={"role": None})
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.json()["count_customer"])
        self.assertFalse(db.get_camera(camera.id).count_customer)

        db.update_camera(camera.id, {
            "role": "entrance", "tripwire": [.1, .5, .9, .5],
            "tripwire_direction": "positive_to_negative",
            "count_customer": True})
        self.assertTrue(db.get_camera(camera.id).count_customer)
        db.update_camera(camera.id, {"role": None})
        self.assertFalse(db.get_camera(camera.id).count_customer)

    def test_customer_summary_counts_confirmed_and_pending_crossings(self):
        _event("pending-in", "entered", customer=True)
        _event("confirmed-in", "entered", customer=True)
        db.resolve_event_if_pending("confirmed-in", "confirmed", "Admin", "1")
        _event("confirmed-out", "left", customer=True)
        db.resolve_event_if_pending("confirmed-out", "confirmed", "Admin", "1")
        _event("rejected-in", "entered", customer=True)
        db.resolve_event_if_pending("rejected-in", "rejected", "Admin", "1")
        _event("plain-in", "entered", customer=False)
        db.resolve_event_if_pending("plain-in", "confirmed", "Admin", "1")

        summary = db.customer_summary()
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["confirmed"], 2)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["today_entrances"], 1)
        self.assertEqual(summary["today_exits"], 1)
        self.assertEqual(summary["today_pending"], 1)
        self.assertEqual(summary["counts"]["pending"], 1)

        tagged = db.list_events(None, customer=True)
        self.assertEqual({event["id"] for event in tagged},
                         {"pending-in", "confirmed-in", "confirmed-out",
                          "rejected-in"})
        self.assertEqual(db.count_events(customer=True), 4)
        self.assertEqual(db.event_counts(customer=True)["pending"], 1)

    def test_only_pending_crossings_can_change_customer_status(self):
        _event("pending", "entered", customer=False)
        updated = db.set_event_customer("pending", True)
        self.assertEqual(updated["customer"], 1)

        _event("done", "entered", customer=False)
        db.resolve_event_if_pending("done", "confirmed", "Admin", "1")
        self.assertIsNone(db.set_event_customer("done", True))

        _event("phone", "looking", customer=False)
        self.assertIsNone(db.set_event_customer("phone", True))

    def test_overview_and_customers_apis_expose_unconfirmed_events(self):
        client = TestClient(app)
        _event("pending-in", "entered", customer=True, name="Alice")
        _event("pending-out", "left", customer=False, name="Bob")

        customers = client.get("/api/customers").json()
        self.assertEqual([event["id"] for event in customers["events"]],
                         ["pending-in"])
        self.assertTrue(customers["events"][0]["customer"])
        self.assertEqual(customers["summary"]["pending"], 1)
        self.assertEqual(customers["counts"]["pending"], 1)

        pending = client.get("/api/customers?status=pending").json()
        self.assertEqual([event["id"] for event in pending["events"]],
                         ["pending-in"])
        confirmed = client.get("/api/customers?status=confirmed").json()
        self.assertEqual(confirmed["events"], [])

        overview = client.get("/api/overview").json()
        self.assertEqual(overview["customers"]["pending"], 1)
        self.assertEqual(overview["events"]["pending"], 2)
        self.assertEqual({event["id"] for event in overview["pending_events"]},
                         {"pending-in", "pending-out"})
        self.assertEqual([event["id"] for event in overview["recent_customers"]],
                         ["pending-in"])

        bad = client.get("/api/customers?status=nope")
        self.assertEqual(bad.status_code, 400)


class CustomerTelegramTests(unittest.TestCase):
    def setUp(self):
        self.service = telegram_bot.TelegramService()
        self.service._admin_cache.clear()
        self.calls = []
        self.event = {
            "id": "e1", "status": "pending", "kind": "entered",
            "caption": "Alice entered\n🛍 Customer: <b>No</b>",
            "telegram_message_id": 10, "telegram_chat_id": "-100",
            "person_name": "Alice", "customer": 0,
        }

    def _call(self, token, method, params=None, files=None, timeout=20):
        self.calls.append((method, params or {}))
        if method == "getChatMember":
            return {"status": "administrator"}
        return None

    def test_parse_customer_callback(self):
        self.assertEqual(telegram_bot.parse_callback("pc:e1"),
                         {"type": "customer", "event_id": "e1"})

    def test_toggle_adds_and_removes_the_customer_line(self):
        with patch.object(telegram_bot.db, "get_event",
                          return_value=dict(self.event)), \
             patch.object(telegram_bot.db, "set_event_customer",
                          return_value={**self.event, "customer": 1}) as update, \
             patch.object(self.service, "_call", side_effect=self._call):
            self.service._handle_update("token", "-100", {
                "callback_query": {
                    "id": "cb1", "data": "pc:e1",
                    "from": {"id": 7, "first_name": "Ann"},
                    "message": {"message_id": 10, "chat": {"id": -100}},
                }})

        update.assert_called_once_with("e1", True)
        edit = [params for method, params in self.calls
                if method == "editMessageCaption"][-1]
        self.assertIn("🛍 Customer: <b>Yes</b>", edit["caption"])
        self.assertNotIn("<b>No</b>", edit["caption"])
        keyboard = json.loads(edit["reply_markup"])["inline_keyboard"]
        self.assertIn("pc:e1",
                      [button["callback_data"] for row in keyboard
                       for button in row])
        self.assertTrue(any(method == "answerCallbackQuery"
                            for method, _ in self.calls))

    def test_toggle_removes_customer_from_a_marked_event(self):
        marked = {**self.event, "customer": 1,
                  "caption": "Alice entered\n🛍 Customer: <b>Yes</b>"}
        with patch.object(telegram_bot.db, "get_event",
                          return_value=dict(marked)), \
             patch.object(telegram_bot.db, "set_event_customer",
                          return_value={**marked, "customer": 0}) as update, \
             patch.object(self.service, "_call", side_effect=self._call):
            self.service._handle_update("token", "-100", {
                "callback_query": {
                    "id": "cb2", "data": "pc:e1",
                    "from": {"id": 7, "first_name": "Ann"},
                    "message": {"message_id": 10, "chat": {"id": -100}},
                }})

        update.assert_called_once_with("e1", False)
        edit = [params for method, params in self.calls
                if method == "editMessageCaption"][-1]
        self.assertIn("🛍 Customer: <b>No</b>", edit["caption"])


if __name__ == "__main__":
    unittest.main()
