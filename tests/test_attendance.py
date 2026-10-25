"""Attendance records, unknown crossings, and the Telegram person picker."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from backend import alerts, db, main, telegram_bot

# Authentication is covered by tests/test_auth.py; these tests exercise other
# endpoints directly.
main.app.state.auth_disabled = True


def _event(event_id, kind="entered", name="Unknown", person_id=None,
           camera="Door"):
    return db.create_event(
        event_id, "cam", camera, 1, name, kind, .9, None, "caption",
        recognized_person_id=person_id,
        recognized_person_name=name)


class AttendanceDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / "test.db")
        db.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self.old_path

    def test_confirm_entrance_and_exit_updates_status_and_today_counts(self):
        db.create_person("alice", "Alice", "", None)
        _event("in", "entered", "Alice", "alice")
        result = db.resolve_event_if_pending("in", "confirmed", "Admin", "1")
        self.assertEqual(result["_attendance"]["person_id"], "alice")
        self.assertEqual(result["_attendance"]["kind"], "entered")

        overview = db.attendance_overview()
        alice = overview["people"][0]
        self.assertEqual(alice["status"], "inside")
        self.assertEqual(alice["today_entrances"], 1)
        self.assertEqual(overview["summary"]["inside"], 1)
        self.assertEqual(overview["summary"]["today_entrances"], 1)

        _event("out", "left", "Alice", "alice")
        db.resolve_event_if_pending("out", "confirmed", "Admin", "1")
        overview = db.attendance_overview()
        alice = overview["people"][0]
        self.assertEqual(alice["status"], "outside")
        self.assertEqual(alice["today_exits"], 1)
        self.assertEqual(overview["summary"]["inside"], 0)
        self.assertEqual(len(overview["recent"]), 2)

    def test_unknown_crossing_is_saved_but_not_counted(self):
        db.create_person("alice", "Alice", "", None)
        event = _event("unknown", "entered")
        result = db.resolve_event_if_pending(
            "unknown", "confirmed", "Admin", "1")
        self.assertNotIn("_attendance", result)
        overview = db.attendance_overview()
        self.assertEqual(overview["people"][0]["status"], "no_record")
        self.assertEqual([e["id"] for e in overview["unknown_events"]],
                         ["unknown"])
        self.assertEqual(overview["recent"], [])
        self.assertEqual(db.get_event("unknown")["status"], "confirmed")

    def test_admin_specification_records_the_chosen_person(self):
        db.create_person("alice", "Alice", "", None)
        db.create_person("bob", "Bob", "", None)
        _event("e", "left", "Unknown")
        updated = db.specify_event_person("e", "bob", "Bob")
        self.assertEqual(updated["person_name"], "Bob")
        self.assertEqual(updated["specified_person_id"], "bob")
        resolved = db.resolve_event_if_pending(
            "e", "confirmed", "Admin", "1")
        self.assertEqual(resolved["_attendance"]["person_id"], "bob")
        self.assertEqual(resolved["_attendance"]["kind"], "left")
        overview = db.attendance_overview()
        bob = next(p for p in overview["people"] if p["id"] == "bob")
        self.assertEqual(bob["status"], "outside")
        self.assertEqual(overview["unknown_events"], [])

    def test_clearing_selection_restores_the_automatic_name(self):
        db.create_person("alice", "Alice", "", None)
        _event("e", "entered", "Alice", "alice")
        db.specify_event_person("e", "alice", "Alice")
        cleared = db.clear_event_person("e")
        self.assertIsNone(cleared["specified_person_id"])
        self.assertEqual(cleared["person_name"], "Alice")

    def test_phone_events_and_duplicate_resolution_never_touch_attendance(self):
        db.create_person("alice", "Alice", "", None)
        _event("phone", "looking", "Alice", "alice")
        result = db.resolve_event_if_pending(
            "phone", "confirmed", "Admin", "1")
        self.assertNotIn("_attendance", result)
        self.assertEqual(db.attendance_overview()["recent"], [])

        _event("in", "entered", "Alice", "alice")
        db.resolve_event_if_pending("in", "confirmed", "Admin", "1")
        self.assertIsNone(db.resolve_event_if_pending(
            "in", "confirmed", "Admin", "1"))
        self.assertEqual(len(db.attendance_overview()["recent"]), 1)

    def test_person_prefix_requires_a_unique_match(self):
        db.create_person("abcd0000000000000000000000000000", "A", "", None)
        db.create_person("abcd1111111111111111111111111111", "B", "", None)
        self.assertIsNone(db.find_person_by_prefix("abcd"))
        self.assertEqual(
            db.find_person_by_prefix("abcd1")["name"], "B")
        self.assertIsNone(db.find_person_by_prefix("a"))
        self.assertIsNone(db.find_person_by_prefix("xyz!"))


class CrossingAttendanceTests(unittest.TestCase):
    def test_crossing_alert_links_a_unique_enrolled_name(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "test.db")), \
             patch.object(alerts, "EVENTS_DIR", Path(folder)), \
             patch.object(alerts.telegram_bot.service, "is_configured",
                          return_value=True), \
             patch.object(alerts.telegram_bot.service, "notify"):
            db.init_db()
            db.create_person("alice", "Alice", "", None)
            event = alerts.report_crossing(
                "cam", "Door", 3, "Alice", "entrance", .9,
                np.zeros((200, 200, 3), np.uint8), (40, 40, 140, 180),
                (.1, .8, .9, .8), "positive_to_negative")
            self.assertEqual(event["recognized_person_id"], "alice")
            self.assertEqual(event["kind"], "entered")
            resolved = db.resolve_event_if_pending(
                event["id"], "confirmed", "Admin", "1")
            self.assertEqual(resolved["_attendance"]["person_id"], "alice")

            unknown = alerts.report_crossing(
                "cam", "Door", 4, "Unknown", "exit", .8,
                np.zeros((200, 200, 3), np.uint8), (40, 40, 140, 180),
                (.1, .8, .9, .8), "negative_to_positive")
            self.assertIsNone(unknown["recognized_person_id"])
            resolved = db.resolve_event_if_pending(
                unknown["id"], "confirmed", "Admin", "1")
            self.assertNotIn("_attendance", resolved)


class AttendanceApiTests(unittest.TestCase):
    def test_api_returns_people_summary_and_unassigned_events(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(db, "DB_PATH", str(Path(folder) / "db.sqlite")):
            db.init_db()
            db.create_person("alice", "Alice", "", None)
            _event("in", "entered", "Alice", "alice")
            db.resolve_event_if_pending("in", "confirmed", "Admin", "1")
            _event("lost", "left", "Unknown")
            db.resolve_event_if_pending("lost", "confirmed", "Admin", "1")
            response = TestClient(main.app).get("/api/attendance")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["summary"]["inside"], 1)
            self.assertEqual(data["people"][0]["name"], "Alice")
            self.assertTrue(data["people"][0]["last_entrance"])
            self.assertEqual(data["unknown_events"][0]["id"], "lost")
            self.assertEqual(data["recent"][0]["event_id"], "in")


class AttendanceTelegramTests(unittest.TestCase):
    def setUp(self):
        self.service = telegram_bot.TelegramService()
        self.service._admin_cache.clear()
        self.calls = []
        self.event = {
            "id": "e1", "status": "pending", "kind": "entered",
            "caption": "Unknown entered", "telegram_message_id": 10,
            "telegram_chat_id": "-100", "person_name": "Unknown",
            "recognized_person_name": "Unknown", "specified_person_id": None,
            "specified_person_name": None,
        }

    def _call(self, token, method, params=None, files=None, timeout=20):
        self.calls.append((method, params or {}))
        if method == "getChatMember":
            return {"status": "administrator"}
        return None

    def _update(self, data):
        return {"callback_query": {
            "id": "cb1", "data": data,
            "from": {"id": 7, "first_name": "Ann"},
            "message": {"message_id": 10, "chat": {"id": -100}},
        }}

    def _run(self, data, **patches):
        defaults = {
            "get_event": {"return_value": dict(self.event)},
            "specify_event_person": {"return_value": dict(self.event)},
            "clear_event_person": {"return_value": dict(self.event)},
            "set_event_customer": {"return_value": dict(self.event)},
            "list_persons": {"return_value": []},
        }
        defaults.update(patches)
        managers = [patch.object(telegram_bot.db, name, **kwargs)
                    for name, kwargs in defaults.items()]
        managers.append(patch.object(self.service, "_call",
                                     side_effect=self._call))
        with ExitStack() as stack:
            for manager in managers:
                stack.enter_context(manager)
            self.service._handle_update("token", "-100", self._update(data))

    def test_parse_attendance_callbacks(self):
        self.assertEqual(
            telegram_bot.parse_callback("pi:e1:2"),
            {"type": "picker", "event_id": "e1", "page": 2})
        self.assertEqual(
            telegram_bot.parse_callback("pp:e1:abcdef12"),
            {"type": "person", "event_id": "e1",
             "person_prefix": "abcdef12"})
        self.assertEqual(
            telegram_bot.parse_callback("px:e1"),
            {"type": "clear", "event_id": "e1"})
        self.assertEqual(
            telegram_bot.parse_callback("pc:e1"),
            {"type": "customer", "event_id": "e1"})
        self.assertIsNone(telegram_bot.parse_callback("pp:e1:ZZZZ"))
        self.assertIsNone(telegram_bot.parse_callback("pi:e1:-1"))

    def test_crossing_keyboard_has_specify_and_fits_callback_limit(self):
        event = dict(self.event, id="a" * 32)
        rows = self.service._alert_keyboard(event)
        payloads = [button["callback_data"] for row in rows for button in row]
        self.assertTrue(any(data.startswith("pi:") for data in payloads))
        self.assertTrue(any(data.startswith("pc:") for data in payloads))
        self.assertTrue(all(len(data.encode()) <= 64 for data in payloads))

        phone = self.service._alert_keyboard(
            dict(event, kind="looking"))
        self.assertFalse(any("pi:" in button["callback_data"]
                             for row in phone for button in row))

    def test_picker_shows_enrolled_people_and_stays_within_callback_limit(self):
        people = [{"id": f"{index:032x}", "name": f"Person {index:02d}"}
                  for index in range(12)]
        self._run("pi:e1:1", list_persons={"return_value": people})
        edits = [params for method, params in self.calls
                 if method == "editMessageCaption"]
        self.assertTrue(edits)
        keyboard = json.loads(edits[-1]["reply_markup"])["inline_keyboard"]
        payloads = [button["callback_data"] for row in keyboard
                    for button in row]
        self.assertTrue(all(len(data.encode()) <= 64 for data in payloads))
        self.assertIn("Page 2 of 2", edits[-1]["caption"])
        self.assertTrue(any("Person 10" in button["text"]
                            for row in keyboard for button in row))

    def test_selecting_person_requires_a_confirm_click(self):
        person = {"id": "a" * 32, "name": "Alice"}
        self._run("pp:e1:" + "a" * 16,
                  find_person_by_prefix={"return_value": person},
                  specify_event_person={"return_value": {
                      **self.event, "specified_person_id": person["id"],
                      "specified_person_name": "Alice",
                      "person_name": "Alice"}})
        selected = [params for method, params in self.calls
                    if method == "editMessageCaption"][-1]
        keyboard = json.loads(selected["reply_markup"])["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["callback_data"], "pu:e1:confirm")
        self.assertIn("Selected person", selected["caption"])
        self.assertTrue(any(button["callback_data"] == "pu:e1:reject"
                            for row in keyboard for button in row))

    def test_confirmed_crossing_reports_attendance_and_unknown_does_not(self):
        attendance = {"person_name": "Alice", "kind": "entered"}
        resolved = {**self.event, "status": "confirmed",
                    "_attendance": attendance}
        with patch.object(telegram_bot.db, "get_event",
                          return_value=dict(self.event)), \
             patch.object(telegram_bot.db, "resolve_event_if_pending",
                          return_value=resolved), \
             patch.object(self.service, "_call", side_effect=self._call):
            self.service._handle_update(
                "token", "-100", self._update("pu:e1:confirm"))
        caption = [params for method, params in self.calls
                   if method == "editMessageCaption"][-1]["caption"]
        self.assertIn("Attendance recorded for Alice", caption)

        with patch.object(telegram_bot.db, "get_event",
                          return_value=dict(self.event)), \
             patch.object(telegram_bot.db, "resolve_event_if_pending",
                          return_value={**self.event,
                                        "status": "confirmed"}), \
             patch.object(self.service, "_call", side_effect=self._call):
            self.service._handle_update(
                "token", "-100", self._update("pu:e1:confirm"))
        caption = [params for method, params in self.calls
                   if method == "editMessageCaption"][-1]["caption"]
        self.assertIn("No attendance recorded", caption)


if __name__ == "__main__":
    unittest.main()
