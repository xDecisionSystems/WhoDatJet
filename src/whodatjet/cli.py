"""Command-line entry point (`whodatjet --help`)."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from whodatjet import auth, coverage, db
from whodatjet import rebuild as rebuild_mod
from whodatjet.archive_store import open_store
from whodatjet.collect import LockBusy, collect, ingest_lock
from whodatjet.config import ConfigError, load_config, load_env_file
from whodatjet.faa_client import CHICAGO
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
    sub.add_parser("rebuild", help="rebuild the registry database by replaying every archive in order")
    sub.add_parser("coverage", help="show verified coverage, gaps and staleness")
    sub.add_parser("serve", help="run the HTTP API (host, port, workers from the environment)")
    keys_p = sub.add_parser("keys", help="manage API keys")
    keys_sub = keys_p.add_subparsers(dest="keys_action", required=True)
    kc = keys_sub.add_parser("create", help="issue a key (shown once)")
    kc.add_argument("name")
    kc.add_argument("--scope", choices=["query", "monitor"], default="query")
    kc.add_argument("--owner", help="owner or contact")
    kc.add_argument("--expires", type=date.fromisoformat, help="last valid day, YYYY-MM-DD")
    kc.add_argument("--rate-limit", type=int, help="requests per minute")
    kc.add_argument("--note")
    keys_sub.add_parser("list", help="list keys and usage")
    kr = keys_sub.add_parser("revoke", help="revoke a key by id or display prefix")
    kr.add_argument("ident")
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

    if args.command == "rebuild":
        try:
            with ingest_lock(cfg.lock_path):
                results = rebuild_mod.rebuild(cfg.registry_db_path, open_store(cfg), cfg.staging_dir,
                                              cfg.data_dir / "backups")
        except LockBusy as exc:
            print(f"{exc}; try again later", file=sys.stderr)
            return 75
        except (ConfigError, ImportError_, RuntimeError) as exc:
            print(f"rebuild failed (live database unchanged): {exc}", file=sys.stderr)
            return 1
        print(f"rebuilt {cfg.registry_db_path} from {len(results)} archive(s); "
              "restart API workers so they reopen the database")
        return 0

    if args.command == "coverage":
        conn = db.connect_reader(cfg.registry_db_path)
        dates = coverage.coverage_dates(conn)
        today = datetime.now(CHICAGO).date()
        latest = dates[-1] if dates else None
        print(f"coverage: {dates[0] if dates else '-'} .. {latest or '-'} ({len(dates)} date(s))")
        print(f"stale: {coverage.is_stale(latest, today, cfg.stale_days)} (today {today}, "
              f"threshold {cfg.stale_days} days)")
        gaps = coverage.find_gaps(dates, cfg.gap_threshold)
        for g in gaps:
            print(f"gap: after {g.after} before {g.before} ({g.missed_publication_days} publication days missed)")
        if not gaps:
            print("gaps: none")
        return 0

    if args.command == "serve":
        import uvicorn

        if args.env_file:
            os.environ["WHODATJET_ENV_FILE"] = str(args.env_file.resolve())
        uvicorn.run("whodatjet.api:create_app_from_env", factory=True, host=cfg.api_host, port=cfg.api_port,
                    workers=cfg.api_workers, limit_concurrency=cfg.api_limit_concurrency,
                    proxy_headers=False, server_header=False, log_level="info")
        return 0

    if args.command == "keys":
        conn = db.connect_writer(cfg.admin_db_path)
        try:
            db.migrate(conn, db.load_migrations("admin"))
            actor = f"cli:{getpass.getuser()}"
            if args.keys_action == "create":
                issued = auth.create_key(conn, name=args.name, scope=args.scope, created_by=actor,
                                         owner_contact=args.owner, expires_at=args.expires,
                                         rate_limit_per_min=args.rate_limit, note=args.note)
                print(f"id:     {issued.id}\nprefix: {issued.prefix}\nscope:  {args.scope}\n"
                      f"key:    {issued.key}\n\nStore this key now; it cannot be shown again.")
            elif args.keys_action == "list":
                rows = auth.list_keys(conn)
                print(f"{'id':>4}  {'prefix':12}  {'scope':7}  {'status':8}  {'24h':>6}  {'30d':>7}  "
                      f"{'expires':10}  {'last used':20}  name")
                for k in rows:
                    state = "revoked" if k["revoked_at"] else (
                        "expired" if k["expires_at"] and k["expires_at"] < date.today().isoformat() else "active")
                    print(f"{k['id']:>4}  {k['key_prefix']:12}  {k['scope']:7}  {state:8}  {k['requests_24h']:>6}  "
                          f"{k['requests_30d']:>7}  {k['expires_at'] or '-':10}  {k['last_used_at'] or '-':20}  "
                          f"{k['name']}")
            else:
                n = auth.revoke_key(conn, args.ident, revoked_by=actor)
                print("revoked (takes effect within 30 seconds)" if n else "no active key matched")
                return 0 if n else 1
        finally:
            conn.close()
        return 0

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
