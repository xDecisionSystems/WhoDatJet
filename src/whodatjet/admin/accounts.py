"""Admin accounts, sessions, lockout, CSRF and the audit log (plan: "Admin authentication").

Passwords are argon2id. Session cookies carry a random token; only its SHA-256 is stored. The CSRF token of
a session is an HMAC of the session token, so it needs no storage and cannot be guessed without the cookie.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "admin"
MIN_PASSWORD = 12
MAX_FAILED = 5
LOCK_MINUTES = 15
IDLE_HOURS = 8

_hasher = PasswordHasher()
# Verified against when a username does not exist, so unknown users cost the same time as known ones.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


class AccountError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str | None = None, **detail) -> None:
    conn.execute("INSERT INTO audit_log (at, actor, action, target, detail_json) VALUES (?,?,?,?,?)",
                 (iso(now()), actor, action, target, json.dumps(detail, sort_keys=True) if detail else None))


def validate_password(username: str, password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"the password must be at least {MIN_PASSWORD} characters")
    if password.lower() == DEFAULT_PASSWORD or username.lower() in password.lower():
        raise AccountError("the password cannot be 'admin' or contain the username")


def _hash(password: str) -> str:
    return _hasher.hash(password)


def ensure_default_admin(conn: sqlite3.Connection) -> bool:
    """Create `admin` / `admin` (password change forced) when no account exists. Returns True if created."""
    if conn.execute("SELECT 1 FROM admin_user LIMIT 1").fetchone():
        return False
    conn.execute("INSERT INTO admin_user (username, password_hash, must_change_password, created_at, created_by) "
                 "VALUES (?,?,1,?,'installer')", (DEFAULT_USER, _hash(DEFAULT_PASSWORD), iso(now())))
    audit(conn, "installer", "admin_created", DEFAULT_USER, default_account=True)
    return True


def create_admin(conn: sqlite3.Connection, username: str, temp_password: str, *, created_by: str) -> None:
    username = username.strip()
    if not username or len(username) > 64 or not username.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise AccountError("usernames use letters, digits, '.', '-' and '_' (at most 64)")
    if len(temp_password) < 8:
        raise AccountError("the temporary password must be at least 8 characters")
    try:
        conn.execute("INSERT INTO admin_user (username, password_hash, must_change_password, created_at, created_by) "
                     "VALUES (?,?,1,?,?)", (username, _hash(temp_password), iso(now()), created_by))
    except sqlite3.IntegrityError as exc:
        raise AccountError(f"user {username!r} already exists") from exc
    audit(conn, created_by, "admin_created", username)


def reset_password(conn: sqlite3.Connection, username: str, *, actor: str) -> bool:
    """CLI recovery: password back to `admin`, change forced, lock cleared, sessions dropped."""
    row = conn.execute("SELECT id FROM admin_user WHERE username = ?", (username,)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE admin_user SET password_hash = ?, must_change_password = 1, failed_attempts = 0, "
                 "locked_until = NULL, disabled_at = NULL WHERE id = ?", (_hash(DEFAULT_PASSWORD), row["id"]))
    conn.execute("DELETE FROM admin_session WHERE admin_user_id = ?", (row["id"],))
    audit(conn, actor, "password_reset", username)
    return True


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    user_id: int | None = None
    username: str = ""
    locked: bool = False


def login(conn: sqlite3.Connection, username: str, password: str, *, remote: str = "") -> LoginResult:
    t = now()
    row = conn.execute("SELECT * FROM admin_user WHERE username = ?", (username,)).fetchone()
    if row is None:
        with contextlib.suppress(VerificationError):
            _hasher.verify(_DUMMY_HASH, password)
        audit(conn, username[:64] or "-", "login_failed", None, reason="unknown_user", remote=remote)
        return LoginResult(False)
    if row["disabled_at"]:
        audit(conn, username, "login_failed", None, reason="disabled", remote=remote)
        return LoginResult(False)
    if row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > t:
        audit(conn, username, "login_failed", None, reason="locked", remote=remote)
        return LoginResult(False, locked=True)
    try:
        _hasher.verify(row["password_hash"], password)
        good = True
    except (VerificationError, InvalidHashError):
        good = False
    if not good:
        attempts = row["failed_attempts"] + 1
        locked_until = iso(t + timedelta(minutes=LOCK_MINUTES)) if attempts >= MAX_FAILED else None
        conn.execute("UPDATE admin_user SET failed_attempts = ?, locked_until = COALESCE(?, locked_until) WHERE id = ?",
                     (0 if locked_until else attempts, locked_until, row["id"]))
        audit(conn, username, "login_failed", None, reason="bad_password", remote=remote,
              locked=bool(locked_until))
        return LoginResult(False, locked=bool(locked_until))
    conn.execute("UPDATE admin_user SET failed_attempts = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
                 (iso(t), row["id"]))
    audit(conn, username, "login", None, remote=remote)
    return LoginResult(True, row["id"], row["username"])


def change_password(conn: sqlite3.Connection, user_id: int, current: str, new: str, *,
                    keep_token: str | None = None) -> None:
    row = conn.execute("SELECT * FROM admin_user WHERE id = ?", (user_id,)).fetchone()
    try:
        _hasher.verify(row["password_hash"], current)
    except (VerificationError, InvalidHashError) as exc:
        raise AccountError("the current password is wrong") from exc
    validate_password(row["username"], new)
    if new == current:
        raise AccountError("choose a different password")
    conn.execute("UPDATE admin_user SET password_hash = ?, must_change_password = 0, password_changed_at = ? "
                 "WHERE id = ?", (_hash(new), iso(now()), user_id))
    keep = hash_token(keep_token) if keep_token else ""
    conn.execute("DELETE FROM admin_session WHERE admin_user_id = ? AND token_hash != ?", (user_id, keep))
    audit(conn, row["username"], "password_changed", row["username"])


def set_disabled(conn: sqlite3.Connection, username: str, disabled: bool, *, actor: str) -> None:
    row = conn.execute("SELECT id, disabled_at FROM admin_user WHERE username = ?", (username,)).fetchone()
    if row is None:
        raise AccountError("no such user")
    if disabled:
        active = conn.execute("SELECT count(*) FROM admin_user WHERE disabled_at IS NULL AND id != ?",
                              (row["id"],)).fetchone()[0]
        if active == 0:
            raise AccountError("cannot disable the last active admin")
        conn.execute("UPDATE admin_user SET disabled_at = ? WHERE id = ?", (iso(now()), row["id"]))
        conn.execute("DELETE FROM admin_session WHERE admin_user_id = ?", (row["id"],))
    else:
        conn.execute("UPDATE admin_user SET disabled_at = NULL, failed_attempts = 0, locked_until = NULL "
                     "WHERE id = ?", (row["id"],))
    audit(conn, actor, "admin_disabled" if disabled else "admin_enabled", username)


# ------------------------------------------------------------------ sessions

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(session_token: str) -> str:
    return hmac.new(session_token.encode(), b"whodatjet-csrf", hashlib.sha256).hexdigest()


def csrf_ok(session_token: str, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(csrf_token(session_token), presented or "")


@dataclass(frozen=True)
class Session:
    token: str
    user_id: int
    username: str
    must_change_password: bool


def new_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    t = now()
    expires = iso(t + timedelta(hours=IDLE_HOURS))
    conn.execute("INSERT INTO admin_session (token_hash, admin_user_id, created_at, last_seen_at, expires_at) "
                 "VALUES (?,?,?,?,?)", (hash_token(token), user_id, iso(t), iso(t), expires))
    conn.execute("DELETE FROM admin_session WHERE expires_at < ?", (iso(t),))
    return token


def get_session(conn: sqlite3.Connection, token: str | None) -> Session | None:
    if not token:
        return None
    t = now()
    row = conn.execute(
        "SELECT s.expires_at, u.id, u.username, u.must_change_password, u.disabled_at FROM admin_session s "
        "JOIN admin_user u ON u.id = s.admin_user_id WHERE s.token_hash = ?", (hash_token(token),)).fetchone()
    if row is None or row["disabled_at"] or datetime.fromisoformat(row["expires_at"]) <= t:
        return None
    conn.execute("UPDATE admin_session SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                 (iso(t), iso(t + timedelta(hours=IDLE_HOURS)), hash_token(token)))
    return Session(token, row["id"], row["username"], bool(row["must_change_password"]))


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM admin_session WHERE token_hash = ?", (hash_token(token),))
