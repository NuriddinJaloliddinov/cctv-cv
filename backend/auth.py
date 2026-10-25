"""Authentication helpers: accounts, roles, sessions and cookie policy."""
import re
from datetime import datetime, timedelta, timezone

from . import db
from .config import AUTH_SECURE_COOKIE, AUTH_SESSION_HOURS
from .security import new_session_token, token_digest

SESSION_COOKIE = "cctv_session"
ROLE_ADMIN = "admin"
ROLE_READER = "reader"
ROLES = (ROLE_ADMIN, ROLE_READER)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")


def normalize_username(value: str) -> str:
    username = str(value or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "Username must be 3-32 characters: letters, numbers, dot, dash "
            "or underscore, starting with a letter or number.")
    return username


def public_user(user: dict | None) -> dict | None:
    if user is None:
        return None
    expires = user.get("session_expires_at")
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "active": bool(user.get("active")),
        "locked": bool(user.get("locked")),
        "failed_attempts": int(user.get("failed_attempts") or 0),
        "created_by": user.get("created_by") or "",
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "last_login_at": user.get("last_login_at"),
        "session_expires_at": expires,
    }


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    return db.get_auth_session(token_digest(token))


def create_session(user: dict) -> tuple[str, datetime]:
    token = new_session_token()
    expires = datetime.now(timezone.utc) + timedelta(
        hours=max(0.25, float(AUTH_SESSION_HOURS)))
    db.create_auth_session(token_digest(token), user["id"],
                           expires.isoformat(timespec="seconds"))
    db.prune_user_sessions(user["id"])
    return token, expires


def is_secure_request(scheme: str, forwarded_proto: str | None = None) -> bool:
    if AUTH_SECURE_COOKIE in ("1", "true", "yes", "on"):
        return True
    if AUTH_SECURE_COOKIE in ("0", "false", "no", "off"):
        return False
    return (scheme or "").lower() == "https" or \
        (forwarded_proto or "").lower() == "https"


def is_admin(user: dict | None) -> bool:
    return bool(user and user.get("role") == ROLE_ADMIN)


def is_reader(user: dict | None) -> bool:
    return bool(user and user.get("role") == ROLE_READER)


def request_ip(request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0]
    return (forwarded.strip() or
            (request.client.host if request.client else ""))


def safe_next(value: str | None) -> str:
    """Only allow an absolute same-site path as a post-login redirect."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value
