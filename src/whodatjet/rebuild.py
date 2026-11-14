"""Rebuild the registry database by replaying every retained archive in snapshot order.

The new database is built beside the live one, then copied *into* the live database file with SQLite's
online backup API: one atomic write transaction, so API workers that have the database open keep working
(each request sees either the old or the new data, never a mixture) and need no restart. The previous
contents are saved first in <data_dir>/backups/ with the same API. Callers must hold the ingest lock, so no
importer writes during the swap.

An earlier version swapped files with a rename and a journal-mode change; that cannot work while the API
holds connections (the live test failed with "database is locked" under load).
"""

from __future__ import annotations

import logging
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


def _copy_db(src_path: Path, dest: Path) -> None:
    """Consistent copy of a database that other connections may have open."""
    s = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        d = sqlite3.connect(dest)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


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
    try:
        if live.exists():
            _copy_db(live, backups_dir / f"{live.name}.pre-rebuild-{stamp}")  # keep what was there
        dst = db.connect_writer(live)  # WAL, creates the file if it is missing
        try:
            src = sqlite3.connect(f"file:{new}?mode=ro", uri=True)
            try:
                src.backup(dst)  # pages=-1: a single transaction, atomic for readers
            finally:
                src.close()
            try:
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # best effort; readers may delay it
            except sqlite3.OperationalError:
                log.info("WAL checkpoint after rebuild deferred (readers busy)")
        finally:
            dst.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{new}{suffix}").unlink(missing_ok=True)
    log.info("rebuilt %s from %d archive(s)", live, len(results))
    return results
