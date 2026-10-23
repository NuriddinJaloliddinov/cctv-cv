"""SQLite persistence: cameras, persons, face embeddings."""
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .config import DATA_DIR
from .models import CameraInfo

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
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_cameras_address "
                "ON cameras(address)"
            )

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
    except sqlite3.DatabaseError:
        path = Path(DB_PATH)
        if path.exists():
            backup = path.with_name(
                f"{path.stem}.corrupt-{int(datetime.now().timestamp())}{path.suffix}"
            )
            path.rename(backup)
        init_db()


# ---------- Cameras ----------

def _row_to_camera(row: sqlite3.Row) -> CameraInfo:
    return CameraInfo(**{k: row[k] for k in row.keys()})


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
               created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
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
            ts, ts,
        ))
    return get_camera(camera_id)


def update_camera(camera_id: str, patch: dict) -> CameraInfo | None:
    if not patch:
        return get_camera(camera_id)
    allowed = {"name", "address", "rtsp_url", "source_type",
               "onvif_user", "onvif_pass", "device", "fps"}
    sets, vals = [], []
    for k, v in patch.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        vals.append(int(v) if k == "fps" and v is not None else v)
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