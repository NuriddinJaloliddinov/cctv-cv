"""Password hashing and opaque session-token helpers.

Only the Python standard library is used. Passwords are stored as PBKDF2-
HMAC-SHA256 with a random per-password salt. Session cookies contain a random
token; only its SHA-256 digest is stored in SQLite.
"""
import base64
import hashlib
import hmac
import secrets

from .config import AUTH_HASH_ITERATIONS

_ALGORITHM = "pbkdf2_sha256"
_SALT_BYTES = 16
_TOKEN_BYTES = 32


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    """Return a self-describing, salted password hash."""
    salt = secrets.token_bytes(_SALT_BYTES)
    iterations = max(10_000, int(AUTH_HASH_ITERATIONS))
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time password check against a stored hash."""
    try:
        algorithm, iterations, salt, digest = str(stored).split("$")
        if algorithm != _ALGORITHM:
            return False
        expected = _unb64(digest)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", str(password).encode("utf-8"), _unb64(salt),
            int(iterations))
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(candidate, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


# A valid-looking hash used for unknown usernames so a failed login costs the
# same time whether or not the account exists.
_DUMMY_HASH = hash_password("not-a-real-password")


def dummy_verify(password: str) -> None:
    verify_password(password, _DUMMY_HASH)
