"""Telegram callback authorization and event persistence tests."""
import os
import tempfile
import unittest
from unittest.mock import patch

from backend import db, telegram_bot


class EventDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        db.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self.old_path

    def test_settings_round_trip_and_delete(self):
        db.set_settings({"a": "1", "b": "2"})
        self.assertEqual(db.get_setting("a"), "1")
        self.assertEqual(db.get_settings(["a"]), {"a": "1"})
        db.set_settings({"a": "3"})
        self.assertEqual(db.get_setting("a"), "3")
        db.delete_settings(["a"])
        self.assertIsNone(db.get_setting("a"))

    def test_event_is_resolved_only_once(self):
        event = db.create_event(
            "e1", "cam1", "Front door", 7, "Alice", "looking",
            0.82, None, "caption")
        self.assertEqual(event["status"], "pending")

        confirmed = db.resolve_event_if_pending(
            "e1", "confirmed", "Admin", "42")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["handled_by"], "Admin")

        rejected = db.resolve_event_if_pending(
            "e1", "rejected", "Other", "43")
        self.assertIsNone(rejected)
        self.assertEqual(db.get_event("e1")["status"], "confirmed")
        self.assertEqual(db.count_events("confirmed"), 1)
        self.assertEqual(db.event_counts()["rejected"], 0)

    def test_events_can_be_listed_by_status(self):
        for index in range(3):
            db.create_event(
                f"e{index}", "cam", "Cam", index, "Unknown", "looking",
                0.5, None, "caption")
        db.resolve_event_if_pending("e1", "confirmed", "Admin", "1")
        self.assertEqual([e["id"] for e in db.list_events("confirmed")], ["e1"])
        self.assertEqual(len(db.list_events("pending")), 2)


class TelegramCallbackTests(unittest.TestCase):
    def setUp(self):
        self.service = telegram_bot.TelegramService()
        self.service._admin_cache.clear()
        self.calls = []
        self.member_status = "administrator"
        self.event = {
            "id": "e1",
            "status": "pending",
            "caption": "<b>Phone use detected</b>",
            "telegram_message_id": 10,
            "telegram_chat_id": "-100",
        }

    def _call(self, token, method, params=None, files=None, timeout=20):
        self.calls.append((method, params or {}))
        if method == "getChatMember":
            return {"status": self.member_status}
        return None

    def _update(self, action="confirm", chat_id=-100, message_id=10,
                user_id=7):
        return {"callback_query": {
            "id": "cb1",
            "data": f"pu:e1:{action}",
            "from": {"id": user_id, "first_name": "Ann", "username": "ann"},
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        }}

    def _handle(self, update, resolve):
        with patch.object(telegram_bot.db, "get_event",
                          return_value=dict(self.event)), \
             patch.object(telegram_bot.db, "resolve_event_if_pending",
                          side_effect=resolve) as resolve_mock, \
             patch.object(self.service, "_call", side_effect=self._call):
            self.service._handle_update("token", "-100", update)
        return resolve_mock

    def test_parse_callback_data(self):
        self.assertEqual(
            telegram_bot.parse_callback_data("pu:abc:confirm"),
            ("abc", "confirm"))
        self.assertEqual(
            telegram_bot.parse_callback_data("pu:abc:reject"),
            ("abc", "reject"))
        self.assertIsNone(telegram_bot.parse_callback_data("pu:abc:delete"))
        self.assertIsNone(telegram_bot.parse_callback_data("nope"))

    def test_group_admin_can_confirm(self):
        resolved = {**self.event, "status": "confirmed"}
        mock = self._handle(self._update(), [resolved])
        mock.assert_called_once_with("e1", "confirmed", "Ann (@ann)", "7")
        methods = [method for method, _ in self.calls]
        self.assertIn("getChatMember", methods)
        self.assertIn("answerCallbackQuery", methods)
        self.assertIn("editMessageCaption", methods)

    def test_non_admin_cannot_confirm(self):
        self.member_status = "member"
        mock = self._handle(self._update(), [])
        mock.assert_not_called()
        answers = [params for method, params in self.calls
                   if method == "answerCallbackQuery"]
        self.assertTrue(answers)
        self.assertIn("administrators", answers[-1]["text"])
        self.assertEqual(answers[-1]["show_alert"], "true")

    def test_callback_from_another_chat_is_rejected(self):
        mock = self._handle(self._update(chat_id=-200), [])
        mock.assert_not_called()

    def test_double_click_does_not_change_finished_event(self):
        self.event["status"] = "confirmed"
        mock = self._handle(self._update(), [])
        mock.assert_not_called()
        answers = [params for method, params in self.calls
                   if method == "answerCallbackQuery"]
        self.assertIn("already confirmed", answers[-1]["text"])


class TelegramConnectionTests(unittest.TestCase):
    """Chat-id formats, resolution, and the bot membership check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        db.init_db()
        self.addCleanup(self._restore)
        self.service = telegram_bot.TelegramService()
        self.chat = {"id": -123456789, "type": "group", "title": "Guard room"}
        self.member_status = "administrator"
        self.chat_error = None
        self.me_error = None
        self.member_error = None

    def _restore(self):
        db.DB_PATH = self.old_path

    def _call(self, token, method, params=None, files=None, timeout=20):
        if method == "getMe":
            if self.me_error:
                raise RuntimeError(self.me_error)
            return {"id": 99, "username": "cctv_bot"}
        if method == "getChat":
            if self.chat_error:
                raise RuntimeError(self.chat_error)
            return dict(self.chat)
        if method == "getChatMember":
            if self.member_error:
                raise RuntimeError(self.member_error)
            return {"status": self.member_status}
        return None

    def _configure(self, chat_id):
        db.set_settings({telegram_bot._TOKEN_KEY: "token",
                         telegram_bot._CHAT_KEY: chat_id})

    def _verify(self):
        with patch.object(self.service, "_call", side_effect=self._call):
            return self.service.test()

    def test_basic_group_id_without_minus_100_is_accepted(self):
        self._configure("-123456789")
        result = self._verify()
        self.assertTrue(result["ok"])
        self.assertIn("Guard room", result["detail"])
        self.assertEqual(db.get_setting(telegram_bot._CHAT_KEY), "-123456789")

    def test_username_is_converted_to_the_numeric_chat_id(self):
        self.chat = {"id": -1001234567890, "type": "supergroup",
                     "username": "guardroom", "title": "Guard room"}
        self._configure("@GuardRoom")
        result = self._verify()
        self.assertTrue(result["ok"])
        self.assertEqual(db.get_setting(telegram_bot._CHAT_KEY),
                         "-1001234567890")

    def test_private_chat_id_is_rejected(self):
        self.chat = {"id": 555, "type": "private", "first_name": "Ann"}
        self._configure("555")
        result = self._verify()
        self.assertFalse(result["ok"])
        self.assertIn("private", result["detail"].lower())

    def test_bot_that_is_not_admin_gets_a_promotion_hint(self):
        self.member_status = "member"
        self._configure("-123456789")
        result = self._verify()
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "bot_not_admin")
        self.assertIn("not a group administrator", result["warning"])
        self.assertIn("promote", result["hint"].lower())

    def test_typed_values_are_checked_without_overwriting_saved_settings(self):
        self._configure("-123456789")
        self.chat = {"id": -987654321, "type": "group", "title": "Other room"}
        with patch.object(self.service, "_call", side_effect=self._call):
            result = self.service.test(chat_id="-987654321")
        self.assertTrue(result["ok"])
        self.assertEqual(result["chat_id"], "-987654321")
        # Nothing is persisted until the dashboard saves after a success.
        self.assertEqual(db.get_setting(telegram_bot._CHAT_KEY), "-123456789")

    def test_member_lookup_failure_does_not_blame_the_token(self):
        self._configure("-123456789")
        self.member_error = "Bad Request: user not found"
        result = self._verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "bot_not_admin")
        self.assertIn("administrator", result["hint"].lower())
        self.assertNotIn("token", result["hint"].lower())

    def test_chat_not_found_is_actionable(self):
        self._configure("-123456789")
        self.chat_error = "Bad Request: chat not found"
        result = self._verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "chat_not_found")
        self.assertIn("group ID", result["hint"])

    def test_unauthorized_token_is_reported_as_invalid(self):
        self._configure("-123456789")
        self.me_error = "Unauthorized"
        result = self._verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_token")
        self.assertIn("@BotFather", result["hint"])

    def test_timeout_is_explained(self):
        self._configure("-123456789")
        self.me_error = "Telegram request timed out"
        result = self._verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "timeout")
        self.assertIn("internet", result["hint"].lower())

    def test_token_is_sanitized_in_technical_details(self):
        self._configure("-123456789")
        self.chat_error = "request failed for token"
        result = self._verify()
        self.assertFalse(result["ok"])
        self.assertNotIn("token", result["error"])
        self.assertIn("***", result["error"])

    def test_unexpected_exception_becomes_a_readable_result(self):
        self._configure("-123456789")
        with patch.object(self.service, "_call",
                          side_effect=ValueError("boom")):
            result = self.service.test()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "unexpected")
        self.assertIn("boom", result["error"])

    def test_callback_accepts_username_based_settings(self):
        event = {"id": "e1", "status": "pending", "caption": "caption",
                 "telegram_message_id": 10}
        update = {"callback_query": {
            "id": "cb1", "data": "pu:e1:confirm",
            "from": {"id": 7, "first_name": "Ann"},
            "message": {"message_id": 10,
                        "chat": {"id": -1001234567890,
                                 "username": "guardroom"}},
        }}
        with patch.object(telegram_bot.db, "get_event",
                          return_value=dict(event)), \
             patch.object(telegram_bot.db, "resolve_event_if_pending",
                          return_value={**event, "status": "confirmed"}) as resolve, \
             patch.object(self.service, "_call",
                          return_value={"status": "administrator"}):
            self.service._handle_update("token", "@guardroom", update)
        resolve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
