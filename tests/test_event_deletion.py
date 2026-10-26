"""Deleting dashboard events removes their stored data and derived attendance."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import db, main


class EventDeletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "db.sqlite"))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.previous_auth_disabled = getattr(main.app.state, "auth_disabled", False)
        main.app.state.auth_disabled = False
        self.addCleanup(setattr, main.app.state, "auth_disabled", self.previous_auth_disabled)
        db.init_db()
        self.admin = TestClient(main.app)
        self.addCleanup(self.admin.close)
        self.assertEqual(self.admin.post("/api/auth/login", json={
            "username": "admin", "password": "12345",
        }).status_code, 200)

    def create_event(self, event_id, *, person_id=None, customer=False, snapshot=None):
        return db.create_event(
            event_id, "cam", "Door", 1, "Alice" if person_id else "Unknown",
            "entered", .9, str(snapshot) if snapshot else None, "caption",
            recognized_person_id=person_id, customer=customer)

    def test_confirmed_crossing_deletes_event_attendance_snapshot_and_totals(self):
        db.create_person("alice", "Alice", "", None)
        snapshot = Path(self.tmp.name) / "event.jpg"
        snapshot.write_bytes(b"image")
        self.create_event("crossing", person_id="alice", customer=True, snapshot=snapshot)
        db.resolve_event_if_pending("crossing", "confirmed", "Admin", "1")
        self.assertEqual(db.attendance_overview()["summary"]["inside"], 1)

        response = self.admin.delete("/api/events/crossing")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(snapshot.exists())
        self.assertIsNone(db.get_event("crossing"))
        with db._cursor() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM attendance WHERE event_id = 'crossing'"
            ).fetchone()[0], 0)
        self.assertEqual(self.admin.get("/api/events?status=all").json()["events"], [])
        self.assertEqual(self.admin.get("/api/customers").json()["summary"]["total"], 0)
        attendance = self.admin.get("/api/attendance").json()
        self.assertEqual(attendance["summary"]["inside"], 0)
        self.assertEqual(attendance["recent"], [])
        overview = self.admin.get("/api/overview").json()
        self.assertEqual(overview["recent_events"], [])
        self.assertEqual(overview["recent_attendance"], [])
        self.assertEqual(self.admin.get("/api/events/crossing/snapshot").status_code, 404)
        self.assertEqual(self.admin.delete("/api/events/crossing").status_code, 404)

    def test_unassigned_and_pending_events_disappear(self):
        self.create_event("unknown")
        db.resolve_event_if_pending("unknown", "confirmed", "Admin", "1")
        self.create_event("pending", customer=True)
        self.assertEqual(len(db.attendance_overview()["unknown_events"]), 1)
        self.assertEqual(db.event_counts()["pending"], 1)
        for event_id in ("unknown", "pending"):
            self.assertEqual(self.admin.delete(f"/api/events/{event_id}").status_code, 200)
        self.assertEqual(db.attendance_overview()["unknown_events"], [])
        self.assertEqual(db.event_counts()["pending"], 0)
        self.assertEqual(db.customer_summary()["total"], 0)

    def test_bulk_categories_delete_only_matching_events(self):
        db.create_person("alice", "Alice", "", None)
        self.create_event("unassigned")
        db.resolve_event_if_pending("unassigned", "confirmed", "Admin", "1")
        self.create_event("attendance", person_id="alice")
        db.resolve_event_if_pending("attendance", "confirmed", "Admin", "1")
        customer_snapshot = Path(self.tmp.name) / "customer.jpg"
        customer_snapshot.write_bytes(b"image")
        self.create_event("customer", customer=True, snapshot=customer_snapshot)
        self.create_event("ordinary")

        for scope, removed in (("unassigned", "unassigned"),
                               ("attendance", "attendance"),
                               ("customers", "customer"),
                               ("telegram", "ordinary")):
            response = self.admin.delete(f"/api/events?scope={scope}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["deleted"], 1)
            self.assertIsNone(db.get_event(removed))
            if scope == "unassigned":
                self.assertIsNotNone(db.get_event("attendance"))
            if scope == "attendance":
                self.assertEqual(db.attendance_overview()["recent"], [])
            if scope == "customers":
                self.assertFalse(customer_snapshot.exists())
        self.assertEqual(self.admin.delete("/api/events?scope=telegram").json()["deleted"], 0)
        self.assertEqual(self.admin.delete("/api/events?scope=invalid").status_code, 422)

    def test_only_administrators_can_delete(self):
        self.create_event("protected")
        anonymous = TestClient(main.app)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.delete("/api/events/protected").status_code, 401)
        self.assertEqual(self.admin.post("/api/users", json={
            "username": "reader", "password": "readerpass", "role": "reader",
        }).status_code, 201)
        self.assertEqual(anonymous.post("/api/auth/login", json={
            "username": "reader", "password": "readerpass",
        }).status_code, 200)
        self.assertEqual(anonymous.delete("/api/events/protected").status_code, 403)
        self.assertEqual(anonymous.delete("/api/events?scope=telegram").status_code, 403)
        self.assertIsNotNone(db.get_event("protected"))


if __name__ == "__main__":
    unittest.main()
