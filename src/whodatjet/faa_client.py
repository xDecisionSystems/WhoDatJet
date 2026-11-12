"""Download the FAA Releasable Aircraft archive and derive its identity and source date.

Phase 0 findings this relies on:
- The FAA CDN answers HEAD with 503, so only GET is used.
- A descriptive, non-browser User-Agent is accepted.
- `Last-Modified` reflects the nightly publication (about 11:30 p.m. Central).
"""

from __future__ import annotations

import hashlib
import logging
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
CHICAGO = ZoneInfo("America/Chicago")
CHUNK = 1024 * 1024


class DownloadError(RuntimeError):
    pass


class ArchiveError(RuntimeError):
    """The downloaded file is not a usable FAA archive (corrupt, or a required file is missing)."""


@dataclass(frozen=True)
class Download:
    path: Path
    http_status: int
    last_modified: str | None
    etag: str | None
    downloaded_at: datetime


@dataclass(frozen=True)
class ArchiveInfo:
    archive_sha256: str
    content_sha256: str
    archive_bytes: int
    members: dict[str, str]  # member name -> sha256 of uncompressed content
    newest_member_mtime: datetime | None  # naive; FAA writes Central local time
    source_date: date
    source_date_basis: str  # http_last_modified | zip_member_mtime | download_date


def download(url: str, dest: Path, *, user_agent: str, attempts: int = 5,
             timeout_s: float = 600, backoff_s: float = 30,
             transport: httpx.BaseTransport | None = None) -> Download:
    """GET `url` into `dest` atomically, retrying transient failures with exponential backoff."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(headers={"User-Agent": user_agent}, timeout=timeout_s,
                              follow_redirects=True, transport=transport) as client, client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise DownloadError(f"HTTP {resp.status_code} from {url}")
                with part.open("wb") as fh:
                    for chunk in resp.iter_bytes(CHUNK):
                        fh.write(chunk)
                expected = resp.headers.get("content-length")
                if expected is not None and part.stat().st_size != int(expected):
                    raise DownloadError(f"truncated download: {part.stat().st_size} of {expected} bytes")
                part.replace(dest)
                return Download(dest, resp.status_code, resp.headers.get("last-modified"),
                                resp.headers.get("etag"), datetime.now(CHICAGO))
        except (httpx.HTTPError, DownloadError, OSError) as exc:
            last_error = exc
            part.unlink(missing_ok=True)
            if attempt < attempts:
                delay = backoff_s * 2 ** (attempt - 1)
                log.warning("download attempt %d/%d failed: %s; retrying in %.0fs", attempt, attempts, exc, delay)
                time.sleep(delay)
    raise DownloadError(f"download failed after {attempts} attempts: {last_error}")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def content_hash(members: dict[str, str]) -> str:
    """Hash of the sorted (member name, member content hash) list; stable across re-zipping."""
    h = hashlib.sha256()
    for name in sorted(members):
        h.update(f"{name}\0{members[name]}\n".encode())
    return h.hexdigest()


def source_date_for(last_modified: str | None, newest_member_mtime: datetime | None,
                    downloaded_at: datetime) -> tuple[date, str]:
    """Prefer HTTP Last-Modified (as a Chicago date), then the newest member mtime, then download date."""
    if last_modified:
        try:
            dt = parsedate_to_datetime(last_modified)
            if dt.tzinfo is not None:
                return dt.astimezone(CHICAGO).date(), "http_last_modified"
        except (TypeError, ValueError):
            log.warning("unparseable Last-Modified header: %r", last_modified)
    if newest_member_mtime is not None:
        return newest_member_mtime.date(), "zip_member_mtime"
    return downloaded_at.astimezone(CHICAGO).date(), "download_date"


def inspect_archive(path: Path, required_files: tuple[str, ...], dl: Download) -> ArchiveInfo:
    """Validate the ZIP and compute both hashes and the source date."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a valid ZIP archive: {exc}") from exc
    with zf:
        names = {i.filename: i for i in zf.infolist() if not i.is_dir()}
        upper = {n.upper(): n for n in names}
        missing = [f for f in required_files if f.upper() not in upper]
        if missing:
            raise ArchiveError(f"required files missing from archive: {', '.join(missing)}")
        members: dict[str, str] = {}
        for name in names:
            h = hashlib.sha256()
            try:
                with zf.open(name) as fh:  # reading fully also verifies each member's CRC
                    while chunk := fh.read(CHUNK):
                        h.update(chunk)
            except (zipfile.BadZipFile, OSError) as exc:
                raise ArchiveError(f"corrupt archive member {name}: {exc}") from exc
            members[name] = h.hexdigest()
        mtimes = [datetime(*i.date_time) for i in names.values()]
    newest = max(mtimes) if mtimes else None
    sdate, basis = source_date_for(dl.last_modified, newest, dl.downloaded_at)
    return ArchiveInfo(
        archive_sha256=_sha256_file(path),
        content_sha256=content_hash(members),
        archive_bytes=path.stat().st_size,
        members=members,
        newest_member_mtime=newest,
        source_date=sdate,
        source_date_basis=basis,
    )
