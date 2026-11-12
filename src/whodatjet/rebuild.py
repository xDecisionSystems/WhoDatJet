"""Rebuild the registry database by replaying every retained archive in snapshot order.

The new database is built beside the live one, checkpointed, and swapped in with an atomic rename.
The previous database is kept in <data_dir>/backups/. Callers must hold the ingest lock, so no
importer writes during the swap. Readers (API workers) must reopen their connections after a swap;
the API detects the file change (plan Phase 4).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from whodatjet import db
from whodatjet.archive_store import ArchiveStore
from whodatjet.faa_client import CHICAGO
from whodatjet.importer import ImportResult, import_pending

log = logging.getLogger(__name__)


def _checkpoint_close(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode = DELETE")  # fold the WAL in and remove -wal/-shm files
    conn.close()


def build(db_path: Path, store: ArchiveStore, staging_dir: Path) -> list[ImportResult]:
    if db_path.exists():
        raise FileExistsError(db_path)
    conn = db.connect_writer(db_path)
    try:
        db.migrate(conn, db.load_migrations("registry"))
        results = import_pending(conn, store, staging_dir)
    except BaseException:
        conn.close()
        raise
    _checkpoint_close(conn)
    return results


def rebuild(live: Path, store: ArchiveStore, staging_dir: Path, backups_dir: Path) -> list[ImportResult]:
    stamp = datetime.now(CHICAGO).strftime("%Y%m%dT%H%M%S")
    new = live.with_name(f"{live.name}.rebuild-{stamp}")
    try:
        results = build(new, store, staging_dir)
        failed = [r for r in results if r.status not in ("imported", "duplicate")]
        if failed:
            raise RuntimeError(f"rebuild aborted; {len(failed)} archive(s) failed: {failed[0].error}")
    except BaseException:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{new}{suffix}").unlink(missing_ok=True)
        raise
    backups_dir.mkdir(parents=True, exist_ok=True)
    if live.exists():
        old = db.connect_writer(live)
        _checkpoint_close(old)
        os.replace(live, backups_dir / f"{live.name}.pre-rebuild-{stamp}")
    os.replace(new, live)
    # Reopen once so the live database is back in WAL mode.
    db.connect_writer(live).close()
    log.info("rebuilt %s from %d archive(s)", live, len(results))
    return results
