"""Alert sender (plan: "Slack notifications").

Phase 5 scope: every alert is logged; if a Slack incoming-webhook URL is configured it is also posted,
with retries. The webhook is read from admin.db settings (written by the admin console, Phase 6) or,
until then, from WHODATJET_SLACK_WEBHOOK_URL. De-duplication and "resolved" messages come with the
admin console's alert_state table (Phase 6).

Messages never contain API keys, webhook URLs, or registrant data.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import sqlite3
import time
from pathlib import Path

import httpx

from whodatjet import __version__

log = logging.getLogger(__name__)
WEBHOOK_RE = re.compile(r"^https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+$")
EVENTS = {
    "ingest_failed": "Ingest failed",
    "format_changed": "FAA format changed",
    "data_stale": "Data stale",
    "coverage_gap": "Coverage gap",
    "api_unhealthy": "API unhealthy",
    "disk_low": "Disk space low",
    "archive_store": "Archive store problem",
    "update_result": "Update result",
    "security": "Security",
    "key_activity": "Key activity",
    "unit_failed": "Service failed",
    "test": "Test message",
}


def valid_webhook(url: str) -> bool:
    return bool(WEBHOOK_RE.match(url))


def webhook_url(admin_db: Path | None) -> str | None:
    if admin_db is not None and admin_db.exists():
        try:
            conn = sqlite3.connect(f"file:{admin_db}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT value FROM setting WHERE key = 'slack_webhook_url'").fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return str(row[0])
        except sqlite3.Error:
            pass  # settings table arrives in Phase 6
    url = os.environ.get("WHODATJET_SLACK_WEBHOOK_URL", "").strip()
    return url or None


def format_message(event: str, detail: str, *, dashboard_url: str | None = None, resolved: bool = False) -> str:
    title = EVENTS.get(event, event)
    head = (f":white_check_mark: *WhoDatJet — resolved: {title}*" if resolved else
            f":rotating_light: *WhoDatJet — {title}*" if event != "test" else "*WhoDatJet — test message*")
    lines = [head,
             f"host `{socket.gethostname()}` · version `{__version__}`", detail.strip()[:1500]]
    if dashboard_url:
        lines.append(f"<{dashboard_url}|Admin dashboard>")
    return "\n".join(line for line in lines if line)


def send(event: str, detail: str, *, admin_db: Path | None = None, transport: httpx.BaseTransport | None = None,
         attempts: int = 3, backoff_s: float = 2.0, resolved: bool = False) -> bool:
    """Log the alert and post it to Slack if configured. Returns True if posted (or nothing to post to)."""
    log.warning("%s: %s", "resolved" if resolved else "alert", detail,
                extra={"event": "alert", "alert": event})
    url = webhook_url(admin_db)
    if not url:
        return True
    if not valid_webhook(url):
        log.error("configured Slack webhook URL is not a hooks.slack.com URL; not sending")
        return False
    text = format_message(event, detail, dashboard_url=os.environ.get("WHODATJET_ADMIN_URL") or None,
                          resolved=resolved)
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=10, transport=transport) as client:
                r = client.post(url, json={"text": text})
            if r.status_code == 200:
                return True
            log.warning("Slack returned HTTP %d (attempt %d/%d)", r.status_code, attempt, attempts)
        except httpx.HTTPError as exc:
            log.warning("Slack post failed (attempt %d/%d): %s", attempt, attempts, type(exc).__name__)
        if attempt < attempts:
            time.sleep(backoff_s * 2 ** (attempt - 1))
    log.error("Slack unreachable after %d attempts; alert logged only", attempts)
    return False
