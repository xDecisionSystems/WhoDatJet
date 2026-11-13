"""Alert state, settings and de-duplication (plan: "Slack notifications").

Each condition has an `alert_key` and an `alert_state` row. While it persists it is re-sent at most once per
repeat interval (default 6 h); when it clears, one "resolved" message is sent. Events can be switched off
individually; a disabled event is still recorded (so the dashboard shows it) but never posted.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from whodatjet import db, notify

DEFAULT_ON = {"ingest_failed", "format_changed", "data_stale", "coverage_gap", "api_unhealthy", "disk_low",
              "archive_store", "update_result", "security", "unit_failed"}
TOGGLEABLE = [e for e in notify.EVENTS if e not in ("test", "unit_failed")]
DEFAULT_REPEAT_HOURS = 6


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str, actor: str) -> None:
    conn.execute("INSERT INTO setting (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                 "updated_by = excluded.updated_by", (key, value, _iso(_now()), actor))


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM setting WHERE key = ?", (key,))


def event_enabled(conn: sqlite3.Connection, event: str) -> bool:
    if event == "test":
        return True
    value = get_setting(conn, f"event.{event}")
    return (event in DEFAULT_ON) if value is None else value == "1"


def repeat_hours(conn: sqlite3.Connection) -> float:
    try:
        return max(0.01, float(get_setting(conn, "repeat_hours", str(DEFAULT_REPEAT_HOURS)) or DEFAULT_REPEAT_HOURS))
    except ValueError:
        return float(DEFAULT_REPEAT_HOURS)


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM alert_state WHERE resolved_at IS NULL ORDER BY first_fired_at").fetchall()


def _open(admin_db: Path) -> sqlite3.Connection | None:
    try:
        conn = db.connect_writer(admin_db)
        db.migrate(conn, db.load_migrations("admin"))
        return conn
    except (sqlite3.Error, OSError):
        return None


def fire(admin_db: Path, alert_key: str, event: str, detail: str, *, now: datetime | None = None,
         transport: httpx.BaseTransport | None = None, attempts: int = 3, backoff_s: float = 2.0) -> bool:
    """Record the condition and post it unless suppressed. Returns True if a message was sent."""
    t = now or _now()
    conn = _open(admin_db)
    if conn is None:  # admin.db unusable: still log/post best-effort, with no de-duplication
        return notify.send(event, detail, admin_db=admin_db, transport=transport, attempts=attempts,
                           backoff_s=backoff_s)
    try:
        row = conn.execute("SELECT * FROM alert_state WHERE alert_key = ?", (alert_key,)).fetchone()
        enabled = event_enabled(conn, event)
        send_now = False
        if row is None or row["resolved_at"] is not None:
            conn.execute("INSERT INTO alert_state (alert_key, event, first_fired_at, last_sent_at, resolved_at, "
                         "detail) VALUES (?,?,?,NULL,NULL,?) "
                         "ON CONFLICT (alert_key) DO UPDATE SET event = excluded.event, "
                         "first_fired_at = excluded.first_fired_at, last_sent_at = NULL, resolved_at = NULL, "
                         "detail = excluded.detail", (alert_key, event, _iso(t), detail))
            send_now = enabled
        else:
            conn.execute("UPDATE alert_state SET detail = ? WHERE alert_key = ?", (detail, alert_key))
            last = datetime.fromisoformat(row["last_sent_at"]) if row["last_sent_at"] else None
            due = last is None or t - last >= timedelta(hours=repeat_hours(conn))
            send_now = enabled and due
        if not send_now:
            return False
        ok = notify.send(event, detail, admin_db=admin_db, transport=transport, attempts=attempts,
                         backoff_s=backoff_s)
        if ok:  # a failed post is retried on the next call instead of waiting a full repeat interval
            conn.execute("UPDATE alert_state SET last_sent_at = ? WHERE alert_key = ?", (_iso(t), alert_key))
        return ok
    finally:
        conn.close()


def resolve(admin_db: Path, alert_key: str, detail: str = "", *, now: datetime | None = None,
            transport: httpx.BaseTransport | None = None, attempts: int = 3, backoff_s: float = 2.0) -> bool:
    """Mark a condition cleared; sends one "resolved" message if the alert had been posted. Cheap when idle."""
    if not admin_db.exists():
        return False
    conn = _open(admin_db)
    if conn is None:
        return False
    try:
        row = conn.execute("SELECT * FROM alert_state WHERE alert_key = ? AND resolved_at IS NULL",
                           (alert_key,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE alert_state SET resolved_at = ? WHERE alert_key = ?", (_iso(now or _now()), alert_key))
        if row["last_sent_at"] and event_enabled(conn, row["event"]):
            return notify.send(row["event"], detail or row["detail"] or "", admin_db=admin_db, transport=transport,
                               attempts=attempts, backoff_s=backoff_s, resolved=True)
        return False
    finally:
        conn.close()
