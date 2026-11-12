"""Command-line entry point: `whodatjet collect`, `whodatjet archives list`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from whodatjet import db
from whodatjet.archive_store import open_store
from whodatjet.collect import LockBusy, collect, ingest_lock
from whodatjet.config import ConfigError, load_config, load_env_file
from whodatjet.importer import ImportError_, import_pending


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="whodatjet")
    parser.add_argument("--env-file", type=Path, help="load settings from this file (environment wins)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="download today's FAA archive and store it if new")
    arch = sub.add_parser("archives", help="inspect the archive store")
    arch.add_argument("action", choices=["list"])
    sub.add_parser("import", help="import archives from the store that are not yet in the registry database")
    mig = sub.add_parser("migrate", help="apply pending database migrations")
    mig.add_argument("--database", choices=["registry", "admin", "all"], default="all")
    mig.add_argument("--status", action="store_true", help="show applied/pending migrations without applying")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("botocore", "boto3", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        if args.env_file:
            load_env_file(args.env_file)
        cfg = load_config()
    except (ConfigError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "collect":
        try:
            result = collect(cfg)
        except LockBusy as exc:
            logging.getLogger("whodatjet").info("%s; skipping this run", exc)
            return 0
        print(json.dumps({k: v for k, v in vars(result).items() if v not in (None, [])}, indent=2))
        return 0 if result.status in ("stored", "duplicate") else 1

    if args.command == "archives":
        try:
            archives = open_store(cfg).list_daily()
        except ConfigError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 2
        for a in archives:
            print(f"{a.source_date}  {a.size / 1e6:8.1f} MB  {a.key}")
        print(f"{len(archives)} archive(s), {sum(a.size for a in archives) / 1e9:.2f} GB total")
        return 0

    if args.command == "import":
        try:
            with ingest_lock(cfg.lock_path):
                conn = db.connect_writer(cfg.registry_db_path, mmap_size=cfg.sqlite_mmap_size,
                                         cache_size_kib=cfg.sqlite_cache_size_kib)
                try:
                    results = import_pending(conn, open_store(cfg), cfg.staging_dir)
                    if results and results[-1].status == "imported":
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    conn.close()
        except LockBusy as exc:
            print(f"{exc}; try again later", file=sys.stderr)
            return 75
        except (ConfigError, ImportError_) as exc:
            print(f"import error: {exc}", file=sys.stderr)
            return 1
        for r in results:
            print(json.dumps({k: v for k, v in vars(r).items() if v not in (None, {}, [])}))
        if not results:
            print("nothing to import")
        return 0 if all(r.status in ("imported", "duplicate") for r in results) else 1

    if args.command == "migrate":
        targets = ["registry", "admin"] if args.database == "all" else [args.database]
        paths = {"registry": cfg.registry_db_path, "admin": cfg.admin_db_path}
        try:
            for name in targets:
                conn = db.connect_writer(paths[name])
                try:
                    migrations = db.load_migrations(name)
                    if args.status:
                        done = db.applied_versions(conn)
                        for m in migrations:
                            state = "applied" if m.version in done else "pending"
                            print(f"{name:8s} {m.version:04d}_{m.name:30s} {state}")
                    else:
                        applied = db.migrate(conn, migrations)
                        print(f"{name}: applied {len(applied)} migration(s) "
                              f"{[f'{m.version:04d}_{m.name}' for m in applied]} -> {paths[name]}")
                finally:
                    conn.close()
        except db.MigrationError as exc:
            print(f"migration error: {exc}", file=sys.stderr)
            return 1
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
