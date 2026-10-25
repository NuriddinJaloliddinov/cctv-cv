"""Telegram bot integration (long polling, no public webhook required).

The bot:

* sends a photo + caption for phone-use and entrance/exit incidents,
* attaches **Confirm** / **Reject** inline buttons,
* adds **Specify person** to entrance/exit alerts so an administrator can
  correct an unknown or misrecognised enrolled person before confirming,
* verifies with ``getChatMember`` that the person pressing a button is a
  group administrator/creator before changing anything.

Configuration (bot token + group id) is stored in the SQLite settings table
and is editable from the dashboard.
"""
import html
import json
import queue
import re
import threading
import time

import requests

from . import db

_TOKEN_KEY = "telegram_bot_token"
_CHAT_KEY = "telegram_chat_id"
_OFFSET_KEY = "telegram_update_offset"

_API_ROOT = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT = 20
_TEST_TIMEOUT = 10
_ADMIN_CACHE_SECONDS = 60.0
_PICKER_PAGE_SIZE = 10
_PERSON_PREFIX = re.compile(r"^[0-9a-f]{4,32}$")


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return "…"
    return f"{token[:4]}…{token[-4:]}"


def _display_user(user: dict) -> str:
    if not user:
        return "Unknown admin"
    name = " ".join(
        part for part in (user.get("first_name"), user.get("last_name"))
        if part
    ).strip()
    username = user.get("username")
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return str(user.get("id", "Unknown admin"))


def parse_callback_data(data: str | None):
    """Parse ``pu:<event_id>:<confirm|reject>``. Returns (id, action) or None."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "pu":
        return None
    event_id, action = parts[1], parts[2]
    if not event_id or action not in ("confirm", "reject"):
        return None
    return event_id, action


def parse_callback(data: str | None):
    """Parse every inline-button callback.

    Verdict buttons keep the original ``pu:`` format. Attendance actions add:

    ``pi:<event>:<page>``   open/repage the enrolled-person picker
    ``pp:<event>:<id-prefix>`` select an enrolled person
    ``px:<event>``          clear the selection and keep the auto name
    """
    if not data:
        return None
    parts = data.split(":")
    kind = parts[0]
    if kind == "pu" and len(parts) == 3:
        event_id, action = parts[1], parts[2]
        if event_id and action in ("confirm", "reject"):
            return {"type": "verdict", "event_id": event_id,
                    "action": action}
        return None
    if kind == "pi" and len(parts) in (2, 3):
        event_id = parts[1]
        page = 0
        if len(parts) == 3:
            if not parts[2].isdigit():
                return None
            page = int(parts[2])
        if not event_id or page > 100000:
            return None
        return {"type": "picker", "event_id": event_id, "page": page}
    if kind == "pp" and len(parts) == 3:
        event_id, prefix = parts[1], parts[2].lower()
        if not event_id or not _PERSON_PREFIX.match(prefix):
            return None
        return {"type": "person", "event_id": event_id,
                "person_prefix": prefix}
    if kind == "px" and len(parts) == 2:
        if not parts[1]:
            return None
        return {"type": "clear", "event_id": parts[1]}
    if kind == "pc" and len(parts) == 2:
        if not parts[1]:
            return None
        return {"type": "customer", "event_id": parts[1]}
    return None


def check_result(ok: bool, detail: str, *, code: str = "", hint: str = "",
                 warning: str = "", error: str = "", **extra) -> dict:
    """Build a verification response the dashboard can render meaningfully."""
    payload = {"ok": ok, "detail": detail}
    if code:
        payload["code"] = code
    if hint:
        payload["hint"] = hint
    if warning:
        payload["warning"] = warning
    if error:
        payload["error"] = error
    payload.update(extra)
    return payload


def diagnose_error(exc: Exception, context: str = "token") -> tuple[str, str, str]:
    """Map a Telegram/network failure to ``(code, detail, hint)``.

    ``context`` distinguishes token checks from group/member lookups, where
    "not found" has a different meaning.
    """
    text = str(exc or "").strip()
    low = text.lower()
    if "unauthorized" in low or "401" in low:
        return ("invalid_token", "Telegram rejected the bot token.",
                "Copy the token again from @BotFather. It looks like "
                "123456789:AA... with no spaces or line breaks.")
    if "chat not found" in low:
        return ("chat_not_found", "Telegram cannot find that group.",
                "Use the numeric group ID (it must be negative). Add the bot to "
                "the group, and if the group was upgraded to a supergroup, use "
                "the new -100... ID.")
    if "not a member" in low or "kicked" in low:
        return ("bot_not_member", "The bot is not a member of that group.",
                "Add the bot to the group, then verify again.")
    if "not enough rights" in low or "administrator" in low or "admin" in low:
        return ("bot_not_admin", "The bot is not allowed to check group members.",
                "Promote the bot to administrator in the group.")
    if "chat_id is empty" in low:
        return ("missing_chat", "No group ID was provided.",
                "Enter the group ID and verify again.")
    if "timed out" in low or "timeout" in low:
        return ("timeout", "Telegram did not respond in time.",
                "Check the server's internet connection, firewall, or HTTPS "
                "proxy, then try again.")
    if "unreachable" in low or "connection" in low or "proxy" in low:
        return ("network", "Could not reach Telegram.",
                "Check the server's internet connection, firewall, or HTTPS "
                "proxy, then try again.")
    if "not found" in low or "404" in low:
        if context == "member":
            return ("bot_not_admin",
                    "Telegram could not look up the bot's group membership.",
                    "Make sure the bot is in the group, then promote it to "
                    "administrator and verify again.")
        return ("invalid_token", "Telegram rejected the bot token.",
                "Copy the full token from @BotFather; it must look like "
                "123456789:AA...")
    return ("telegram_error", "Telegram returned an error.", "")


class TelegramService:
    def __init__(self):
        self._stop = threading.Event()
        self._reload = threading.Event()
        self._notifications: queue.Queue = queue.Queue()
        self._poll_thread: threading.Thread | None = None
        self._send_thread: threading.Thread | None = None
        self._started = False
        self._generation = 0
        self._last_token = ""
        self._admin_cache: dict[tuple[str, str], tuple[bool, float]] = {}
        self._state_lock = threading.Lock()
        self._bot_username = ""
        self._last_error = ""

    # ---------- lifecycle ----------

    def start(self):
        if self._started:
            return
        self._started = True
        self._stop.clear()
        self._reload.clear()
        self._last_token = self._token()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="telegram-poll", daemon=True)
        self._send_thread = threading.Thread(
            target=self._send_loop, name="telegram-send", daemon=True)
        self._poll_thread.start()
        self._send_thread.start()

    def stop(self):
        if not self._started:
            return
        self._stop.set()
        self._reload.set()
        self._notifications.put(None)
        for thread in (self._poll_thread, self._send_thread):
            if thread is not None:
                thread.join(timeout=5)
        self._started = False

    def reconfigure(self):
        """Wake the poller after the token / group id changed."""
        token = self._token()
        if token != self._last_token:
            db.delete_settings([_OFFSET_KEY])
            self._last_token = token
            with self._state_lock:
                self._bot_username = ""
        self._generation += 1
        self._reload.set()

    # ---------- configuration / status ----------

    def _settings(self) -> tuple[str, str]:
        values = db.get_settings([_TOKEN_KEY, _CHAT_KEY])
        return ((values.get(_TOKEN_KEY) or "").strip(),
                (values.get(_CHAT_KEY) or "").strip())

    def _token(self) -> str:
        return self._settings()[0]

    def is_configured(self) -> bool:
        token, chat_id = self._settings()
        return bool(token and chat_id)

    def status(self) -> dict:
        token, chat_id = self._settings()
        with self._state_lock:
            username = self._bot_username
            error = self._last_error
        return {
            "configured": bool(token and chat_id),
            "token_set": bool(token),
            "token_masked": _mask_token(token),
            "chat_id": chat_id,
            "bot_username": username,
            "last_error": error,
            "admin_only": True,
        }

    def save(self, bot_token: str | None, chat_id: str | None):
        """Persist provided fields. ``None`` means "leave unchanged"."""
        values = {}
        if bot_token is not None:
            values[_TOKEN_KEY] = bot_token.strip()
        if chat_id is not None:
            values[_CHAT_KEY] = chat_id.strip()
        if values:
            db.set_settings(values)
        self.reconfigure()

    def clear(self):
        db.delete_settings([_TOKEN_KEY, _CHAT_KEY, _OFFSET_KEY])
        self.reconfigure()

    def test(self, bot_token: str | None = None,
             chat_id: str | None = None) -> dict:
        """Verify a token/group without saving it.

        ``None`` means "use the saved value", so the dashboard can test the
        fields as typed and only persist them once Telegram accepts them.
        """
        try:
            return self._verify(bot_token, chat_id)
        except Exception as e:  # a bare 500 would leave the UI speechless
            try:
                token = self._token()
            except Exception:
                token = ""
            safe = str(e).replace(token, "***") if token else str(e)
            print(f"[telegram] connection check crashed: {safe}")
            return check_result(
                False, "The connection check could not be completed.",
                code="unexpected",
                hint="Check the server log, then try again.",
                error=safe)

    def _verify(self, token_override=None, chat_override=None) -> dict:
        saved_token, saved_chat = self._settings()
        token = (token_override or "").strip() or saved_token
        chat_id = (chat_override or "").strip() or saved_chat
        if not token:
            return check_result(
                False, "Enter the bot token first.", code="missing_token",
                hint="Create a bot with @BotFather and paste its token here.")

        def failure(exc: Exception, fallback: str,
                    context: str = "token") -> dict:
            safe = str(exc).replace(token, "***") if token else str(exc)
            if not isinstance(exc, RuntimeError):
                code, detail, hint = ("unexpected", fallback,
                                      "Check the server log, then try again.")
            else:
                code, detail, hint = diagnose_error(exc, context)
                if code == "telegram_error":
                    detail = safe.strip() or fallback
                    hint = ("Check that the bot token is valid and that the bot "
                            "is a member of the group.")
            print(f"[telegram] connection check failed ({code}): {safe}")
            return check_result(False, detail, code=code, hint=hint, error=safe)

        try:
            me = self._call(token, "getMe", timeout=_TEST_TIMEOUT) or {}
        except Exception as e:
            return failure(e, "Could not verify the bot token.")
        bot_name = me.get("username") or me.get("first_name") or "bot"

        if not chat_id:
            return check_result(
                False, "Enter the group ID next.", code="missing_chat",
                hint="Group IDs are negative: -100... for supergroups, "
                     "-... for older groups. A positive number is a user ID.")

        try:
            chat = self._call(token, "getChat", {"chat_id": chat_id},
                              timeout=_TEST_TIMEOUT) or {}
        except Exception as e:
            return failure(e, "Could not open that group.")

        chat_type = chat.get("type")
        title = chat.get("title") or chat.get("username") or chat_id
        if chat_type == "channel":
            return check_result(
                False, "That ID belongs to a channel, not a group.",
                code="channel",
                hint="Add the bot to a group and use that group's ID; alerts "
                     "are confirmed by group administrators.")
        if chat_type not in ("group", "supergroup"):
            return check_result(
                False, "That ID belongs to a private chat/user, not a group.",
                code="not_a_group",
                hint="Group IDs are negative, e.g. -123456789 (older group) or "
                     "-1001234567890 (supergroup). A positive number is a "
                     "user ID.")

        # Telegram accepts an @username for public supergroups, but callback
        # queries carry the numeric chat id. Persist the numeric id so the
        # Confirm/Reject buttons keep working.
        resolved = str(chat.get("id") or chat_id)
        if not chat_override and resolved != chat_id:
            # Normalise a saved @username. Values being tested are saved by the
            # dashboard only after Telegram has accepted them.
            db.set_settings({_CHAT_KEY: resolved})

        try:
            member = self._call(token, "getChatMember", {
                "chat_id": resolved, "user_id": me.get("id"),
            }, timeout=_TEST_TIMEOUT) or {}
        except Exception as e:
            return failure(e, "Could not check the bot's group membership.",
                           context="member")
        status = member.get("status")
        if status in ("left", "kicked"):
            return check_result(
                False, "The bot is not a member of that group.",
                code="bot_not_member",
                hint="Add the bot to the group, then verify again.")

        with self._state_lock:
            self._bot_username = bot_name
            self._last_error = ""
        self._reload.set()

        summary = f"Connected to {title} as @{bot_name}."
        if status not in ("administrator", "creator"):
            return check_result(
                True, summary, code="bot_not_admin",
                warning="The bot is not a group administrator. Telegram only "
                        "guarantees member checks for admins, so Confirm/Reject "
                        "may fail.",
                hint="Promote the bot to administrator in the group.",
                chat=title, bot=bot_name, chat_id=resolved)
        return check_result(
            True, summary,
            hint="Only group administrators can confirm or reject alerts.",
            chat=title, bot=bot_name, chat_id=resolved)

    # ---------- notifications ----------

    def notify(self, event_id: str, snapshot_path: str | None,
               caption: str) -> bool:
        if not self.is_configured():
            return False
        self._notifications.put({
            "event_id": event_id,
            "snapshot_path": snapshot_path,
            "caption": caption,
        })
        return True

    # ---------- HTTP ----------

    def _call(self, token: str, method: str, params: dict | None = None,
              files: dict | None = None, timeout: float = _POLL_TIMEOUT):
        url = _API_ROOT.format(token=token, method=method)
        connect_timeout = min(5.0, float(timeout))
        try:
            response = requests.post(
                url, data=params or {}, files=files,
                timeout=(connect_timeout, timeout),
            )
        except requests.Timeout as e:
            raise RuntimeError("Telegram request timed out") from e
        except requests.RequestException as e:
            # Exception strings can contain the request URL, which contains
            # the bot token. Never surface it through the dashboard/API.
            detail = str(e).replace(token, "***") if token else str(e)
            raise RuntimeError(f"Telegram is unreachable ({detail})") from e
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(f"Telegram returned HTTP {response.status_code}")
        if not isinstance(payload, dict):
            raise RuntimeError("Telegram returned an unexpected response")
        if not payload.get("ok"):
            description = payload.get("description") or \
                f"HTTP {response.status_code}"
            if token:
                description = description.replace(token, "***")
            raise RuntimeError(description)
        return payload.get("result")

    def _set_error(self, message: str):
        with self._state_lock:
            self._last_error = message

    def _answer(self, token, callback_id, text="", alert=False):
        params = {
            "callback_query_id": callback_id,
            "show_alert": "true" if alert else "false",
        }
        if text:
            params["text"] = text[:200]
        try:
            self._call(token, "answerCallbackQuery", params, timeout=15)
        except Exception:
            pass

    # ---------- polling ----------

    def _wait_reload(self, seconds: float):
        self._reload.wait(seconds)
        self._reload.clear()

    def _poll_loop(self):
        offset = int(db.get_setting(_OFFSET_KEY, "0") or 0)
        generation = self._generation
        while not self._stop.is_set():
            if self._generation != generation:
                generation = self._generation
                offset = int(db.get_setting(_OFFSET_KEY, "0") or 0)

            token, chat_id = self._settings()
            if not token or not chat_id:
                self._wait_reload(1.0)
                continue

            try:
                updates = self._call(token, "getUpdates", {
                    "offset": offset,
                    "timeout": _POLL_TIMEOUT,
                    "allowed_updates": json.dumps(["callback_query"]),
                }, timeout=_POLL_TIMEOUT + 10)
                self._set_error("")
            except Exception as e:
                message = str(e)
                # A pre-existing webhook makes getUpdates fail with 409.
                if "webhook" in message.lower():
                    try:
                        self._call(token, "deleteWebhook", {}, timeout=15)
                        continue
                    except Exception:
                        pass
                self._set_error(message)
                self._wait_reload(3.0)
                continue

            # The token may have changed while getUpdates was blocking.
            if self._generation != generation:
                continue

            changed = False
            for update in updates or []:
                try:
                    update_id = int(update.get("update_id", 0))
                    offset = max(offset, update_id + 1)
                    changed = True
                    self._handle_update(token, chat_id, update)
                except Exception as e:
                    print(f"[telegram] update {update.get('update_id')}: {e}")
            if changed:
                db.set_settings({_OFFSET_KEY: str(offset)})

    def _is_admin(self, token: str, chat_id: str, user_id) -> tuple[bool, str]:
        key = (str(chat_id), str(user_id))
        now = time.monotonic()
        cached = self._admin_cache.get(key)
        if cached and cached[1] > now:
            return cached[0], ""
        try:
            member = self._call(token, "getChatMember", {
                "chat_id": chat_id, "user_id": user_id,
            }, timeout=15)
        except Exception:
            return False, "Could not verify your group role. Try again."
        allowed = member.get("status") in ("creator", "administrator")
        self._admin_cache[key] = (allowed, now + _ADMIN_CACHE_SECONDS)
        return allowed, ""

    def _handle_update(self, token: str, chat_id: str, update: dict):
        callback = update.get("callback_query")
        if not callback:
            return
        callback_id = callback.get("id")
        parsed = parse_callback(callback.get("data"))
        if parsed is None:
            self._answer(token, callback_id, "Unknown action.")
            return
        event_id = parsed["event_id"]
        event = db.get_event(event_id)
        if event is None:
            self._answer(token, callback_id, "This event no longer exists.", True)
            return

        message = callback.get("message") or {}
        message_chat = message.get("chat") or {}
        # Older settings may still hold an @username instead of the numeric id;
        # accept that too, because getChatMember/sendPhoto understand usernames.
        saved_chat = str(chat_id)
        actual_id = str(message_chat.get("id"))
        actual_username = (message_chat.get("username") or "").lstrip("@").lower()
        same_chat = saved_chat == actual_id or (
            saved_chat.startswith("@") and actual_username
            and saved_chat[1:].lower() == actual_username)
        if not same_chat:
            self._answer(token, callback_id, "This alert belongs to another chat.", True)
            return
        if event.get("telegram_message_id") and \
                message.get("message_id") != event["telegram_message_id"]:
            self._answer(token, callback_id, "This alert does not match the event.", True)
            return

        user = callback.get("from") or {}
        user_id = user.get("id")
        if user_id is None:
            self._answer(token, callback_id, "Could not identify your account.", True)
            return

        allowed, reason = self._is_admin(token, chat_id, user_id)
        if not allowed:
            self._answer(token, callback_id,
                         reason or "Only group administrators can confirm or "
                                   "reject events.", True)
            return

        if event.get("status") != "pending":
            self._answer(token, callback_id,
                         f"This event is already {event['status']}.", True)
            return

        if parsed["type"] != "verdict":
            if event.get("kind") not in db.ATTENDANCE_KINDS:
                self._answer(token, callback_id,
                             "Person and customer actions apply only to "
                             "entrance/exit alerts.", True)
                return
            if parsed["type"] == "picker":
                self._show_person_picker(token, chat_id, message, event,
                                         parsed.get("page", 0), callback_id)
            elif parsed["type"] == "person":
                self._select_person(token, chat_id, message, event,
                                    parsed["person_prefix"], callback_id)
            elif parsed["type"] == "customer":
                self._toggle_customer(token, chat_id, message, event,
                                      callback_id)
            else:
                self._clear_person(token, chat_id, message, event, callback_id)
            return

        new_status = "confirmed" if parsed["action"] == "confirm" else "rejected"
        name = _display_user(user)
        updated = db.resolve_event_if_pending(
            event_id, new_status, name, str(user_id))
        if updated is None:
            self._answer(token, callback_id,
                         "This event was already handled.", True)
            return

        verdict = ("✅ Confirmed by " if new_status == "confirmed"
                   else "❌ Rejected by ") + html.escape(name)
        if event.get("kind") in db.ATTENDANCE_KINDS:
            attendance = updated.get("_attendance")
            if new_status == "confirmed":
                if attendance:
                    verdict += ("\n📋 Attendance recorded for "
                                f"{html.escape(attendance['person_name'])}.")
                else:
                    verdict += ("\n📋 No attendance recorded "
                                "(unknown, ambiguous, or unassigned crossing).")
            else:
                verdict += "\n📋 Attendance unchanged."
        caption = (event.get("caption") or "") + "\n\n" + verdict
        self._edit_message(token, chat_id, message, caption, [])
        self._answer(token, callback_id,
                     "Event confirmed." if new_status == "confirmed"
                     else "Event rejected.")

    # ---------- attendance person picker ----------

    @staticmethod
    def _name_button(name: str) -> str:
        name = " ".join((name or "Unnamed").split())
        return name if len(name) <= 48 else name[:47] + "…"

    def _crossing_keyboard(self, event: dict) -> list:
        rows = [[
            {"text": "✅ Confirm",
             "callback_data": f"pu:{event['id']}:confirm"},
            {"text": "❌ Reject",
             "callback_data": f"pu:{event['id']}:reject"},
        ]]
        label = ("🔄 Change person" if event.get("specified_person_name")
                 else "👤 Specify person")
        rows.append([{"text": label,
                      "callback_data": f"pi:{event['id']}"}])
        customer_label = ("🛍 Customer ✓ (remove)" if event.get("customer")
                          else "🛍 Count as customer")
        rows.append([{"text": customer_label,
                      "callback_data": f"pc:{event['id']}"}])
        return rows

    def _alert_keyboard(self, event: dict) -> list:
        if event.get("kind") in db.ATTENDANCE_KINDS:
            return self._crossing_keyboard(event)
        return [[
            {"text": "✅ Confirm",
             "callback_data": f"pu:{event['id']}:confirm"},
            {"text": "❌ Reject",
             "callback_data": f"pu:{event['id']}:reject"},
        ]]

    def _edit_message(self, token: str, chat_id: str, message: dict,
                      caption: str, keyboard: list):
        if not message.get("message_id"):
            return
        params = {
            "chat_id": chat_id,
            "message_id": message["message_id"],
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": keyboard}),
        }
        try:
            self._call(token, "editMessageCaption", params, timeout=15)
            return
        except Exception:
            pass
        # Alerts sent with sendMessage (photo upload failed) have text instead.
        text_params = {key: value for key, value in params.items()
                       if key != "caption"}
        text_params["text"] = caption[:4096]
        try:
            self._call(token, "editMessageText", text_params, timeout=15)
        except Exception:
            # The stored decision/person is authoritative; cosmetics can fail.
            pass

    def _show_person_picker(self, token: str, chat_id: str, message: dict,
                            event: dict, page: int, callback_id):
        people = sorted(db.list_persons(),
                        key=lambda p: ((p.get("name") or "").casefold(),
                                       p.get("id") or ""))
        if not people:
            self._answer(token, callback_id,
                         "No enrolled people yet. Enroll someone first.", True)
            return
        pages = max(1, (len(people) + _PICKER_PAGE_SIZE - 1)
                    // _PICKER_PAGE_SIZE)
        page = max(0, min(int(page), pages - 1))
        start = page * _PICKER_PAGE_SIZE
        selected_id = event.get("specified_person_id")
        rows = []
        for person in people[start:start + _PICKER_PAGE_SIZE]:
            prefix = str(person["id"])[:16]
            mark = "✓ " if person["id"] == selected_id else ""
            rows.append([{
                "text": mark + self._name_button(person["name"]),
                "callback_data": f"pp:{event['id']}:{prefix}",
            }])
        nav = []
        if page > 0:
            nav.append({"text": "◀ Previous",
                        "callback_data": f"pi:{event['id']}:{page - 1}"})
        if page < pages - 1:
            nav.append({"text": "Next ▶",
                        "callback_data": f"pi:{event['id']}:{page + 1}"})
        if nav:
            rows.append(nav)
        rows.append([{"text": "✖ Cancel / keep auto name",
                      "callback_data": f"px:{event['id']}"}])

        caption = (event.get("caption") or "") + "\n\n👤 <b>Specify person</b>"
        if event.get("specified_person_name"):
            caption += (f"\nCurrent selection: "
                        f"<b>{html.escape(event['specified_person_name'])}</b>")
        caption += (f"\nPage {page + 1} of {pages}. Choose the enrolled person, "
                    "then press <b>Confirm</b>.")
        self._edit_message(token, chat_id, message, caption, rows)
        self._answer(token, callback_id, "Choose a person from the list.")

    def _select_person(self, token: str, chat_id: str, message: dict,
                       event: dict, prefix: str, callback_id):
        person = db.find_person_by_prefix(prefix)
        if person is None:
            self._answer(token, callback_id,
                         "That person is no longer available. Open Specify "
                         "person again.", True)
            return
        updated = db.specify_event_person(
            event["id"], person["id"], person["name"])
        if updated is None:
            self._answer(token, callback_id,
                         "This event was already handled.", True)
            return
        action = "entrance" if updated["kind"] == "entered" else "exit"
        caption = (updated.get("caption") or "") + (
            f"\n\n👤 Selected person: <b>{html.escape(person['name'])}</b>\n"
            f"Press ✅ Confirm to record this {action} in attendance, or "
            "❌ Reject.")
        self._edit_message(token, chat_id, message, caption,
                           self._crossing_keyboard(updated))
        self._answer(token, callback_id,
                     f"{person['name']} selected. Press Confirm to record "
                     "attendance.")

    def _toggle_customer(self, token: str, chat_id: str, message: dict,
                         event: dict, callback_id):
        customer = not bool(event.get("customer"))
        updated = db.set_event_customer(event["id"], customer)
        if updated is None:
            self._answer(token, callback_id,
                         "This event was already handled.", True)
            return
        line = f"🛍 Customer: <b>{'Yes' if customer else 'No'}</b>"
        caption = updated.get("caption") or ""
        if "🛍 Customer:" in caption:
            caption = re.sub(r"🛍 Customer:\s*<b>[^<]*</b>", line,
                             caption, count=1)
        else:
            caption = caption.rstrip() + "\n" + line
        self._edit_message(token, chat_id, message, caption,
                           self._crossing_keyboard(updated))
        self._answer(token, callback_id,
                     "Marked as customer." if customer
                     else "Customer tag removed.")

    def _clear_person(self, token: str, chat_id: str, message: dict,
                      event: dict, callback_id):
        updated = db.clear_event_person(event["id"])
        if updated is None:
            self._answer(token, callback_id,
                         "This event was already handled.", True)
            return
        caption = (updated.get("caption") or "") + (
            "\n\nNo person selected. Confirm keeps this crossing unassigned, "
            "so it will not change attendance.")
        self._edit_message(token, chat_id, message, caption,
                           self._crossing_keyboard(updated))
        self._answer(token, callback_id, "Selection cleared.")

    # ---------- sending ----------

    def _send_loop(self):
        while not self._stop.is_set():
            try:
                item = self._notifications.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                continue
            try:
                self._send_alert(item)
            except Exception as e:
                print(f"[telegram] send failed: {e}")

    def _send_alert(self, item: dict):
        token, chat_id = self._settings()
        event = db.get_event(item["event_id"])
        if event is None or event.get("status") != "pending":
            return
        if not token or not chat_id:
            db.update_event(event["id"], {"status": "failed"})
            self._set_error("Telegram is not configured.")
            return

        caption = (item.get("caption") or "")[:1024]
        keyboard = json.dumps({"inline_keyboard":
                               self._alert_keyboard(event)})
        params = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }

        message = None
        snapshot = item.get("snapshot_path")
        if snapshot:
            try:
                with open(snapshot, "rb") as photo:
                    message = self._call(token, "sendPhoto", params, files={
                        "photo": ("snapshot.jpg", photo, "image/jpeg"),
                    }, timeout=60)
            except Exception as e:
                self._set_error(f"Photo alert failed: {e}")
        if message is None:
            try:
                message_params = {
                    key: value for key, value in params.items()
                    if key != "caption"
                }
                message_params["text"] = caption
                message = self._call(token, "sendMessage", message_params,
                                     timeout=30)
            except Exception as e:
                db.update_event(event["id"], {"status": "failed"})
                self._set_error(f"Alert could not be sent: {e}")
                return

        db.update_event(event["id"], {
            "telegram_chat_id": chat_id,
            "telegram_message_id": message.get("message_id"),
        })
        self._set_error("")


service = TelegramService()
