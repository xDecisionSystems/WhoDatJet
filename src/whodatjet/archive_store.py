"""Archive storage. Archives are irreplaceable: objects are written once and never overwritten or deleted.

Keys are relative to the store root/prefix: daily/<source_date>/<content_sha256>.zip
Each archive carries metadata (S3 object metadata, or a JSON sidecar for the filesystem store).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from whodatjet.config import Config, S3Config

DAILY_KEY_RE = re.compile(r"^daily/(\d{4}-\d{2}-\d{2})/([0-9a-f]{64})\.zip$")
CHUNK = 1024 * 1024


class ArchiveStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredArchive:
    key: str  # relative to the store
    source_date: date
    content_sha256: str
    size: int


def daily_key(source_date: date, content_sha256: str) -> str:
    return f"daily/{source_date.isoformat()}/{content_sha256}.zip"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


class ArchiveStore(Protocol):
    kind: str
    location: str

    def list_daily(self) -> list[StoredArchive]: ...
    def exists(self, key: str) -> bool: ...
    def put_verified(self, key: str, path: Path, archive_sha256: str, metadata: dict[str, str]) -> str: ...
    def get(self, key: str, dest: Path) -> Path: ...
    def metadata(self, key: str) -> dict[str, str]: ...

    def find_content(self, content_sha256: str) -> StoredArchive | None:
        return next((a for a in self.list_daily() if a.content_sha256 == content_sha256), None)


def _parse_key(key: str, size: int) -> StoredArchive | None:
    m = DAILY_KEY_RE.match(key)
    return StoredArchive(key, date.fromisoformat(m.group(1)), m.group(2), size) if m else None


class FilesystemArchiveStore(ArchiveStore):
    kind = "filesystem"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.location = str(self.root)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ArchiveStoreError(f"key escapes store root: {key!r}")
        return p

    def list_daily(self) -> list[StoredArchive]:
        daily = self.root / "daily"
        out = []
        if daily.exists():
            for p in daily.glob("*/*.zip"):
                a = _parse_key(p.relative_to(self.root).as_posix(), p.stat().st_size)
                if a:
                    out.append(a)
        return sorted(out, key=lambda a: (a.source_date, a.key))

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def put_verified(self, key: str, path: Path, archive_sha256: str, metadata: dict[str, str]) -> str:
        dest = self._path(key)
        if dest.exists():
            raise ArchiveStoreError(f"refusing to overwrite existing archive {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
        with path.open("rb") as src, tmp.open("wb") as out:
            while chunk := src.read(CHUNK):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if sha256_file(tmp) != archive_sha256:
            tmp.unlink()
            raise ArchiveStoreError(f"checksum mismatch writing {dest}")
        sidecar = dest.with_suffix(".json")
        sidecar.write_text(json.dumps({**metadata, "archive-sha256": archive_sha256}, indent=2, sort_keys=True))
        os.link(tmp, dest)  # fails if dest appeared meanwhile; never overwrites
        tmp.unlink()
        return key

    def get(self, key: str, dest: Path) -> Path:
        src = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with src.open("rb") as s, dest.open("wb") as d:
            while chunk := s.read(CHUNK):
                d.write(chunk)
        return dest

    def metadata(self, key: str) -> dict[str, str]:
        sidecar = self._path(key).with_suffix(".json")
        return json.loads(sidecar.read_text()) if sidecar.exists() else {}


class S3ArchiveStore(ArchiveStore):
    kind = "s3"

    def __init__(self, cfg: S3Config, client=None):  # client injectable for tests
        self.bucket = cfg.bucket
        self.prefix = cfg.prefix
        self.location = f"s3://{cfg.bucket}/{cfg.prefix} @ {cfg.endpoint_url or 'aws'}"
        self.s3 = client or boto3.client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            region_name=cfg.region,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            verify=cfg.ca_bundle or True,
            config=BotoConfig(s3={"addressing_style": cfg.addressing_style},
                              retries={"max_attempts": 5, "mode": "standard"},
                              connect_timeout=15, read_timeout=120),
        )

    def _full(self, key: str) -> str:
        return self.prefix + key

    def list_daily(self) -> list[StoredArchive]:
        out: list[StoredArchive] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._full("daily/")):
            for obj in page.get("Contents", []):
                a = _parse_key(obj["Key"][len(self.prefix):], obj["Size"])
                if a:
                    out.append(a)
        return sorted(out, key=lambda a: (a.source_date, a.key))

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._full(key))
            return True
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise

    def put_verified(self, key: str, path: Path, archive_sha256: str, metadata: dict[str, str]) -> str:
        """Upload with an x-amz-checksum-sha256 (the server rejects a corrupted body), then confirm the
        stored size and checksum. Refuses to overwrite an existing object."""
        full = self._full(key)
        if self.exists(key):
            raise ArchiveStoreError(f"refusing to overwrite existing archive {full}")
        sha_b64 = base64.b64encode(bytes.fromhex(archive_sha256)).decode()
        size = path.stat().st_size
        with path.open("rb") as body:
            self.s3.put_object(Bucket=self.bucket, Key=full, Body=body, ContentLength=size,
                               ContentType="application/zip", ChecksumSHA256=sha_b64,
                               Metadata={**metadata, "archive-sha256": archive_sha256})
        head = self.s3.head_object(Bucket=self.bucket, Key=full, ChecksumMode="ENABLED")
        if head.get("ContentLength") != size:
            raise ArchiveStoreError(f"size mismatch after upload of {full}: {head.get('ContentLength')} != {size}")
        stored = head.get("ChecksumSHA256")
        meta_sha = head.get("Metadata", {}).get("archive-sha256")
        if stored is not None and stored.split("-")[0] != sha_b64:
            raise ArchiveStoreError(f"checksum mismatch after upload of {full}")
        if stored is None and meta_sha != archive_sha256:
            raise ArchiveStoreError(f"server returned no checksum and metadata mismatch for {full}")
        return key

    def get(self, key: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(self.bucket, self._full(key), str(dest))
        return dest

    def metadata(self, key: str) -> dict[str, str]:
        return dict(self.s3.head_object(Bucket=self.bucket, Key=self._full(key)).get("Metadata", {}))


def open_store(cfg: Config) -> ArchiveStore:
    """S3 when configured, otherwise a filesystem store under <data_dir>/archives."""
    if cfg.archive_backend == "s3":
        return S3ArchiveStore(cfg.require_s3())
    return FilesystemArchiveStore(cfg.archive_dir)
