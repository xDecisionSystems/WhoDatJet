"""SQLite connection policy and migration runner.

Two databases (see plan, "Admin database"):
- registry: whodatjet.db — derived from archives; the importer is the only writer.
- admin: admin.db — API keys, admin accounts, settings, audit log.

Writers use WAL, foreign keys, and a busy timeout. API readers open the file read-only
(`mode=ro` URI plus `PRAGMA query_only`), so they can never modify registry data.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

BUSY_TIMEOUT_MS = 10_000
MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
DATABASES = ("registry", "admin")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def _apply_pragmas(conn: sqlite3.Connection, *, mmap_size: int, cache_size_kib: int) -> None:
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    if mmap_size:
        conn.execute(f"PRAGMA mmap_size = {int(mmap_size)}")
    if cache_size_kib:
        conn.execute(f"PRAGMA cache_size = -{int(cache_size_kib)}")  # negative = KiB


def connect_writer(path: Path, *, mmap_size: int = 0, cache_size_kib: int = 0) -> sqlite3.Connection:
    """Read-write connection in autocommit mode; callers manage transactions explicitly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if mode.lower() != "wal":
        conn.close()
        raise RuntimeError(f"could not enable WAL on {path} (got {mode}); is it on a network filesystem?")
    conn.execute("PRAGMA synchronous = NORMAL")
    _apply_pragmas(conn, mmap_size=mmap_size, cache_size_kib=cache_size_kib)
    return conn


def connect_reader(path: Path, *, mmap_size: int = 0, cache_size_kib: int = 0) -> sqlite3.Connection:
    """Read-only connection for API workers. One per thread; never shared across threads."""
    if not path.exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, mmap_size=mmap_size, cache_size_kib=cache_size_kib)
    conn.execute("PRAGMA query_only = ON")
    return conn


def load_migrations(database: str, directory: Path | None = None) -> list[Migration]:
    if database not in DATABASES:
        raise ValueError(f"unknown database {database!r}")
    if directory is not None:
        files = [(p.name, p.read_text(encoding="utf-8")) for p in directory.iterdir()]
    else:
        pkg = resources.files("whodatjet").joinpath("migrations", database)
        files = [(p.name, p.read_text(encoding="utf-8")) for p in pkg.iterdir() if p.is_file()]
    out = []
    for name, sql in files:
        m = MIGRATION_RE.match(name)
        if m:
            out.append(Migration(int(m.group(1)), m.group(2), sql))
        elif name.endswith(".sql"):
            raise MigrationError(f"badly named migration {name!r}; expected NNNN_name.sql")
    out.sort(key=lambda m: m.version)
    versions = [m.version for m in out]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration versions in {database}: {versions}")
    return out


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL)""")


def applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    _ensure_table(conn)
    return {r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, migrations: list[Migration]) -> list[Migration]:
    """Apply pending migrations in order, each in its own transaction. Returns those applied.

    Refuses to run if an already-applied migration's file has changed (checksum mismatch)."""
    done = applied_versions(conn)
    for m in migrations:
        if m.version in done and done[m.version] != m.checksum:
            raise MigrationError(f"migration {m.version:04d}_{m.name} was modified after being applied")
    applied = []
    for m in migrations:
        if m.version in done:
            continue
        now = datetime.now(UTC).isoformat(timespec="seconds")
        # executescript commits any open transaction first, so BEGIN/COMMIT live inside the script.
        script = (f"BEGIN;\n{m.sql}\n;\nINSERT INTO schema_migrations (version, name, checksum, applied_at) "
                  f"VALUES ({m.version}, '{m.name}', '{m.checksum}', '{now}');\nCOMMIT;")
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"migration {m.version:04d}_{m.name} failed: {exc}") from exc
        applied.append(m)
    return applied
