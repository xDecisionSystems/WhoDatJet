"""Periodic health checks (`whodatjet watchdog`, run every 10 minutes by whodatjet-watchdog.timer).

Each check maps to an alert key; a failing check calls alerts.fire (de-duplicated), a passing one calls
alerts.resolve (one "resolved" message). Checks never raise: a broken check is reported as its own problem.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from whodatjet import alerts, coverage, db
from whodatjet import status as status_mod
from whodatjet.config import Config

log = logging.getLogger(__name__)
DISK_MIN_FREE_PCT = 15.0
DISK_MIN_DAYS = 60
LOGIN_FAILURES = 5
LOGIN_WINDOW_MIN = 15
DEFAULT_PASSWORD_GRACE_H = 24


@dataclass
class Check:
    key: str
    event: str
    problem: str | None  # None = healthy
    ok_detail: str = ""


def check_stale(cfg: Config, today: date) -> Check:
    try:
        conn = db.connect_reader(cfg.registry_db_path)
    except Exception:
        return Check("data_stale", "data_stale", None)  # no registry yet: nothing to be stale
    try:
        dates = coverage.coverage_dates(conn)
    finally:
        conn.close()
    if not dates:
        return Check("data_stale", "data_stale", None)
    latest = dates[-1]
    if coverage.is_stale(latest, today, cfg.stale_days):
        return Check("data_stale", "data_stale", f"latest verified coverage is {latest} ({(today - latest).days} "
                     f"days ago; threshold {cfg.stale_days})", "Verified coverage is current again.")
    return Check("data_stale", "data_stale", None, "Verified coverage is current again.")


def check_disk(cfg: Config) -> Check:
    try:
        du = shutil.disk_usage(cfg.data_dir)
    except OSError as exc:
        return Check("disk_low", "disk_low", f"cannot read data volume usage: {exc}")
    pct = 100 * du.free / du.total
    days = status_mod.disk_days_remaining(cfg)
    if pct < DISK_MIN_FREE_PCT:
        return Check("disk_low", "disk_low", f"data volume has {pct:.1f}% free (minimum {DISK_MIN_FREE_PCT:.0f}%)",
                     "Disk space is back above the threshold.")
    if days is not None and days < DISK_MIN_DAYS:
        return Check("disk_low", "disk_low", f"projected {days} days until the data volume is full "
                     f"(minimum {DISK_MIN_DAYS})", "Disk space is back above the threshold.")
    return Check("disk_low", "disk_low", None, "Disk space is back above the threshold.")


def check_archive_store(cfg: Config) -> Check:
    h = status_mod.archive_health(cfg)
    if h["ok"]:
        return Check("archive_store", "archive_store", None, "The archive store is reachable again.")
    return Check("archive_store", "archive_store", f"archive store {h['location']} is not usable ({h['error']})",
                 "The archive store is reachable again.")


def check_coverage_gap(cfg: Config) -> Check:
    try:
        conn = db.connect_reader(cfg.registry_db_path)
    except Exception:
        return Check("coverage_gap", "coverage_gap", None)
    try:
        gaps = coverage.find_gaps(coverage.coverage_dates(conn), cfg.gap_threshold)
    finally:
        conn.close()
    if gaps:
        g = gaps[-1]
        return Check("coverage_gap", "coverage_gap", f"{len(gaps)} gap(s); latest {g.after} .. {g.before} "
                     f"({g.missed_publication_days} publication days missed)", "No coverage gaps remain.")
    return Check("coverage_gap", "coverage_gap", None, "No coverage gaps remain.")


def check_http(name: str, url: str, state: dict[str, int], *, transport: httpx.BaseTransport | None = None,
               strikes: int = 2) -> Check:
    """Healthy unless the URL fails `strikes` runs in a row (a single blip is not an alert)."""
    key = f"api_unhealthy:{name}"
    try:
        with httpx.Client(timeout=5, transport=transport) as client:
            ok = client.get(url).status_code == 200
        err = "" if ok else "unexpected status"
    except httpx.HTTPError as exc:
        ok, err = False, type(exc).__name__
    state[key] = 0 if ok else state.get(key, 0) + 1
    if state[key] >= strikes:
        return Check(key, "api_unhealthy", f"{name} health check failed {state[key]} times in a row ({err})",
                     f"{name} is healthy again.")
    return Check(key, "api_unhealthy", None, f"{name} is healthy again.")


def check_security(cfg: Config, now: datetime) -> list[Check]:
    out: list[Check] = []
    try:
        conn = db.connect_reader(cfg.admin_db_path)
    except Exception:
        return out
    try:
        since = (now - timedelta(minutes=LOGIN_WINDOW_MIN)).isoformat(timespec="seconds")
        fails = conn.execute("SELECT count(*) FROM audit_log WHERE action = 'login_failed' AND at >= ?",
                             (since,)).fetchone()[0]
        out.append(Check("security:login_failures", "security",
                         f"{fails} failed admin logins in the last {LOGIN_WINDOW_MIN} minutes"
                         if fails >= LOGIN_FAILURES else None, "Failed admin logins have stopped."))
        row = conn.execute("SELECT created_at FROM admin_user WHERE username = 'admin' AND must_change_password = 1 "
                           "AND disabled_at IS NULL").fetchone()
        stale_default = bool(row) and now - datetime.fromisoformat(row["created_at"]) > timedelta(
            hours=DEFAULT_PASSWORD_GRACE_H)
        out.append(Check("security:default_password", "security",
                         f"the default admin password has not been changed {DEFAULT_PASSWORD_GRACE_H} hours "
                         "after installation" if stale_default else None, "The default admin password was changed."))
    except Exception:
        log.exception("security check failed")
    finally:
        conn.close()
    return out


def run(cfg: Config, *, now: datetime | None = None, today: date | None = None, api_url: str | None = None,
        admin_url: str | None = None, transport: httpx.BaseTransport | None = None,
        state_file: Path | None = None) -> list[Check]:
    from whodatjet.faa_client import CHICAGO

    now = now or datetime.now(UTC)
    today = today or datetime.now(CHICAGO).date()
    state_path = state_file or cfg.data_dir / "watchdog-state.json"
    state: dict[str, int] = {}
    try:
        import json
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        pass
    checks: list[Check] = [check_stale(cfg, today), check_coverage_gap(cfg), check_disk(cfg),
                           check_archive_store(cfg)]
    checks.append(check_http("API", api_url or f"http://127.0.0.1:{cfg.api_port}/healthz", state, transport=transport))
    if admin_url:
        checks.append(check_http("admin console", admin_url, state, transport=transport))
    checks.extend(check_security(cfg, now))
    try:
        import json
        state_path.write_text(json.dumps(state))
    except OSError:
        log.warning("could not save watchdog state to %s", state_path)
    for c in checks:
        try:
            if c.problem:
                alerts.fire(cfg.admin_db_path, c.key, c.event, c.problem, now=now, transport=transport)
            else:
                alerts.resolve(cfg.admin_db_path, c.key, c.ok_detail, now=now, transport=transport)
        except Exception:
            log.exception("could not process check %s", c.key)
    return checks
