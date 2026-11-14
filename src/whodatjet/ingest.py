"""Scheduled ingestion: collect the FAA archive, then import anything new, under one lock.

Every run is recorded in the registry database's `ingest_run` table:
- a failed download or upload       -> one `failed` run (with HTTP details and the error)
- content identical to a snapshot   -> one `duplicate` run linked to that snapshot
- a newly stored archive            -> the importer's own run (`imported` or `failed`)
The process exits nonzero when anything failed, so systemd's `OnFailure=` alert fires.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from whodatjet import db
from whodatjet.archive_store import ArchiveStore, open_store
from whodatjet.collect import RunResult, collect, ingest_lock
from whodatjet.config import Config
from whodatjet.faa_client import CHICAGO
from whodatjet.importer import ImportResult, check_schema, import_pending, needs_rebuild
from whodatjet.rebuild import rebuild

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    collect: RunResult
    imports: list[ImportResult] = field(default_factory=list)
    error: str | None = None
    rebuilt_archives: int = 0  # >0 when this run first rebuilt the registry (after a schema change)

    @property
    def ok(self) -> bool:
        return (self.error is None and self.collect.status in ("stored", "duplicate")
                and all(r.status in ("imported", "duplicate") for r in self.imports))

    def summary(self) -> dict:
        return {
            "ok": self.ok, "collect_status": self.collect.status, "source_date": self.collect.source_date,
            "key": self.collect.key, "collect_error": self.collect.error,
            "imports": [{"key": r.archive_key, "status": r.status, "error": r.error,
                         **{k: r.stats[k] for k in ("master_inserted", "master_closed", "observed_opened",
                                                    "observed_closed", "dereg_intervals") if k in r.stats}}
                        for r in self.imports],
            "error": self.error, "rebuilt_archives": self.rebuilt_archives,
        }


def _record_run(conn: sqlite3.Connection, result: RunResult) -> None:
    snapshot_id = None
    if result.content_sha256:
        row = conn.execute("SELECT id FROM snapshot WHERE content_sha256 = ?", (result.content_sha256,)).fetchone()
        snapshot_id = row[0] if row else None
    conn.execute(
        "INSERT INTO ingest_run (started_at, finished_at, run_date, status, snapshot_id, http_status, "
        "http_last_modified, error_message) VALUES (?,?,?,?,?,?,?,?)",
        (result.started_at, result.finished_at, datetime.now(CHICAGO).date().isoformat(),
         "duplicate" if result.status == "duplicate" else "failed", snapshot_id, result.http_status,
         result.http_last_modified, result.error))


def ingest(cfg: Config, store: ArchiveStore | None = None, *, backoff_s: float = 30) -> IngestResult:
    """Raises collect.LockBusy if another ingest, import or rebuild is running."""
    with ingest_lock(cfg.lock_path):
        store = store or open_store(cfg)
        collected = collect(cfg, store, backoff_s=backoff_s, lock_held=True)
        result = IngestResult(collected)
        try:
            conn = db.connect_writer(cfg.registry_db_path, mmap_size=cfg.sqlite_mmap_size,
                                     cache_size_kib=cfg.sqlite_cache_size_kib)
        except Exception as exc:
            result.error = f"cannot open registry database: {exc}"
            return result
        try:
            reason = needs_rebuild(conn)
            if reason:
                # A schema change (new FAA columns) means every stored record must be re-read from the archives.
                conn.close()
                log.warning("rebuilding the registry from the archives first: %s", reason)
                result.rebuilt_archives = len(rebuild(cfg.registry_db_path, store, cfg.staging_dir,
                                                      cfg.data_dir / "backups"))
                conn = db.connect_writer(cfg.registry_db_path, mmap_size=cfg.sqlite_mmap_size,
                                         cache_size_kib=cfg.sqlite_cache_size_kib)
            check_schema(conn)
            if collected.status in ("failed", "duplicate"):
                _record_run(conn, collected)
            # Import everything not yet imported: the new archive, retried uploads, earlier failures.
            result.imports = import_pending(conn, store, cfg.staging_dir)
            if any(r.status == "imported" for r in result.imports):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            log.error("ingest failed: %s", result.error)
        finally:
            conn.close()
        level = logging.INFO if result.ok else logging.ERROR
        log.log(level, "ingest finished", extra={"event": "ingest", "summary": result.summary()})
        return result
