"""Public HTTP API (plan: "API"). Admin routes live in a separate process (Phase 6).

Run with `whodatjet serve`. Endpoints are synchronous handlers, so FastAPI runs them in its thread
pool; each thread keeps its own read-only SQLite connection and reopens it if the database file is
replaced (e.g. by `whodatjet rebuild`).
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from whodatjet import __version__, coverage, db, query
from whodatjet import status as status_mod
from whodatjet.auth import KeyCache, RateLimiter, UsageRecorder
from whodatjet.config import Config, load_config, load_env_file
from whodatjet.faa_client import CHICAGO
from whodatjet.normalize import InvalidNNumber, normalize_n_number

log = logging.getLogger(__name__)
UNAUTHORIZED = {"detail": "missing, invalid, expired, or revoked API key"}
OPEN_PATHS = ("/healthz", "/docs", "/openapi.json", "/redoc")


class Connections:
    """One read-only connection per thread per database; reopened when the file's inode changes."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._local = threading.local()

    def get(self, path: Path) -> sqlite3.Connection:
        cache: dict = self._local.__dict__.setdefault("conns", {})
        try:
            ino = os.stat(path).st_ino
        except FileNotFoundError as exc:
            raise HTTPException(503, "database unavailable") from exc
        entry = cache.get(path)
        if entry is not None and entry[1] == ino:
            return entry[0]
        if entry is not None:
            entry[0].close()
        conn = db.connect_reader(path, mmap_size=self.cfg.sqlite_mmap_size,
                                 cache_size_kib=self.cfg.sqlite_cache_size_kib)
        cache[path] = (conn, ino)
        return conn


def _parse_date(raw: str | None, today: date) -> date:
    if raw is None:
        return today
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(400, f"invalid date {raw!r}; expected YYYY-MM-DD") from exc


def _n_number(raw: str) -> str:
    try:
        return normalize_n_number(raw)
    except InvalidNNumber as exc:
        raise HTTPException(400, str(exc)) from exc


def create_app(cfg: Config, *, today: Callable[[], date] | None = None, worker_id: str | None = None,
               start_flusher: bool = True, key_cache: KeyCache | None = None) -> FastAPI:
    today_fn = today or (lambda: datetime.now(CHICAGO).date())
    conns = Connections(cfg)
    keys = key_cache or KeyCache(cfg.admin_db_path)
    limiter = RateLimiter(cfg.api_workers)
    usage = UsageRecorder(cfg.admin_db_path, worker_id or f"{os.uname().nodename}:{os.getpid()}")
    stop = threading.Event()
    status_cache: dict = {}

    def flusher() -> None:
        while not stop.wait(60):
            try:
                usage.flush()
            except Exception:  # never let metrics break the API
                log.exception("usage flush failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        thread = threading.Thread(target=flusher, name="usage-flush", daemon=True) if start_flusher else None
        if thread:
            thread.start()
        yield
        stop.set()
        try:
            usage.flush()
        except Exception:
            log.exception("final usage flush failed")

    app = FastAPI(title="WhoDatJet", version=__version__, lifespan=lifespan,
                  description="Historical FAA aircraft-registration lookups with provenance and confidence.")
    app.state.usage = usage

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        start = time.perf_counter()
        key_id = None
        path = request.url.path
        if path.startswith("/v1/"):
            auth = request.headers.get("authorization", "")
            presented = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key")
            try:
                info = keys.verify(presented, today_fn())
            except Exception:
                log.exception("API key verification failed")
                info = None
            if info is None:
                resp = JSONResponse(UNAUTHORIZED, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                usage.record(None, 401, (time.perf_counter() - start) * 1000)
                return resp
            key_id = info.id
            if info.scope == "monitor" and path != "/v1/status":
                usage.record(key_id, 403, (time.perf_counter() - start) * 1000)
                return JSONResponse({"detail": "this key may only call /v1/status"}, status_code=403)
            retry = limiter.check(info)
            if retry is not None:
                usage.record(key_id, 429, (time.perf_counter() - start) * 1000)
                return JSONResponse({"detail": "rate limit exceeded"}, status_code=429,
                                    headers={"Retry-After": str(retry)})
        response = await call_next(request)
        if path.startswith("/v1/") or path == "/healthz":
            usage.record(key_id, response.status_code, (time.perf_counter() - start) * 1000)
        return response

    def registry() -> sqlite3.Connection:
        return conns.get(cfg.registry_db_path)

    @app.get("/healthz")
    def healthz(strict: bool = False):
        body: dict = {"status": "ok", "version": __version__}
        problems = []
        try:
            conn = registry()
            snaps = query.Snapshots(conn)
            last_run = conn.execute("SELECT status FROM ingest_run WHERE status != 'running' "
                                    "ORDER BY id DESC LIMIT 1").fetchone()
            latest = date.fromisoformat(snaps.end) if snaps.end else None
            body.update(database="ok", last_coverage_date=snaps.end,
                        data_age_days=(today_fn() - latest).days if latest else None,
                        last_run_status=last_run[0] if last_run else None)
            if coverage.is_stale(latest, today_fn(), cfg.stale_days):
                problems.append("stale")
            if last_run and last_run[0] == "failed":
                problems.append("last_run_failed")
        except (HTTPException, sqlite3.Error):
            body["database"] = "unavailable"
            problems.append("database_unavailable")
        body["problems"] = problems
        if problems:
            body["status"] = "degraded" if body.get("database") == "ok" else "down"
        return JSONResponse(body, status_code=503 if strict and problems else 200)

    @app.get("/v1/tail-numbers/{n_number}")
    def tail_number(n_number: str, date: str | None = None, fields: str | None = None):  # noqa: A002
        n = _n_number(n_number)
        d = _parse_date(date, today_fn())
        try:
            wanted = query.parse_fields(fields)
            result = query.lookup(registry(), n, d, today=today_fn(), gap_threshold=cfg.gap_threshold,
                                  stale_days=cfg.stale_days)
        except query.QueryError as exc:
            raise HTTPException(400, str(exc)) from exc
        if isinstance(result, query.NotFound):
            return JSONResponse(status_code=404, content={
                "detail": "no registration found for this N-number on this date", "n_number": result.n_number,
                "date": result.date, "reason": result.reason, "coverage_start": result.coverage_start,
                "coverage_end": result.coverage_end})
        return query.select_fields(result, wanted)

    @app.get("/v1/tail-numbers/{n_number}/history")
    def tail_history(n_number: str, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        result = query.history(registry(), _n_number(n_number), limit=limit, offset=offset,
                               gap_threshold=cfg.gap_threshold)
        if result is None:
            raise HTTPException(404, "no records for this N-number")
        return result

    @app.get("/v1/aircraft/{aircraft_id}")
    def aircraft(aircraft_id: int):
        result = query.aircraft(registry(), aircraft_id)
        if result is None:
            raise HTTPException(404, "aircraft not found")
        return result

    @app.get("/v1/snapshots")
    def snapshots():
        return query.snapshots(registry(), gap_threshold=cfg.gap_threshold)

    @app.get("/v1/status")
    def status():
        now = time.monotonic()
        if status_cache.get("at", -1e9) > now - 30:
            return status_cache["body"]
        body = _status(cfg, registry, conns, today_fn())
        status_cache.update(at=now, body=body)
        return body

    return app


def _status(cfg: Config, registry: Callable[[], sqlite3.Connection], conns: Connections, today: date) -> dict:
    """Flat operational summary for dashboards (plan: "Homepage monitoring"). No registry data."""
    out: dict = {"status": "ok", "version": f"v{__version__}", "profile": cfg.profile, "last_import_at": None,
                 "last_import_result": None, "last_coverage_date": None, "data_age_days": None, "snapshots": 0,
                 "open_gaps": 0, "active_alerts": 0, "requests_24h": 0, "p95_ms_24h": None, "errors_503_24h": 0,
                 "disk_free_pct": None, "archive_store": cfg.archive_backend, "archive_store_ok": None,
                 "archive_replica_lag": None, "disk_days_remaining": None}
    try:
        conn = registry()
        last = conn.execute("SELECT finished_at, status FROM ingest_run WHERE status != 'running' "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        dates = coverage.coverage_dates(conn)
        latest = dates[-1] if dates else None
        out.update(
            last_import_at=last[0] if last else None, last_import_result=last[1] if last else None,
            last_coverage_date=latest.isoformat() if latest else None,
            data_age_days=(today - latest).days if latest else None,
            snapshots=len(dates), open_gaps=len(coverage.find_gaps(dates, cfg.gap_threshold)))
        if coverage.is_stale(latest, today, cfg.stale_days) or (last and last[1] == "failed"):
            out["status"] = "degraded"
    except (HTTPException, sqlite3.Error):
        out["status"] = "down"
    try:
        adm = conns.get(cfg.admin_db_path)
        m = adm.execute("SELECT COALESCE(SUM(requests), 0), MAX(p95_ms), COALESCE(SUM(status_503), 0) "
                        "FROM api_metrics_minute "
                        "WHERE minute >= strftime('%Y-%m-%dT%H:%M', 'now', '-1 day')").fetchone()
        out.update(requests_24h=m[0], p95_ms_24h=round(m[1], 1) if m[1] is not None else None, errors_503_24h=m[2])
    except (HTTPException, sqlite3.Error):
        pass
    try:
        adm = conns.get(cfg.admin_db_path)
        out["active_alerts"] = adm.execute("SELECT count(*) FROM alert_state WHERE resolved_at IS NULL").fetchone()[0]
        if out["active_alerts"] and out["status"] == "ok":
            out["status"] = "degraded"
    except (HTTPException, sqlite3.Error):
        pass
    try:
        du = shutil.disk_usage(cfg.data_dir)
        out["disk_free_pct"] = round(100 * du.free / du.total, 1)
    except OSError:
        pass
    out["disk_days_remaining"] = status_mod.disk_days_remaining(cfg)
    return out


def create_app_from_env() -> FastAPI:
    """Uvicorn factory for `whodatjet serve` (each worker process builds its own app)."""
    env_file = os.environ.get("WHODATJET_ENV_FILE")
    if env_file:
        load_env_file(Path(env_file))
    return create_app(load_config())
