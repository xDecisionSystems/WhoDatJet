"""Daily collection: download the FAA archive and retain it in the archive store if its content is new.

Until the registry database exists (plan Phase 2), every run is recorded as one JSON line in
`<data_dir>/logs/ingest-runs.jsonl`, and the S3 object metadata carries the snapshot details.
"""

from __future__ import annotations

import fcntl
import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from whodatjet import __version__
from whodatjet.archive_store import ArchiveStore, daily_key, open_store
from whodatjet.config import Config
from whodatjet.faa_client import CHICAGO, Download, download, inspect_archive

log = logging.getLogger(__name__)


class LockBusy(RuntimeError):
    pass


@dataclass
class RunResult:
    run_id: str
    started_at: str
    status: str = "running"  # stored | duplicate | failed
    finished_at: str | None = None
    source_date: str | None = None
    source_date_basis: str | None = None
    http_status: int | None = None
    http_last_modified: str | None = None
    archive_sha256: str | None = None
    content_sha256: str | None = None
    archive_bytes: int | None = None
    key: str | None = None
    error: str | None = None
    retried_pending: list[dict] = field(default_factory=list)


@contextmanager
def ingest_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy(f"another collection run holds {path}") from exc
        yield


def _now() -> str:
    return datetime.now(CHICAGO).isoformat(timespec="seconds")


def _sidecar(zip_path: Path) -> Path:
    return zip_path.with_suffix(".json")


def store_archive(cfg: Config, store: ArchiveStore, path: Path, dl: Download, result: RunResult) -> None:
    """Inspect a staged archive and upload it unless identical content is already stored."""
    info = inspect_archive(path, cfg.required_files, dl)
    result.source_date = info.source_date.isoformat()
    result.source_date_basis = info.source_date_basis
    result.archive_sha256 = info.archive_sha256
    result.content_sha256 = info.content_sha256
    result.archive_bytes = info.archive_bytes

    existing = store.find_content(info.content_sha256)
    if existing is not None:
        result.status, result.key = "duplicate", existing.key
        log.info("content already stored as %s; nothing to upload", existing.key)
        return
    key = daily_key(info.source_date, info.content_sha256)
    metadata = {
        "content-sha256": info.content_sha256,
        "source-date": info.source_date.isoformat(),
        "source-date-basis": info.source_date_basis,
        "downloaded-at": dl.downloaded_at.isoformat(timespec="seconds"),
        "http-last-modified": dl.last_modified or "",
        "http-etag": (dl.etag or "").strip('"'),
        "collector-version": __version__,
    }
    result.key = store.put_verified(key, path, info.archive_sha256, metadata)
    result.status = "stored"
    log.info("stored %s (%d bytes, source date %s via %s)", result.key, info.archive_bytes,
             info.source_date, info.source_date_basis)


def _retry_pending(cfg: Config, store: ArchiveStore, result: RunResult) -> None:
    """Upload archives left in staging by earlier runs whose upload failed."""
    for sidecar in sorted(cfg.staging_dir.glob("pending-*.json")):
        zip_path = sidecar.with_suffix(".zip")
        if not zip_path.exists():
            sidecar.unlink()
            continue
        meta = json.loads(sidecar.read_text())
        dl = Download(zip_path, meta["http_status"], meta.get("last_modified"), meta.get("etag"),
                      datetime.fromisoformat(meta["downloaded_at"]))
        sub = RunResult(run_id=meta["run_id"], started_at=_now())
        try:
            store_archive(cfg, store, zip_path, dl, sub)
            zip_path.unlink()
            sidecar.unlink()
        except Exception as exc:  # keep it for the next run
            sub.status, sub.error = "failed", f"{type(exc).__name__}: {exc}"
            log.error("pending archive %s still not stored: %s", zip_path.name, sub.error)
        result.retried_pending.append({"run_id": sub.run_id, "status": sub.status,
                                       "key": sub.key, "error": sub.error})


def collect(cfg: Config, store: ArchiveStore | None = None, *, backoff_s: float = 30,
            lock_held: bool = False) -> RunResult:
    """Download and retain the FAA archive. Pass `lock_held=True` when the caller holds the ingest lock."""
    result = RunResult(run_id=uuid.uuid4().hex[:12], started_at=_now())
    cfg.staging_dir.mkdir(parents=True, exist_ok=True)
    staged = cfg.staging_dir / f"pending-{result.run_id}.zip"
    try:
        with nullcontext() if lock_held else ingest_lock(cfg.lock_path):
            store = store or open_store(cfg)
            _retry_pending(cfg, store, result)
            dl = download(cfg.faa_url, staged, user_agent=cfg.user_agent,
                          attempts=cfg.download_attempts, timeout_s=cfg.download_timeout_s, backoff_s=backoff_s)
            result.http_status, result.http_last_modified = dl.http_status, dl.last_modified
            _sidecar(staged).write_text(json.dumps({
                "run_id": result.run_id, "http_status": dl.http_status, "last_modified": dl.last_modified,
                "etag": dl.etag, "downloaded_at": dl.downloaded_at.isoformat()}))
            store_archive(cfg, store, staged, dl, result)
            staged.unlink()
            _sidecar(staged).unlink()
    except LockBusy:
        raise
    except Exception as exc:
        result.status, result.error = "failed", f"{type(exc).__name__}: {exc}"
        log.error("collection failed: %s", result.error)
        # An archive that downloaded but failed validation is useless; one that failed to upload is kept.
        if result.archive_sha256 is None:
            staged.unlink(missing_ok=True)
            _sidecar(staged).unlink(missing_ok=True)
    finally:
        result.finished_at = _now()
        if result.status != "running":
            cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
            with cfg.log_path.open("a") as fh:
                fh.write(json.dumps(asdict(result)) + "\n")
    return result
