"""Daily-pipeline report for the status page: was the FAA file downloaded, saved to S3, and processed?

Three independent stages, each `ok`, `failed`, `stale` (no success within `max_age_h`) or `unknown`
(no run recorded yet):

- download:  the last collection run fetched and validated the FAA archive. Source: the run log
             (`ingest-runs.jsonl`), one line per collection run.
- s3:        the last collection run stored the archive (or found identical content already stored) and
             nothing is waiting in staging for a retried upload.
- processed: every stored snapshot was imported into the registry, and the last import did not fail.

The ingest timer runs twice a day and a run that finds nothing new is a `duplicate`, so a healthy
pipeline shows a success within the last day even on weekends and holidays (no FAA publication).
Exposes no registry data, so it is safe to serve without an API key.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from whodatjet import coverage
from whodatjet.config import Config
from whodatjet.faa_client import CHICAGO

DEFAULT_MAX_AGE_H = 30  # two runs a day (00:15 and 06:15 Central) plus slack for a retry
_TAIL_BYTES = 256 * 1024


def _recent_runs(log_path: Path) -> list[dict]:
    """Runs from the end of the run log, oldest first. Malformed or partial lines are skipped."""
    try:
        with log_path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    runs = []
    for line in data.splitlines():
        try:
            run = json.loads(line)
        except ValueError:
            continue
        if isinstance(run, dict):
            runs.append(run)
    return runs


def _age_ok(finished_at: str | None, now: datetime, max_age_h: float) -> bool:
    try:
        return now - datetime.fromisoformat(finished_at or "") <= timedelta(hours=max_age_h)
    except ValueError:
        return False


def _run_stage(runs: list[dict], succeeded, now: datetime, max_age_h: float) -> dict:
    """Stage state from the run log: the latest run decides ok/failed, the age of the last success decides stale."""
    if not runs:
        return {"status": "unknown", "last_attempt_at": None, "last_success_at": None, "error": None}
    last = runs[-1]
    last_ok = next((r for r in reversed(runs) if succeeded(r)), None)
    out = {"last_attempt_at": last.get("finished_at"),
           "last_success_at": last_ok.get("finished_at") if last_ok else None, "error": None}
    if not succeeded(last):
        out.update(status="failed", error=last.get("error") or "run did not complete")
    elif not _age_ok(last.get("finished_at"), now, max_age_h):
        out.update(status="stale", error=f"no run in the last {max_age_h:g} hours")
    else:
        out["status"] = "ok"
    return out


def pipeline_status(cfg: Config, registry: sqlite3.Connection | None, now: datetime | None = None,
                    today: date | None = None, max_age_h: float = DEFAULT_MAX_AGE_H) -> dict:
    now = now or datetime.now(CHICAGO)
    runs = _recent_runs(cfg.log_path)

    download = _run_stage(runs, lambda r: r.get("archive_sha256") is not None, now, max_age_h)
    s3 = _run_stage(runs, lambda r: r.get("status") in ("stored", "duplicate"), now, max_age_h)
    last = runs[-1] if runs else {}
    download["http_last_modified"] = last.get("http_last_modified")
    s3["key"] = last.get("key")

    pending = len(list(cfg.staging_dir.glob("pending-*.zip"))) if cfg.staging_dir.is_dir() else 0
    s3["pending_uploads"] = pending
    if pending and s3["status"] == "ok":
        s3.update(status="failed", error=f"{pending} downloaded archive(s) are waiting for upload")

    processed: dict = {"status": "unknown", "last_imported_at": None, "coverage_date": None,
                       "not_imported": None, "error": None}
    if registry is None:
        processed.update(status="failed", error="registry database unavailable")
    else:
        try:
            latest = registry.execute("SELECT source_date, imported_at FROM snapshot WHERE import_status = 'imported' "
                                      "ORDER BY imported_at DESC, id DESC LIMIT 1").fetchone()
            bad = registry.execute("SELECT count(*), max(error_message) FROM snapshot "
                                   "WHERE import_status IN ('pending','failed')").fetchone()
            last_run = registry.execute("SELECT status, error_message FROM ingest_run WHERE status != 'running' "
                                        "ORDER BY id DESC LIMIT 1").fetchone()
            processed.update(last_imported_at=latest[1] if latest else None,
                             coverage_date=latest[0] if latest else None, not_imported=bad[0])
            collect_ok = s3["status"] in ("ok", "stale")  # archive stored, so a failed run must be the import
            if bad[0]:
                processed.update(status="failed", error=bad[1] or f"{bad[0]} stored snapshot(s) not imported")
            elif last_run and last_run[0] == "failed" and collect_ok:
                processed.update(status="failed", error=last_run[1] or "last import failed")
            elif latest is None:
                processed["status"] = "unknown"
            elif coverage.is_stale(datetime.fromisoformat(latest[0]).date(), today or now.date(), cfg.stale_days):
                processed.update(status="stale", error=f"latest processed publication is {latest[0]}")
            else:
                processed["status"] = "ok"
        except sqlite3.Error as exc:
            processed.update(status="failed", error=f"registry query failed: {type(exc).__name__}")

    stages = {"download": download, "s3": s3, "processed": processed}
    return {"status": "ok" if all(s["status"] == "ok" for s in stages.values()) else "degraded",
            "checked_at": now.isoformat(timespec="seconds"), **stages}
