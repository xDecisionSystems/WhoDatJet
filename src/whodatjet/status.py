"""Operator status summary (`whodatjet status`)."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date
from pathlib import Path

from whodatjet import __version__, coverage, db
from whodatjet.config import Config


def _size(p: Path) -> int | None:
    return p.stat().st_size if p.exists() else None


def archive_health(cfg: Config) -> dict:
    """Location and reachability of the primary archive store (cheap: one HEAD request / stat)."""
    from whodatjet.archive_store import open_store
    from whodatjet.config import ConfigError

    try:
        store = open_store(cfg)
    except ConfigError as exc:
        return {"kind": cfg.archive_backend, "location": "not configured", "ok": False, "error": str(exc)}
    try:
        if store.kind == "filesystem":
            ok = store.root.is_dir()  # type: ignore[attr-defined]
        else:
            store.exists(".whodatjet-canary")  # raises on network/credential errors; False just means absent
            ok = True
        return {"kind": store.kind, "location": store.location, "ok": ok, "error": None if ok else "missing"}
    except Exception as exc:  # any failure to reach the store counts as unreachable
        return {"kind": store.kind, "location": store.location, "ok": False, "error": type(exc).__name__}


def disk_days_remaining(cfg: Config) -> int | None:
    """Days until the data volume fills, from the growth of a filesystem archive store; None if unknowable."""
    if cfg.archive_backend != "filesystem":
        return None
    try:
        archives = [p for p in cfg.archive_dir.glob("*/*.zip")]
        if len(archives) < 2:
            return None
        times = sorted(p.stat().st_mtime for p in archives)
        span_days = max(1.0, (times[-1] - times[0]) / 86400)
        per_day = sum(p.stat().st_size for p in archives) / span_days
        return int(shutil.disk_usage(cfg.data_dir).free / per_day) if per_day > 0 else None
    except OSError:
        return None


def gather(cfg: Config, today: date) -> dict:
    out: dict = {"version": __version__, "profile": cfg.profile, "data_dir": str(cfg.data_dir),
                 "archive_backend": cfg.archive_backend, "api_workers": cfg.api_workers,
                 "registry_db_bytes": _size(cfg.registry_db_path), "admin_db_bytes": _size(cfg.admin_db_path)}
    try:
        du = shutil.disk_usage(cfg.data_dir)
        out["disk_free_pct"] = round(100 * du.free / du.total, 1)
        out["disk_free_gb"] = round(du.free / 1e9, 1)
    except OSError:
        pass
    try:
        conn = db.connect_reader(cfg.registry_db_path)
    except (FileNotFoundError, sqlite3.Error) as exc:
        out["registry"] = f"unavailable: {exc}"
        return out
    try:
        dates = coverage.coverage_dates(conn)
        latest = dates[-1] if dates else None
        out.update(
            coverage_start=dates[0].isoformat() if dates else None,
            coverage_end=latest.isoformat() if latest else None,
            data_age_days=(today - latest).days if latest else None,
            stale=coverage.is_stale(latest, today, cfg.stale_days),
            snapshots=len(dates),
            open_gaps=[{"after": g.after.isoformat(), "before": g.before.isoformat(),
                        "missed": g.missed_publication_days} for g in coverage.find_gaps(dates, cfg.gap_threshold)],
        )
        last = conn.execute("SELECT started_at, status, error_message FROM ingest_run WHERE status != 'running' "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        out["last_run"] = dict(last) if last else None
        out["failed_runs_7d"] = conn.execute("SELECT count(*) FROM ingest_run WHERE status = 'failed' "
                                             "AND run_date >= date('now', '-7 day')").fetchone()[0]
        events = conn.execute("SELECT kind, count(*) FROM identity_event GROUP BY kind").fetchall()
        out["identity_events"] = dict(events)
    finally:
        conn.close()
    try:
        adm = db.connect_reader(cfg.admin_db_path)
        try:
            out["active_api_keys"] = adm.execute("SELECT count(*) FROM api_key WHERE revoked_at IS NULL AND "
                                                 "(expires_at IS NULL OR expires_at >= ?)",
                                                 (today.isoformat(),)).fetchone()[0]
            m = adm.execute("SELECT COALESCE(SUM(requests),0), MAX(p95_ms), COALESCE(SUM(status_503),0) "
                            "FROM api_metrics_minute WHERE minute >= strftime('%Y-%m-%dT%H:%M','now','-1 day')"
                            ).fetchone()
            out.update(requests_24h=m[0], p95_ms_24h=m[1], errors_503_24h=m[2])
        finally:
            adm.close()
    except (FileNotFoundError, sqlite3.Error):
        out["active_api_keys"] = None
    return out


def render(s: dict) -> str:
    lines = [f"WhoDatJet {s['version']}  profile={s['profile']}  workers={s['api_workers']}  "
             f"archives={s['archive_backend']}"]
    if isinstance(s.get("registry"), str):
        lines.append(f"registry: {s['registry']}")
    else:
        stale = "STALE" if s.get("stale") else "fresh"
        lines.append(f"coverage: {s.get('coverage_start')} .. {s.get('coverage_end')}  ({s.get('snapshots')} "
                     f"snapshots, age {s.get('data_age_days')} d, {stale})")
        gaps = s.get("open_gaps") or []
        lines.append("gaps: " + (", ".join(f"{g['after']}..{g['before']} ({g['missed']} missed)" for g in gaps)
                                 if gaps else "none"))
        lr = s.get("last_run") or {}
        lines.append(f"last run: {lr.get('status', '-')} at {lr.get('started_at', '-')}"
                     + (f"  error: {lr['error_message']}" if lr.get("error_message") else ""))
        lines.append(f"failed runs (7 d): {s.get('failed_runs_7d')}   "
                     f"identity events: {s.get('identity_events') or {}}")
    lines.append(f"API: {s.get('active_api_keys')} active keys, {s.get('requests_24h', 0)} requests/24 h, "
                 f"p95 {round(s['p95_ms_24h'], 1) if s.get('p95_ms_24h') is not None else '-'} ms, "
                 f"{s.get('errors_503_24h', 0)}x 503")
    mb = lambda b: f"{b / 1e6:.0f} MB" if b else "-"  # noqa: E731
    lines.append(f"storage: registry {mb(s.get('registry_db_bytes'))}, admin {mb(s.get('admin_db_bytes'))}, "
                 f"disk free {s.get('disk_free_pct')}% ({s.get('disk_free_gb')} GB)")
    return "\n".join(lines)
