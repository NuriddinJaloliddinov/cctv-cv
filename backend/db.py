"""SQLite persistence: cameras, persons, face embeddings."""
import os
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

from .config import DATA_DIR, PHONE_CONF_THRESHOLD, PHONE_LOOK_PITCH_RATIO
from .config import (
    AUTH_DEFAULT_PASSWORD, AUTH_DEFAULT_USERNAME,
    AUTH_MAX_FAILED_ATTEMPTS,
)
from .models import CameraInfo
from .security import hash_password

DB_PATH = os.path.join(DATA_DIR, "cctv.db")
_lock = threading.Lock()


# ---------- Connection helpers ----------

def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _cursor():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- Schema ----------

def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    """Add columns that a database file created earlier does not have."""
    existing = {row[1] for row in conn.execute(
        f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db() -> None:
    try:
        with _cursor() as conn:
            # --- cameras ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cameras (
                    id           TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    address      TEXT NOT NULL,
                    rtsp_url     TEXT,
                    source_type  TEXT NOT NULL DEFAULT 'onvif',
                    onvif_user   TEXT NOT NULL DEFAULT '',
                    onvif_pass   TEXT NOT NULL DEFAULT '',
                    device       TEXT NOT NULL DEFAULT 'auto',
                    fps          INTEGER NOT NULL DEFAULT 10,
                    phone_conf   REAL NOT NULL DEFAULT 0.25,
                    phone_pitch  REAL NOT NULL DEFAULT 0.70,
                    role         TEXT,
                    tripwire     TEXT,
                    tripwire_direction TEXT,
                    count_customer INTEGER NOT NULL DEFAULT 0,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_cameras_address "
                "ON cameras(address)"
            )
            # Per-camera phone tuning arrived after the first release.
            _ensure_columns(conn, "cameras", {
                "phone_conf": "REAL NOT NULL DEFAULT 0.25",
                "phone_pitch": "REAL NOT NULL DEFAULT 0.70",
                "role": "TEXT",
                "tripwire": "TEXT",
                "tripwire_direction": "TEXT",
                "count_customer": "INTEGER NOT NULL DEFAULT 0",
            })

            # --- persons ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS persons (
                    id             TEXT PRIMARY KEY,
                    name           TEXT NOT NULL,
                    notes          TEXT NOT NULL DEFAULT '',
                    thumbnail_path TEXT,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                )
            """)

            # --- person_embeddings ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS person_embeddings (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id  TEXT NOT NULL,
                    model      TEXT NOT NULL DEFAULT 'sface',
                    embedding  BLOB NOT NULL,
                    source     TEXT NOT NULL DEFAULT 'image',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (person_id) REFERENCES persons(id) ON DELETE CASCADE
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_emb_person "
                "ON person_embeddings(person_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_emb_model "
                "ON person_embeddings(model)"
            )

            # --- settings (Telegram credentials, poll offsets, ...) ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            # --- events (phone-use incidents reviewed in Telegram) ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id                  TEXT PRIMARY KEY,
                    camera_id           TEXT,
                    camera_name         TEXT NOT NULL DEFAULT '',
                    track_id            INTEGER,
                    person_name         TEXT NOT NULL DEFAULT 'Unknown',
                    kind                TEXT NOT NULL,
                    confidence          REAL NOT NULL DEFAULT 0,
                    snapshot_path       TEXT,
                    caption             TEXT NOT NULL DEFAULT '',
                    status              TEXT NOT NULL DEFAULT 'pending',
                    telegram_chat_id    TEXT,
                    telegram_message_id INTEGER,
                    handled_by          TEXT,
                    handled_by_id       TEXT,
                    customer            INTEGER NOT NULL DEFAULT 0,
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_status_created "
                "ON events(status, created_at DESC)"
            )
            # Attendance was added after the first events release.
            _ensure_columns(conn, "events", {
                "recognized_person_id": "TEXT",
                "recognized_person_name": "TEXT",
                "specified_person_id": "TEXT",
                "specified_person_name": "TEXT",
                "customer": "INTEGER NOT NULL DEFAULT 0",
            })
            conn.execute("""
                CREATE TABLE IF NOT EXISTS attendance (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id      TEXT NOT NULL UNIQUE,
                    person_id     TEXT NOT NULL,
                    person_name   TEXT NOT NULL,
                    kind          TEXT NOT NULL,
                    camera_id     TEXT,
                    camera_name   TEXT NOT NULL DEFAULT '',
                    occurred_at   TEXT NOT NULL,
                    recorded_at   TEXT NOT NULL,
                    handled_by    TEXT,
                    handled_by_id TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attendance_person_time "
                "ON attendance(person_id, occurred_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attendance_time "
                "ON attendance(occurred_at DESC)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS review_samples (
                    id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    score REAL,
                    label TEXT NOT NULL DEFAULT 'unreviewed',
                    person_box TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                )
            """)
            _ensure_columns(conn, "review_samples", {"person_box": "TEXT"})
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_camera_date "
                         "ON review_samples(camera_id, created_at DESC)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS review_capture_log (
                    sample_id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_capture_hour "
                         "ON review_capture_log(camera_id, captured_at)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS face_candidates (
                    id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_face_candidates_time "
                         "ON face_candidates(camera_id, created_at DESC)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS face_capture_log (
                    id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_face_capture_hour "
                         "ON face_capture_log(camera_id, captured_at)")
            # --- authentication ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id               TEXT PRIMARY KEY,
                    username         TEXT NOT NULL,
                    password_hash    TEXT NOT NULL,
                    role             TEXT NOT NULL DEFAULT 'reader',
                    active           INTEGER NOT NULL DEFAULT 1,
                    failed_attempts  INTEGER NOT NULL DEFAULT 0,
                    locked           INTEGER NOT NULL DEFAULT 0,
                    created_by       TEXT,
                    created_at       TEXT NOT NULL,
                    updated_at       TEXT NOT NULL,
                    last_login_at    TEXT
                )
            """)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
                "ON users(username COLLATE NOCASE)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash  TEXT PRIMARY KEY,
                    user_id     TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    expires_at  TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user "
                "ON auth_sessions(user_id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_audit (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT,
                    username   TEXT NOT NULL DEFAULT '',
                    action     TEXT NOT NULL,
                    detail     TEXT NOT NULL DEFAULT '',
                    ip         TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_auth_audit_time "
                "ON auth_audit(created_at DESC)")
            # Create the bootstrap administrator only for a brand-new database.
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                ts = _now()
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, role, "
                    "active, created_by, created_at, updated_at) "
                    "VALUES (?,?,?,?,1,?,?,?)",
                    (uuid.uuid4().hex, AUTH_DEFAULT_USERNAME,
                     hash_password(AUTH_DEFAULT_PASSWORD), "admin",
                     "system", ts, ts),
                )

            # Preserve the current window when upgrading an existing database.
            conn.execute("INSERT OR IGNORE INTO review_capture_log "
                         "(sample_id, camera_id, captured_at) "
                         "SELECT id, camera_id, created_at FROM review_samples "
                         "WHERE source != 'upload' AND created_at >= ?",
                         ((datetime.now(timezone.utc) - timedelta(hours=1))
                          .isoformat(timespec="seconds"),))
    except sqlite3.DatabaseError:
        path = Path(DB_PATH)
        if path.exists():
            backup = path.with_name(
                f"{path.stem}.corrupt-{int(datetime.now().timestamp())}{path.suffix}"
            )
            path.rename(backup)
        init_db()


def add_review_sample(sample_id, camera_id, source, image_path, score=None):
    with _cursor() as conn:
        ts = _now()
        conn.execute("INSERT INTO review_samples "
                     "(id, camera_id, source, image_path, score, created_at) "
                     "VALUES (?,?,?,?,?,?)",
                     (sample_id, camera_id, source, image_path, score, ts))
        if source != "upload":
            conn.execute("INSERT INTO review_capture_log "
                         "(sample_id, camera_id, captured_at) VALUES (?,?,?)",
                         (sample_id, camera_id, ts))
            conn.execute("DELETE FROM review_capture_log WHERE captured_at < ?",
                         ((datetime.now(timezone.utc) - timedelta(hours=1))
                          .isoformat(timespec="seconds"),))


def review_captures_last_hour(camera_id):
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
        timespec="seconds")
    with _cursor() as conn:
        row = conn.execute("SELECT COUNT(*) FROM review_capture_log "
                           "WHERE camera_id = ? AND captured_at >= ?",
                           (camera_id, since)).fetchone()
    return row[0]


def get_review_sample(sample_id):
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM review_samples WHERE id = ?",
                           (sample_id,)).fetchone()
    return _review_row(row) if row else None


def _review_row(row):
    result = dict(row)
    result["person_box"] = json.loads(result["person_box"]) if result["person_box"] else None
    return result


def list_review_samples(camera_id=None, label=None, limit=100):
    filters, values = [], []
    if camera_id:
        filters.append("camera_id = ?")
        values.append(camera_id)
    if label:
        filters.append("label = ?")
        values.append(label)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    with _cursor() as conn:
        rows = conn.execute("SELECT * FROM review_samples" + where +
                            " ORDER BY created_at DESC, id DESC LIMIT ?",
                            (*values, limit)).fetchall()
    return [_review_row(row) for row in rows]


def set_review_box(sample_id, box):
    with _cursor() as conn:
        changed = conn.execute("UPDATE review_samples SET person_box = ? "
                               "WHERE id = ?", (json.dumps(box) if box else None,
                                                sample_id)).rowcount
    return bool(changed)


def label_review_sample(sample_id, label):
    with _cursor() as conn:
        changed = conn.execute("UPDATE review_samples "
                               "SET label = ?, reviewed_at = ? WHERE id = ?",
                               (label, _now(), sample_id)).rowcount
    return bool(changed)


def delete_review_sample(sample_id):
    with _cursor() as conn:
        row = conn.execute("SELECT image_path FROM review_samples WHERE id = ?",
                           (sample_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM review_samples WHERE id = ?", (sample_id,))
    return row[0] if row else None


def review_counts():
    with _cursor() as conn:
        rows = conn.execute("SELECT label, COUNT(*) AS n FROM review_samples "
                            "GROUP BY label").fetchall()
    return {row["label"]: row["n"] for row in rows}


# ---------- Unknown face review ----------

def face_candidate_count():
    with _cursor() as conn:
        return conn.execute("SELECT COUNT(*) FROM face_candidates").fetchone()[0]


def face_capture_count(camera_id):
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
        timespec="seconds")
    with _cursor() as conn:
        return conn.execute("SELECT COUNT(*) FROM face_capture_log "
                            "WHERE camera_id = ? AND captured_at >= ?",
                            (camera_id, since)).fetchone()[0]


def add_face_candidate(candidate_id, camera_id, image_path, embedding):
    ts = _now()
    with _cursor() as conn:
        conn.execute("INSERT INTO face_candidates "
                     "(id, camera_id, image_path, embedding, created_at) "
                     "VALUES (?,?,?,?,?)",
                     (candidate_id, camera_id, image_path, embedding.tobytes(), ts))
        conn.execute("INSERT INTO face_capture_log (id, camera_id, captured_at) "
                     "VALUES (?,?,?)", (candidate_id, camera_id, ts))
        conn.execute("DELETE FROM face_capture_log WHERE captured_at < ?",
                     ((datetime.now(timezone.utc) - timedelta(hours=1))
                      .isoformat(timespec="seconds"),))


def list_face_candidates(limit=100):
    with _cursor() as conn:
        rows = conn.execute("SELECT id, camera_id, image_path, created_at "
                            "FROM face_candidates ORDER BY created_at DESC, id DESC "
                            "LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def get_face_candidate(candidate_id):
    with _cursor() as conn:
        row = conn.execute("SELECT id, camera_id, image_path, created_at "
                           "FROM face_candidates WHERE id = ?",
                           (candidate_id,)).fetchone()
    return dict(row) if row else None


def recent_face_embeddings(limit=300):
    with _cursor() as conn:
        rows = conn.execute("SELECT embedding FROM face_candidates "
                            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [np.frombuffer(row["embedding"], dtype=np.float32) for row in rows]


def remove_face_candidate(candidate_id):
    with _cursor() as conn:
        row = conn.execute("SELECT image_path FROM face_candidates WHERE id = ?",
                           (candidate_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM face_candidates WHERE id = ?", (candidate_id,))
    return row["image_path"] if row else None


# ---------- Cameras ----------

def _row_to_camera(row: sqlite3.Row) -> CameraInfo:
    values = {k: row[k] for k in row.keys()}
    values["tripwire"] = json.loads(values["tripwire"]) if values.get("tripwire") else None
    return CameraInfo(**values)


def list_cameras() -> list[CameraInfo]:
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM cameras ORDER BY created_at ASC"
        ).fetchall()
        return [_row_to_camera(r) for r in rows]


def get_camera(camera_id: str) -> CameraInfo | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM cameras WHERE id = ?", (camera_id,)
        ).fetchone()
        return _row_to_camera(row) if row else None


def find_by_address(address: str) -> CameraInfo | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM cameras WHERE address = ?", (address,)
        ).fetchone()
        return _row_to_camera(row) if row else None


def create_camera(data: dict) -> CameraInfo:
    camera_id = uuid.uuid4().hex
    ts = _now()
    with _cursor() as conn:
        conn.execute("""
            INSERT INTO cameras
              (id, name, address, rtsp_url, source_type,
               onvif_user, onvif_pass, device, fps,
                phone_conf, phone_pitch, role, tripwire, tripwire_direction,
                count_customer, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            camera_id,
            data["name"],
            data["address"],
            data.get("rtsp_url"),
            data.get("source_type", "onvif"),
            data.get("onvif_user", ""),
            data.get("onvif_pass", ""),
            data.get("device", "auto"),
            int(data.get("fps", 10)),
            float(data.get("phone_conf", PHONE_CONF_THRESHOLD)),
            float(data.get("phone_pitch", PHONE_LOOK_PITCH_RATIO)),
            data.get("role"),
            json.dumps(data["tripwire"]) if data.get("tripwire") else None,
            data.get("tripwire_direction"),
            1 if data.get("count_customer") and data.get("role") else 0,
            ts, ts,
        ))
    return get_camera(camera_id)


def update_camera(camera_id: str, patch: dict) -> CameraInfo | None:
    if not patch:
        return get_camera(camera_id)
    if "role" in patch and patch["role"] is None:
        # A customer flag only makes sense on an entrance/exit camera.
        patch = {**patch, "count_customer": False}
    allowed = {"name", "address", "rtsp_url", "source_type",
               "onvif_user", "onvif_pass", "device", "fps",
               "phone_conf", "phone_pitch", "role", "tripwire",
               "tripwire_direction", "count_customer"}
    sets, vals = [], []
    for k, v in patch.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        if v is None:
            vals.append(v)
        elif k == "fps":
            vals.append(int(v))
        elif k in ("phone_conf", "phone_pitch"):
            vals.append(float(v))
        elif k == "count_customer":
            vals.append(1 if v else 0)
        elif k == "tripwire":
            vals.append(json.dumps(v) if v is not None else None)
        else:
            vals.append(v)
    if not sets:
        return get_camera(camera_id)
    sets.append("updated_at = ?")
    vals.append(_now())
    vals.append(camera_id)
    with _cursor() as conn:
        cur = conn.execute(
            f"UPDATE cameras SET {', '.join(sets)} WHERE id = ?", vals
        )
        if cur.rowcount == 0:
            return None
    return get_camera(camera_id)


def delete_camera(camera_id: str) -> bool:
    with _cursor() as conn:
        cur = conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
        return cur.rowcount > 0


# ---------- Version counters (matcher cache invalidation) ----------

_persons_version = 0
_embeddings_version = 0
_version_lock = threading.Lock()


def _bump_persons_version():
    global _persons_version
    with _version_lock:
        _persons_version += 1


def _bump_embeddings_version():
    global _embeddings_version
    with _version_lock:
        _embeddings_version += 1


def persons_version() -> int:
    return _persons_version


def embeddings_version() -> int:
    return _embeddings_version


# ---------- Persons ----------

def list_persons() -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT id, name, notes, thumbnail_path, created_at, updated_at "
            "FROM persons ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_person(person_id: str) -> dict | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM persons WHERE id = ?", (person_id,)
        ).fetchone()
    return dict(row) if row else None


def create_person(person_id: str, name: str, notes: str,
                  thumbnail_path: str | None) -> dict | None:
    ts = _now()
    with _cursor() as conn:
        conn.execute(
            "INSERT INTO persons (id, name, notes, thumbnail_path, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (person_id, name, notes, thumbnail_path, ts, ts),
        )
    _bump_persons_version()
    return get_person(person_id)


def update_person(person_id: str, patch: dict) -> dict | None:
    allowed = {"name", "notes", "thumbnail_path"}
    sets, vals = [], []
    for k, v in patch.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return get_person(person_id)
    sets.append("updated_at = ?")
    vals.extend([_now(), person_id])
    with _cursor() as conn:
        cur = conn.execute(
            f"UPDATE persons SET {', '.join(sets)} WHERE id = ?", vals)
        if cur.rowcount == 0:
            return None
    _bump_persons_version()
    return get_person(person_id)


def delete_person(person_id: str) -> bool:
    with _cursor() as conn:
        cur = conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
        ok = cur.rowcount > 0
    if ok:
        _bump_persons_version()
        _bump_embeddings_version()
    return ok


# ---------- Embeddings ----------

def add_embeddings(person_id: str, embeddings: list[np.ndarray],
                   source: str = "image") -> int:
    ts = _now()
    with _cursor() as conn:
        for emb in embeddings:
            conn.execute(
                "INSERT INTO person_embeddings "
                "(person_id, model, embedding, source, created_at) "
                "VALUES (?, 'sface', ?, ?, ?)",
                (person_id, emb.tobytes(), source, ts),
            )
    _bump_embeddings_version()
    return len(embeddings)


def count_embeddings(person_id: str) -> int:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM person_embeddings WHERE person_id = ?",
            (person_id,),
        ).fetchone()
    return row[0] if row else 0


def load_all_embeddings() -> tuple[np.ndarray, list[str]]:
    with _cursor() as conn:
        rows = conn.execute("""
            SELECT pe.embedding, p.name
            FROM person_embeddings pe
            JOIN persons p ON p.id = pe.person_id
            WHERE pe.model = 'sface'
        """).fetchall()
    if not rows:
        return np.zeros((0, 128), dtype=np.float32), []
    embs = np.stack([
        np.frombuffer(r["embedding"], dtype=np.float32) for r in rows
    ])
    names = [r["name"] for r in rows]
    return embs, names


# ---------- Settings ----------

def get_setting(key: str, default: str | None = None) -> str | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def get_settings(keys: list[str] | None = None) -> dict[str, str]:
    with _cursor() as conn:
        if keys:
            marks = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT key, value FROM settings WHERE key IN ({marks})",
                keys,
            ).fetchall()
        else:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_settings(values: dict[str, str]) -> None:
    if not values:
        return
    with _cursor() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


def delete_settings(keys: list[str]) -> None:
    if not keys:
        return
    with _cursor() as conn:
        marks = ",".join("?" for _ in keys)
        conn.execute(f"DELETE FROM settings WHERE key IN ({marks})", keys)


# ---------- Accounts and sessions ----------


def _public_user_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    data.pop("password_hash", None)
    data.pop("session_expires_at", None)
    return data


def get_user(user_id: str) -> dict | None:
    """Full account row, including the password hash (internal use)."""
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (str(username).strip(),)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY role = 'admin' DESC, "
            "username COLLATE NOCASE ASC").fetchall()
    return [_public_user_row(row) for row in rows]


def count_active_admins(exclude_id: str | None = None) -> int:
    with _cursor() as conn:
        if exclude_id:
            row = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' "
                "AND active = 1 AND id != ?", (exclude_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' "
                "AND active = 1").fetchone()
    return int(row[0]) if row else 0


def create_user(username: str, password_hash: str, role: str = "reader",
                active: bool = True, created_by: str = "") -> dict:
    user_id = uuid.uuid4().hex
    ts = _now()
    with _cursor() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, active, "
            "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, username, password_hash, role, 1 if active else 0,
             created_by, ts, ts),
        )
    return get_user(user_id)


def update_user(user_id: str, patch: dict) -> dict | None:
    allowed = {"username", "role", "active", "password_hash",
               "failed_attempts", "locked", "last_login_at"}
    sets, values = [], []
    for key, value in patch.items():
        if key not in allowed:
            continue
        if key in ("active", "locked"):
            value = 1 if value else 0
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return get_user(user_id)
    sets.append("updated_at = ?")
    values.extend([_now(), user_id])
    with _cursor() as conn:
        changed = conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id = ?", values
        ).rowcount
    return get_user(user_id) if changed else None


def register_failed_login(user_id: str, limit: int | None = None) -> dict | None:
    """Increment the failure counter and lock at the configured threshold."""
    limit = max(1, int(limit or AUTH_MAX_FAILED_ATTEMPTS))
    with _cursor() as conn:
        row = conn.execute(
            "SELECT failed_attempts, locked FROM users WHERE id = ?",
            (user_id,)).fetchone()
        if row is None:
            return None
        attempts = int(row["failed_attempts"] or 0) + 1
        locked = bool(row["locked"]) or attempts >= limit
        conn.execute(
            "UPDATE users SET failed_attempts = ?, locked = ?, updated_at = ? "
            "WHERE id = ?", (attempts, 1 if locked else 0, _now(), user_id))
    return {"attempts": attempts, "locked": locked, "limit": limit}


def reset_login_failures(user_id: str) -> None:
    with _cursor() as conn:
        conn.execute(
            "UPDATE users SET failed_attempts = 0, locked = 0, updated_at = ? "
            "WHERE id = ?", (_now(), user_id))


def unlock_user(user_id: str) -> dict | None:
    return update_user(user_id, {"failed_attempts": 0, "locked": False})


def touch_login(user_id: str) -> None:
    with _cursor() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ?, failed_attempts = 0, "
            "locked = 0, updated_at = ? WHERE id = ?",
            (_now(), _now(), user_id))


def create_auth_session(token_hash: str, user_id: str,
                        expires_at: str) -> None:
    with _cursor() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, created_at, "
            "expires_at) VALUES (?,?,?,?)",
            (token_hash, user_id, _now(), expires_at))


def get_auth_session(token_hash: str) -> dict | None:
    """Return the active user behind a cookie token, or None."""
    with _cursor() as conn:
        row = conn.execute(
            "SELECT u.*, s.expires_at AS session_expires_at "
            "FROM auth_sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?", (token_hash,)).fetchone()
        if row is None:
            return None
        if row["session_expires_at"] <= _now() or not row["active"]:
            conn.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (token_hash,))
            return None
    return dict(row)


def delete_auth_session(token_hash: str) -> None:
    with _cursor() as conn:
        conn.execute(
            "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))


def delete_user_sessions(user_id: str, keep_token_hash: str | None = None) -> None:
    with _cursor() as conn:
        if keep_token_hash:
            conn.execute(
                "DELETE FROM auth_sessions WHERE user_id = ? "
                "AND token_hash != ?", (user_id, keep_token_hash))
        else:
            conn.execute(
                "DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))


def prune_user_sessions(user_id: str, keep: int = 5) -> None:
    with _cursor() as conn:
        conn.execute(
            "DELETE FROM auth_sessions WHERE user_id = ? AND token_hash NOT IN "
            "(SELECT token_hash FROM auth_sessions WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?)",
            (user_id, user_id, max(1, int(keep))))


def record_auth_event(user_id: str | None, username: str, action: str,
                      detail: str = "", ip: str | None = None) -> None:
    with _cursor() as conn:
        conn.execute(
            "INSERT INTO auth_audit (user_id, username, action, detail, ip, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (user_id, username or "", action, detail or "", ip, _now()),
        )
        conn.execute(
            "DELETE FROM auth_audit WHERE id NOT IN "
            "(SELECT id FROM auth_audit ORDER BY id DESC LIMIT 5000)")


def list_auth_audit(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM auth_audit ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(row) for row in rows]


# ---------- Events ----------

# Only these crossing events can affect attendance. Phone-use kinds never do.
ATTENDANCE_KINDS = ("entered", "left")

_EVENT_FIELDS = {
    "person_name", "kind", "confidence", "snapshot_path", "caption",
    "status", "telegram_chat_id", "telegram_message_id",
    "handled_by", "handled_by_id", "customer",
}


def _row_to_event(row: sqlite3.Row) -> dict:
    return dict(row)


def create_event(event_id: str, camera_id: str | None, camera_name: str,
                 track_id: int | None, person_name: str, kind: str,
                 confidence: float, snapshot_path: str | None,
                 caption: str, recognized_person_id: str | None = None,
                 recognized_person_name: str | None = None,
                 customer: bool = False) -> dict:
    ts = _now()
    with _cursor() as conn:
        conn.execute("""
            INSERT INTO events
              (id, camera_id, camera_name, track_id, person_name, kind,
               confidence, snapshot_path, caption, status, customer,
               recognized_person_id, recognized_person_name,
               created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?,?,?,?,?)
        """, (
            event_id, camera_id, camera_name, track_id, person_name, kind,
            float(confidence), snapshot_path, caption,
            1 if customer else 0,
            recognized_person_id, recognized_person_name, ts, ts,
        ))
    return get_event(event_id)


def get_event(event_id: str) -> dict | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
    return _row_to_event(row) if row else None


def delete_event(event_id: str) -> dict | None:
    """Remove an event and any attendance derived from it in one transaction."""
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM attendance WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return _row_to_event(row)


def delete_events_in_scope(scope: str) -> list[dict]:
    """Delete a dashboard category and its linked attendance atomically."""
    where = {
        "unassigned": "e.status = 'confirmed' AND e.kind IN ('entered', 'left') "
                      "AND a.id IS NULL",
        "attendance": "a.id IS NOT NULL",
        "customers": "e.customer = 1",
        "telegram": "1 = 1",
    }[scope]
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT DISTINCT e.* FROM events e "
            "LEFT JOIN attendance a ON a.event_id = e.id WHERE " + where
        ).fetchall()
        ids = [(row["id"],) for row in rows]
        if ids:
            conn.executemany("DELETE FROM attendance WHERE event_id = ?", ids)
            conn.executemany("DELETE FROM events WHERE id = ?", ids)
    return [_row_to_event(row) for row in rows]


def update_event(event_id: str, patch: dict) -> dict | None:
    sets, vals = [], []
    for key, value in patch.items():
        if key in _EVENT_FIELDS:
            sets.append(f"{key} = ?")
            vals.append(value)
    if not sets:
        return get_event(event_id)
    sets.append("updated_at = ?")
    vals.extend([_now(), event_id])
    with _cursor() as conn:
        conn.execute(
            f"UPDATE events SET {', '.join(sets)} WHERE id = ?", vals
        )
    return get_event(event_id)


def resolve_event_if_pending(event_id: str, status: str,
                             handled_by: str,
                             handled_by_id: str) -> dict | None:
    """Atomically transition pending -> status and record confirmed crossings.

    Returns None if the event was already handled. A confirmed entrance/exit
    event creates at most one attendance row. Unknown/unassigned crossings
    set no row, so they remain visible as events without changing attendance.
    """
    attendance = None
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None or row["status"] != "pending":
            return None
        cur = conn.execute(
            "UPDATE events SET status = ?, handled_by = ?, handled_by_id = ?, "
            "updated_at = ? WHERE id = ? AND status = 'pending'",
            (status, handled_by, handled_by_id, _now(), event_id),
        )
        if cur.rowcount == 0:
            return None

        if status == "confirmed" and row["kind"] in ATTENDANCE_KINDS:
            # An admin-selected person wins over automatic recognition. If
            # the selected person no longer exists, do not silently credit a
            # different (automatic) identity.
            person_id = row["specified_person_id"] or row["recognized_person_id"]
            person_name = (row["specified_person_name"]
                           or row["recognized_person_name"]
                           or row["person_name"])
            if person_id:
                person = conn.execute(
                    "SELECT name FROM persons WHERE id = ?",
                    (person_id,)).fetchone()
                if person is not None:
                    person_name = person["name"]
                    conn.execute("""
                        INSERT OR IGNORE INTO attendance
                          (event_id, person_id, person_name, kind, camera_id,
                           camera_name, occurred_at, recorded_at, handled_by,
                           handled_by_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    """, (
                        event_id, person_id, person_name, row["kind"],
                        row["camera_id"], row["camera_name"] or "",
                        row["created_at"], _now(), handled_by, handled_by_id,
                    ))
                    recorded = conn.execute(
                        "SELECT * FROM attendance WHERE event_id = ?",
                        (event_id,)).fetchone()
                    if recorded is not None:
                        attendance = dict(recorded)

    event = get_event(event_id)
    if event is not None and attendance is not None:
        event["_attendance"] = attendance
    return event


def find_person_id_by_name(name: str) -> str | None:
    """Resolve a recognised name only when it identifies one enrolled person."""
    name = (name or "").strip()
    if not name or name == "Unknown":
        return None
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT id FROM persons WHERE name = ?", (name,)).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def find_person_by_prefix(prefix: str) -> dict | None:
    """Resolve a callback-safe person id prefix, refusing ambiguous matches."""
    prefix = (prefix or "").strip().lower()
    if not 4 <= len(prefix) <= 32 or any(
            ch not in "0123456789abcdef" for ch in prefix):
        return None
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM persons WHERE id LIKE ? LIMIT 2",
            (prefix + "%",)).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def specify_event_person(event_id: str, person_id: str,
                         person_name: str) -> dict | None:
    """Attach an admin-selected person to a still-pending event."""
    with _cursor() as conn:
        cur = conn.execute(
            "UPDATE events SET specified_person_id = ?, "
            "specified_person_name = ?, person_name = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (person_id, person_name, person_name, _now(), event_id),
        )
        if cur.rowcount == 0:
            return None
    return get_event(event_id)


def clear_event_person(event_id: str) -> dict | None:
    """Undo an admin selection and restore the automatically recognised name."""
    with _cursor() as conn:
        row = conn.execute(
            "SELECT recognized_person_name FROM events "
            "WHERE id = ? AND status = 'pending'", (event_id,)).fetchone()
        if row is None:
            return None
        name = row["recognized_person_name"] or "Unknown"
        conn.execute(
            "UPDATE events SET specified_person_id = NULL, "
            "specified_person_name = NULL, person_name = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (name, _now(), event_id),
        )
    return get_event(event_id)


def set_event_customer(event_id: str, customer: bool) -> dict | None:
    """Set the customer tag on a still-pending entrance/exit alert."""
    with _cursor() as conn:
        cur = conn.execute(
            "UPDATE events SET customer = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending' AND kind IN (?, ?)",
            (1 if customer else 0, _now(), event_id, *ATTENDANCE_KINDS),
        )
        if cur.rowcount == 0:
            return None
    return get_event(event_id)


def _local_today_start_utc() -> str:
    """Start of the server's local day, expressed in the stored UTC format."""
    now_local = datetime.now().astimezone()
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).isoformat(timespec="seconds")


def attendance_overview(limit: int = 100) -> dict:
    """Enrolled people with their latest entrance/exit and today's counts."""
    limit = max(1, min(int(limit), 500))
    today = _local_today_start_utc()
    with _cursor() as conn:
        person_rows = conn.execute(
            "SELECT id, name, thumbnail_path, created_at FROM persons "
            "ORDER BY name COLLATE NOCASE ASC, created_at ASC"
        ).fetchall()
        latest = conn.execute("""
            WITH ranked AS (
                SELECT a.*, ROW_NUMBER() OVER (
                    PARTITION BY person_id
                    ORDER BY occurred_at DESC, id DESC) AS rn
                FROM attendance a)
            SELECT * FROM ranked WHERE rn = 1
        """).fetchall()
        latest_kind = conn.execute("""
            WITH ranked AS (
                SELECT a.*, ROW_NUMBER() OVER (
                    PARTITION BY person_id, kind
                    ORDER BY occurred_at DESC, id DESC) AS rn
                FROM attendance a)
            SELECT * FROM ranked WHERE rn = 1
        """).fetchall()
        today_rows = conn.execute(
            "SELECT person_id, kind, COUNT(*) AS n FROM attendance "
            "WHERE occurred_at >= ? GROUP BY person_id, kind",
            (today,)).fetchall()
        recent = conn.execute(
            "SELECT * FROM attendance ORDER BY occurred_at DESC, id DESC "
            "LIMIT ?", (limit,)).fetchall()
        unknown = conn.execute("""
            SELECT e.* FROM events e
            LEFT JOIN attendance a ON a.event_id = e.id
            WHERE e.status = 'confirmed' AND e.kind IN (?, ?)
              AND a.id IS NULL
            ORDER BY e.created_at DESC, e.id DESC LIMIT ?
        """, (*ATTENDANCE_KINDS, limit)).fetchall()

    def record(row):
        value = dict(row)
        value.pop("rn", None)
        return value

    latest_by_person = {row["person_id"]: record(row) for row in latest}
    latest_kind_by_person = {
        (row["person_id"], row["kind"]): record(row)
        for row in latest_kind}
    today_counts = {(row["person_id"], row["kind"]): int(row["n"])
                    for row in today_rows}

    people = []
    for row in person_rows:
        last = latest_by_person.get(row["id"])
        if last is None:
            status = "no_record"
        else:
            status = "inside" if last["kind"] == "entered" else "outside"
        people.append({
            "id": row["id"],
            "name": row["name"],
            "has_thumbnail": bool(row["thumbnail_path"]),
            "status": status,
            "last_event": last,
            "last_entrance": latest_kind_by_person.get(
                (row["id"], "entered")),
            "last_exit": latest_kind_by_person.get((row["id"], "left")),
            "today_entrances": today_counts.get((row["id"], "entered"), 0),
            "today_exits": today_counts.get((row["id"], "left"), 0),
        })

    return {
        "people": people,
        "summary": {
            "people": len(people),
            "inside": sum(1 for p in people if p["status"] == "inside"),
            "today_entrances": sum(p["today_entrances"] for p in people),
            "today_exits": sum(p["today_exits"] for p in people),
        },
        "unknown_events": [dict(row) for row in unknown],
        "recent": [dict(row) for row in recent],
    }


def _event_filter_sql(status: str | None, customer: bool | None,
                      kind: str | None):
    filters, values = [], []
    if status:
        filters.append("status = ?")
        values.append(status)
    if customer is not None:
        filters.append("customer = ?")
        values.append(1 if customer else 0)
    if kind:
        filters.append("kind = ?")
        values.append(kind)
    return (" WHERE " + " AND ".join(filters) if filters else ""), values


def list_events(status: str | None = None, limit: int = 100,
                offset: int = 0, customer: bool | None = None,
                kind: str | None = None) -> list[dict]:
    limit = max(0, min(int(limit), 500))
    offset = max(0, int(offset))
    where, values = _event_filter_sql(status, customer, kind)
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM events" + where +
            " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def count_events(status: str | None = None, customer: bool | None = None,
                 kind: str | None = None) -> int:
    where, values = _event_filter_sql(status, customer, kind)
    with _cursor() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM events" + where, values).fetchone()
    return int(row["n"]) if row else 0


def event_counts(customer: bool | None = None) -> dict[str, int]:
    where, values = _event_filter_sql(None, customer, None)
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM events" + where +
            " GROUP BY status", values).fetchall()
    counts = {"pending": 0, "confirmed": 0, "rejected": 0, "failed": 0}
    for row in rows:
        counts[row["status"]] = int(row["n"])
    return counts


def customer_summary() -> dict:
    """Customer-tagged crossing totals used by the Customers section."""
    today = _local_today_start_utc()
    with _cursor() as conn:
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM events WHERE customer = 1 "
            "GROUP BY status").fetchall()
        today_rows = conn.execute(
            "SELECT status, kind, COUNT(*) AS n FROM events "
            "WHERE customer = 1 AND created_at >= ? "
            "GROUP BY status, kind", (today,)).fetchall()

    counts = {"pending": 0, "confirmed": 0, "rejected": 0, "failed": 0}
    for row in status_rows:
        counts[row["status"]] = int(row["n"])

    today_entrances = today_exits = today_pending = 0
    for row in today_rows:
        n = int(row["n"])
        if row["status"] == "pending":
            today_pending += n
        elif row["status"] == "confirmed":
            if row["kind"] == "entered":
                today_entrances += n
            elif row["kind"] == "left":
                today_exits += n

    return {
        "today_entrances": today_entrances,
        "today_exits": today_exits,
        "today_pending": today_pending,
        "pending": counts["pending"],
        "confirmed": counts["confirmed"],
        "rejected": counts["rejected"],
        "total": sum(counts.values()),
        "counts": counts,
    }


def dashboard_overview(limit: int = 8) -> dict:
    """Aggregated counters and short lists for the Overview section."""
    limit = max(1, min(int(limit), 50))
    today = _local_today_start_utc()
    with _cursor() as conn:
        people = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        inside = conn.execute("""
            WITH ranked AS (
                SELECT person_id, kind, ROW_NUMBER() OVER (
                    PARTITION BY person_id
                    ORDER BY occurred_at DESC, id DESC) AS rn
                FROM attendance)
            SELECT COUNT(*) FROM ranked WHERE rn = 1 AND kind = 'entered'
        """).fetchone()[0]
        today_rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM attendance "
            "WHERE occurred_at >= ? GROUP BY kind", (today,)).fetchall()
        recent_attendance = conn.execute(
            "SELECT * FROM attendance ORDER BY occurred_at DESC, id DESC "
            "LIMIT ?", (limit,)).fetchall()

    today_counts = {row["kind"]: int(row["n"]) for row in today_rows}
    return {
        "attendance": {
            "people": int(people),
            "inside": int(inside),
            "today_entrances": today_counts.get("entered", 0),
            "today_exits": today_counts.get("left", 0),
        },
        "customers": customer_summary(),
        "events": event_counts(),
        "recent_events": list_events(None, limit=limit),
        "pending_events": list_events("pending", limit=limit),
        "recent_customers": list_events(None, limit=limit, customer=True),
        "recent_attendance": [dict(row) for row in recent_attendance],
    }
