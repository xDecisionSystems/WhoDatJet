"""Consistent online backups of whodatjet.db and admin.db (SQLite backup API), safe while the API
serves and the importer runs. Archives are backed up by their store (plan: "Archive storage")."""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from whodatjet.faa_client import CHICAGO

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackupFile:
    source: Path
    path: Path
    bytes: int


def _backup_one(src: Path, dest: Path) -> BackupFile:
    # Keep sensitive data private from the first write and isolate concurrent runs.
    with tempfile.NamedTemporaryFile(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent, delete=False) as f:
        tmp = Path(f.name)
    try:
        s = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
        try:
            d = sqlite3.connect(tmp)
            try:
                s.backup(d, pages=4096)
                ok = d.execute("PRAGMA quick_check").fetchone()[0]
                if ok != "ok":
                    raise RuntimeError(f"backup of {src} failed integrity check: {ok}")
            finally:
                d.close()
        finally:
            s.close()
        tmp.replace(dest)
        return BackupFile(src, dest, dest.stat().st_size)
    finally:
        tmp.unlink(missing_ok=True)


def backup(databases: dict[str, Path], dest_dir: Path, *, keep: int = 14, label: str | None = None) -> list[BackupFile]:
    """Back up each existing database to <dest_dir>/<name>-<stamp>.db and keep the newest `keep` per name."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = label or datetime.now(CHICAGO).strftime("%Y%m%dT%H%M%S")
    out = []
    for name, src in databases.items():
        if not src.exists():
            log.warning("database %s does not exist; skipped", src)
            continue
        out.append(_backup_one(src, dest_dir / f"{name}-{stamp}.db"))
        if keep > 0:
            old = sorted(dest_dir.glob(f"{name}-*.db"))[:-keep]
            for p in old:
                p.unlink()
    return out
