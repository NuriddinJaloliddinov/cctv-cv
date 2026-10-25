"""Login, session cookies, roles, account management and lockout."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import db, main, security
from backend.main import app


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / "auth.db")
        db.init_db()
        self.addCleanup(self._restore_db)

        self.previous_auth_disabled = getattr(
            app.state, "auth_disabled", False)
        app.state.auth_disabled = False
        self.addCleanup(self._restore_auth)

        self.admin = TestClient(app)
        self.addCleanup(self.admin.close)
        self.assertEqual(self.login(self.admin).status_code, 200)

    def _restore_db(self):
        db.DB_PATH = self.old_path

    def _restore_auth(self):
        app.state.auth_disabled = self.previous_auth_disabled

    def login(self, client, username="admin", password="12345"):
        return client.post("/api/auth/login", json={
            "username": username, "password": password})

    def make_reader(self, username="reader", password="readerpass"):
        response = self.admin.post("/api/users", json={
            "username": username, "password": password, "role": "reader"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def new_client(self):
        client = TestClient(app)
        self.addCleanup(client.close)
        return client


class LoginTests(AuthTestCase):
    def test_default_admin_login_uses_hashed_secret_and_session_cookie(self):
        response = self.login(self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        user = response.json()["user"]
        self.assertEqual(user["username"], "admin")
        self.assertEqual(user["role"], "admin")
        self.assertTrue(user["active"])

        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)

        stored = db.get_user_by_username("admin")
        self.assertTrue(stored["password_hash"].startswith("pbkdf2_sha256$"))
        self.assertNotIn("12345", stored["password_hash"])
        self.assertTrue(security.verify_password(
            "12345", stored["password_hash"]))

        token = self.admin.cookies.get("cctv_session")
        self.assertTrue(token)
        digest = security.token_digest(token)
        with db._cursor() as conn:
            row = conn.execute(
                "SELECT token_hash, expires_at FROM auth_sessions "
                "WHERE token_hash = ?", (digest,)).fetchone()
        self.assertIsNotNone(row, "session for the current cookie was not stored")
        self.assertNotEqual(row["token_hash"], token)
        self.assertEqual(row["token_hash"], digest)
        self.assertGreater(row["expires_at"],
                           datetime.now(timezone.utc).isoformat(
                               timespec="seconds"))

        me = self.admin.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["id"], user["id"])

    def test_wrong_username_and_password_do_not_reveal_accounts(self):
        response = self.login(self.admin, "nobody", "wrong")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Invalid username or password", response.json()["detail"])

    def test_three_failures_lock_until_an_admin_unlocks(self):
        self.make_reader()
        client = self.new_client()
        first = self.login(client, "reader", "bad")
        second = self.login(client, "reader", "bad")
        third = self.login(client, "reader", "bad")
        self.assertEqual(first.status_code, 401)
        self.assertEqual(second.status_code, 401)
        self.assertEqual(third.status_code, 423)

        blocked = self.login(client, "reader", "readerpass")
        self.assertEqual(blocked.status_code, 423)

        self.assertEqual(self.login(self.admin).status_code, 200)
        users = self.admin.get("/api/users").json()["users"]
        reader = next(u for u in users if u["username"] == "reader")
        self.assertTrue(reader["locked"])
        self.assertGreaterEqual(reader["failed_attempts"], 3)

        unlocked = self.admin.post(f"/api/users/{reader['id']}/unlock")
        self.assertEqual(unlocked.status_code, 200)
        self.assertFalse(unlocked.json()["locked"])
        self.assertEqual(self.login(client, "reader", "readerpass").status_code, 200)

    def test_logout_and_expired_sessions_are_rejected(self):
        self.assertEqual(self.login(self.admin).status_code, 200)
        self.assertEqual(self.admin.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.admin.get("/api/auth/me").status_code, 401)

        self.assertEqual(self.login(self.admin).status_code, 200)
        with db._cursor() as conn:
            conn.execute(
                "UPDATE auth_sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.admin.get("/api/auth/me").status_code, 401)

    def test_disabled_and_unknown_accounts_cannot_sign_in(self):
        reader = self.make_reader()
        self.admin.put(f"/api/users/{reader['id']}", json={"active": False})
        response = self.login(self.new_client(), "reader", "readerpass")
        self.assertEqual(response.status_code, 403)
        self.assertIn("disabled", response.json()["detail"])

    def test_origin_mismatch_blocks_state_changing_requests(self):
        self.assertEqual(self.login(self.admin).status_code, 200)
        response = self.admin.post(
            "/api/users",
            json={"username": "mallory", "password": "pass1234",
                  "role": "reader"},
            headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403)
        same_origin = self.admin.post(
            "/api/users",
            json={"username": "okuser", "password": "pass1234",
                  "role": "reader"},
            headers={"Origin": "http://testserver"})
        self.assertEqual(same_origin.status_code, 201, same_origin.text)


class AuthorizationTests(AuthTestCase):
    def test_dashboard_and_api_need_a_session(self):
        anonymous = self.new_client()
        self.assertEqual(anonymous.get("/api/overview").status_code, 401)
        page = anonymous.get("/", follow_redirects=False)
        self.assertEqual(page.status_code, 302)
        self.assertIn("/login.html", page.headers["location"])
        index = anonymous.get("/index.html", follow_redirects=False)
        self.assertEqual(index.status_code, 302)
        self.assertEqual(anonymous.get("/login.html").status_code, 200)
        self.assertEqual(anonymous.get("/style.css").status_code, 200)

    def test_reader_can_read_but_cannot_mutate_or_manage_accounts(self):
        self.make_reader()
        reader = self.new_client()
        self.assertEqual(self.login(reader, "reader", "readerpass").status_code, 200)

        self.assertEqual(reader.get("/api/overview").status_code, 200)
        self.assertEqual(reader.get("/api/cameras").status_code, 200)
        self.assertEqual(reader.get("/api/users").status_code, 403)
        self.assertEqual(reader.get("/api/telegram").status_code, 403)
        self.assertEqual(reader.post("/api/cameras", json={
            "name": "Door", "address": "rtsp://door"}).status_code, 403)
        self.assertEqual(reader.post(
            "/api/start", json={"camera_id": "x"}).status_code, 403)
        self.assertEqual(reader.post(
            "/api/users", json={"username": "other", "password": "pass1234",
                                "role": "reader"}).status_code, 403)

    def test_reader_can_change_own_password(self):
        self.make_reader()
        reader = self.new_client()
        self.login(reader, "reader", "readerpass")
        changed = reader.post("/api/auth/password", json={
            "current_password": "readerpass",
            "new_password": "newreaderpass"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(reader.get("/api/auth/me").status_code, 200)

        self.assertEqual(reader.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.login(
            reader, "reader", "readerpass").status_code, 401)
        self.assertEqual(self.login(
            reader, "reader", "newreaderpass").status_code, 200)

        wrong = reader.post("/api/auth/password", json={
            "current_password": "nope", "new_password": "anotherpass"})
        self.assertEqual(wrong.status_code, 400)

    def test_disabling_an_account_ends_its_sessions(self):
        reader_data = self.make_reader()
        reader = self.new_client()
        self.login(reader, "reader", "readerpass")
        self.assertEqual(reader.get("/api/auth/me").status_code, 200)

        self.assertEqual(self.admin.put(
            f"/api/users/{reader_data['id']}",
            json={"active": False}).status_code, 200)
        self.assertEqual(reader.get("/api/auth/me").status_code, 401)

        enabled = self.admin.put(
            f"/api/users/{reader_data['id']}", json={"active": True})
        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(self.login(
            reader, "reader", "readerpass").status_code, 200)

    def test_admin_cannot_disable_or_demote_self(self):
        self.login(self.admin)
        me = self.admin.get("/api/auth/me").json()["user"]
        disabled = self.admin.put(
            f"/api/users/{me['id']}", json={"active": False})
        self.assertEqual(disabled.status_code, 400)
        demoted = self.admin.put(
            f"/api/users/{me['id']}", json={"role": "reader"})
        self.assertEqual(demoted.status_code, 400)


class AccountManagementTests(AuthTestCase):
    def test_create_duplicate_validate_and_reset_password(self):
        self.login(self.admin)
        reader = self.make_reader()
        duplicate = self.admin.post("/api/users", json={
            "username": "READER", "password": "pass1234", "role": "reader"})
        self.assertEqual(duplicate.status_code, 409)

        invalid = self.admin.post("/api/users", json={
            "username": "bad name!", "password": "pass1234",
            "role": "reader"})
        self.assertEqual(invalid.status_code, 400)

        short = self.admin.post("/api/users", json={
            "username": "shortpass", "password": "x", "role": "reader"})
        self.assertEqual(short.status_code, 422)

        reset = self.admin.put(
            f"/api/users/{reader['id']}", json={"password": "resetpass"})
        self.assertEqual(reset.status_code, 200)
        client = self.new_client()
        self.assertEqual(self.login(
            client, "reader", "readerpass").status_code, 401)
        self.assertEqual(self.login(
            client, "reader", "resetpass").status_code, 200)

    def test_admin_can_promote_reader_and_see_audit_log(self):
        self.login(self.admin)
        reader = self.make_reader()
        promoted = self.admin.put(
            f"/api/users/{reader['id']}", json={"role": "admin"})
        self.assertEqual(promoted.status_code, 200)
        self.assertEqual(promoted.json()["role"], "admin")

        audit = self.admin.get("/api/auth/audit")
        self.assertEqual(audit.status_code, 200)
        actions = {event["action"] for event in audit.json()["events"]}
        self.assertIn("login", actions)
        self.assertIn("user_created", actions)

    def test_reader_cannot_unlock_or_list_accounts(self):
        reader = self.make_reader()
        client = self.new_client()
        self.login(client, "reader", "readerpass")
        self.assertEqual(client.post(
            f"/api/users/{reader['id']}/unlock").status_code, 403)
        self.assertEqual(client.get("/api/users").status_code, 403)


if __name__ == "__main__":
    unittest.main()
