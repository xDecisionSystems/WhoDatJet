"""Configuration from environment variables, optionally loaded from a .env file.

Variables already present in the process environment take precedence over the file,
so systemd `EnvironmentFile=` and the CLI's `--env-file` behave the same way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from whodatjet import __version__

DEFAULT_FAA_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
DEFAULT_REQUIRED_FILES = ("MASTER.txt", "ACFTREF.txt", "DEREG.txt")
DEFAULT_USER_AGENT = f"whodatjet/{__version__} (FAA registry history research)"


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing variables."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class S3Config:
    endpoint_url: str | None
    region: str | None
    bucket: str
    prefix: str
    addressing_style: str
    ca_bundle: str | None
    access_key_id: str
    secret_access_key: str

    def __repr__(self) -> str:  # never expose credentials in logs or tracebacks
        return (f"S3Config(endpoint_url={self.endpoint_url!r}, bucket={self.bucket!r}, "
                f"prefix={self.prefix!r}, credentials=<redacted>)")


@dataclass(frozen=True)
class Config:
    faa_url: str
    user_agent: str
    required_files: tuple[str, ...]
    data_dir: Path
    s3: S3Config | None
    download_attempts: int
    download_timeout_s: float
    sqlite_mmap_size: int = 0
    sqlite_cache_size_kib: int = 0
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_workers: int = 1
    api_limit_concurrency: int | None = None  # per worker; overload returns a fast 503
    profile: str = "custom"
    gap_threshold: int = 2  # missed publication days before a span counts as a gap
    stale_days: int = 4  # calendar days after the latest coverage date before data is stale
    archive_backend_setting: str | None = None  # "s3" | "filesystem"; default: s3 if configured
    archive_dir_setting: Path | None = None

    @property
    def archive_backend(self) -> str:
        if self.archive_backend_setting:
            return self.archive_backend_setting
        return "s3" if self.s3 is not None else "filesystem"

    @property
    def archive_dir(self) -> Path:
        return self.archive_dir_setting or self.data_dir / "archives"

    def require_s3(self) -> S3Config:
        if self.s3 is None:
            raise ConfigError("archive store not configured: set WHODATJET_S3_BUCKET, "
                              "WHODATJET_S3_ACCESS_KEY_ID and WHODATJET_S3_SECRET_ACCESS_KEY")
        return self.s3

    @property
    def registry_db_path(self) -> Path:
        return self.data_dir / "whodatjet.db"

    @property
    def admin_db_path(self) -> Path:
        return self.data_dir / "admin.db"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "ingest-runs.jsonl"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "ingest.lock"


class ConfigError(RuntimeError):
    pass


def _load_s3() -> S3Config | None:
    names = ("WHODATJET_S3_BUCKET", "WHODATJET_S3_ACCESS_KEY_ID", "WHODATJET_S3_SECRET_ACCESS_KEY")
    present = [n for n in names if _get(n)]
    if not present:
        return None
    if len(present) != len(names):
        missing = [n for n in names if n not in present]
        raise ConfigError(f"incomplete archive store settings; missing: {', '.join(missing)}")
    prefix = _get("WHODATJET_S3_PREFIX", "") or ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return S3Config(
        endpoint_url=_get("WHODATJET_S3_ENDPOINT_URL"),
        region=_get("WHODATJET_S3_REGION"),
        bucket=_get("WHODATJET_S3_BUCKET") or "",
        prefix=prefix,
        addressing_style=_get("WHODATJET_S3_ADDRESSING_STYLE", "path") or "path",
        ca_bundle=_get("WHODATJET_S3_CA_BUNDLE"),
        access_key_id=_get("WHODATJET_S3_ACCESS_KEY_ID") or "",
        secret_access_key=_get("WHODATJET_S3_SECRET_ACCESS_KEY") or "",
    )


def _int(name: str, default: int) -> int:
    raw = _get(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def load_config() -> Config:
    s3 = _load_s3()
    required = _get("WHODATJET_REQUIRED_FILES")
    return Config(
        faa_url=_get("WHODATJET_FAA_URL", DEFAULT_FAA_URL) or DEFAULT_FAA_URL,
        user_agent=_get("WHODATJET_USER_AGENT", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
        required_files=tuple(f.strip() for f in required.split(",")) if required else DEFAULT_REQUIRED_FILES,
        data_dir=Path(_get("WHODATJET_DATA_DIR", "data") or "data").resolve(),
        s3=s3,
        download_attempts=_int("WHODATJET_DOWNLOAD_ATTEMPTS", 5),
        download_timeout_s=float(_get("WHODATJET_DOWNLOAD_TIMEOUT_S", "600") or 600),
        sqlite_mmap_size=_int("SQLITE_MMAP_SIZE", 0),
        sqlite_cache_size_kib=_int("SQLITE_CACHE_SIZE", 0),
        api_host=_get("WHODATJET_API_HOST", "127.0.0.1") or "127.0.0.1",
        api_port=_int("WHODATJET_API_PORT", 8000),
        api_workers=_int("UVICORN_WORKERS", 1),
        api_limit_concurrency=_int("UVICORN_LIMIT_CONCURRENCY", 0) or None,
        profile=_get("WHODATJET_PROFILE", "custom") or "custom",
        gap_threshold=_int("WHODATJET_GAP_THRESHOLD_DAYS", 2),
        stale_days=_int("WHODATJET_STALE_DAYS", 4),
        archive_backend_setting=_archive_backend(),
        archive_dir_setting=Path(d).resolve() if (d := _get("WHODATJET_ARCHIVE_DIR")) else None,
    )


def _archive_backend() -> str | None:
    value = _get("WHODATJET_ARCHIVE_BACKEND")
    if value is not None and value not in ("s3", "filesystem"):
        raise ConfigError(f"WHODATJET_ARCHIVE_BACKEND must be 's3' or 'filesystem', got {value!r}")
    return value
