"""API keys: issuing, verification cache, per-worker rate limits, and usage aggregation.

- Keys look like `wdj_` + 32 random bytes in base62. Only a display prefix and the SHA-256 of the
  full key are stored; keys are high-entropy, so a fast hash is appropriate.
- Each API worker caches valid key hashes from admin.db and refreshes them every 30 seconds, so a
  revocation takes effect within 30 seconds without a database read per request.
- Rate limits are enforced per worker (limit / workers, at least 1 per minute).
- Usage counters are aggregated in memory and flushed to admin.db about once a minute.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from whodatjet import db

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
PREFIX_LEN = 12  # "wdj_" + 8 characters
SCOPES = ("query", "monitor")
REFRESH_SECONDS = 30.0


def _base62(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, r = divmod(n, 62)
        out.append(ALPHABET[r])
    return "".join(reversed(out)).rjust(43, "0")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class IssuedKey:
    id: int
    key: str  # shown once; never stored
    prefix: str


def create_key(conn: sqlite3.Connection, *, name: str, scope: str = "query", created_by: str,
               owner_contact: str | None = None, expires_at: date | None = None,
               rate_limit_per_min: int | None = None, note: str | None = None) -> IssuedKey:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    if not name.strip():
        raise ValueError("name is required")
    key = "wdj_" + _base62(secrets.token_bytes(32))
    prefix = key[:PREFIX_LEN]
    cur = conn.execute(
        "INSERT INTO api_key (name, owner_contact, key_prefix, key_hash, scope, created_at, created_by, expires_at, "
        "rate_limit_per_min, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name.strip(), owner_contact, prefix, hash_key(key), scope, _now(), created_by,
         expires_at.isoformat() if expires_at else None, rate_limit_per_min, note))
    return IssuedKey(int(cur.lastrowid or 0), key, prefix)


def revoke_key(conn: sqlite3.Connection, ident: str, *, revoked_by: str) -> int:
    """Revoke by numeric id or display prefix. Returns the number of keys revoked (0 or 1)."""
    column = "id" if ident.isdigit() else "key_prefix"
    cur = conn.execute(f"UPDATE api_key SET revoked_at = ?, revoked_by = ? WHERE {column} = ? AND revoked_at IS NULL",
                       (_now(), revoked_by, ident))
    return cur.rowcount


def list_keys(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT k.id, k.name, k.owner_contact, k.key_prefix, k.scope, k.created_at, k.created_by, k.expires_at,
                  k.revoked_at, k.rate_limit_per_min, k.last_used_at,
                  COALESCE((SELECT SUM(requests) FROM api_usage_daily u WHERE u.api_key_id = k.id
                            AND u.day >= date('now','-1 day')), 0) AS requests_24h,
                  COALESCE((SELECT SUM(requests) FROM api_usage_daily u WHERE u.api_key_id = k.id
                            AND u.day >= date('now','-30 day')), 0) AS requests_30d
           FROM api_key k ORDER BY k.id""").fetchall()


# ---------------------------------------------------------------- verification

@dataclass(frozen=True)
class KeyInfo:
    id: int
    scope: str
    expires_at: str | None
    rate_limit_per_min: int | None


class KeyCache:
    def __init__(self, admin_db: Path, *, refresh_seconds: float = REFRESH_SECONDS, clock=time.monotonic):
        self.admin_db = admin_db
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self._keys: dict[str, KeyInfo] = {}
        self._loaded_at = -math.inf
        self._lock = threading.Lock()

    def _load(self) -> None:
        conn = db.connect_reader(self.admin_db)
        try:
            rows = conn.execute("SELECT id, key_hash, scope, expires_at, rate_limit_per_min FROM api_key "
                                "WHERE revoked_at IS NULL").fetchall()
        finally:
            conn.close()
        self._keys = {r["key_hash"]: KeyInfo(r["id"], r["scope"], r["expires_at"], r["rate_limit_per_min"])
                      for r in rows}

    def refresh_if_due(self) -> None:
        if self.clock() - self._loaded_at < self.refresh_seconds:
            return
        with self._lock:
            if self.clock() - self._loaded_at >= self.refresh_seconds:
                self._load()
                self._loaded_at = self.clock()

    def verify(self, presented: str | None, today: date) -> KeyInfo | None:
        if not presented or not presented.startswith("wdj_"):
            return None
        self.refresh_if_due()
        # Lookup is keyed on the SHA-256 of the presented key, so lookup timing reveals nothing about
        # stored keys (an attacker would need a preimage); no separate constant-time compare is needed.
        info = self._keys.get(hash_key(presented))
        if info is None:
            return None
        if info.expires_at and today.isoformat() > info.expires_at:
            return None
        return info


class RateLimiter:
    """Fixed one-minute windows per key, per worker."""

    def __init__(self, workers: int, clock=time.time):
        self.workers = max(1, workers)
        self.clock = clock
        self._windows: dict[int, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def per_worker_limit(self, limit_per_min: int) -> int:
        return max(1, limit_per_min // self.workers)

    def check(self, key: KeyInfo) -> int | None:
        """Returns None if allowed, else seconds until the window resets."""
        if not key.rate_limit_per_min:
            return None
        now = self.clock()
        window = int(now // 60)
        with self._lock:
            w, count = self._windows.get(key.id, (window, 0))
            if w != window:
                w, count = window, 0
            if count >= self.per_worker_limit(key.rate_limit_per_min):
                return max(1, int(60 - now % 60))
            self._windows[key.id] = (w, count + 1)
        return None


# ---------------------------------------------------------------- usage

@dataclass
class _Minute:
    requests: int = 0
    latencies: list[float] = field(default_factory=list)
    status_401: int = 0
    status_429: int = 0
    status_503: int = 0


class UsageRecorder:
    def __init__(self, admin_db: Path, worker: str):
        self.admin_db = admin_db
        self.worker = worker
        self._lock = threading.Lock()
        self._daily: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
        self._last_used: dict[int, str] = {}
        self._minutes: dict[str, _Minute] = defaultdict(_Minute)

    def record(self, key_id: int | None, status: int, latency_ms: float) -> None:
        now = datetime.now(UTC)
        with self._lock:
            m = self._minutes[now.strftime("%Y-%m-%dT%H:%M")]
            m.requests += 1
            m.latencies.append(latency_ms)
            if status == 401:
                m.status_401 += 1
            elif status == 429:
                m.status_429 += 1
            elif status == 503:
                m.status_503 += 1
            if key_id is not None:
                counts = self._daily[(key_id, now.date().isoformat())]
                counts[0] += 1
                counts[1] += status >= 400
                self._last_used[key_id] = now.isoformat(timespec="seconds")

    def flush(self) -> None:
        with self._lock:
            daily, self._daily = self._daily, defaultdict(lambda: [0, 0])
            last_used, self._last_used = self._last_used, {}
            minutes, self._minutes = self._minutes, defaultdict(_Minute)
        if not (daily or minutes):
            return
        conn = db.connect_writer(self.admin_db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO api_usage_daily (api_key_id, day, requests, errors) VALUES (?,?,?,?) "
                "ON CONFLICT (api_key_id, day) DO UPDATE SET requests = requests + excluded.requests, "
                "errors = errors + excluded.errors",
                [(k, d, c[0], c[1]) for (k, d), c in daily.items()])
            conn.executemany("UPDATE api_key SET last_used_at = max(COALESCE(last_used_at, ''), ?) WHERE id = ?",
                             [(t, k) for k, t in last_used.items()])
            rows = []
            for minute, m in minutes.items():
                lat = sorted(m.latencies)
                p95 = lat[min(len(lat) - 1, int(math.ceil(0.95 * len(lat))) - 1)] if lat else None
                rows.append((minute, self.worker, m.requests, p95, m.status_401, m.status_429, m.status_503))
            conn.executemany(
                "INSERT INTO api_metrics_minute (minute, worker, requests, p95_ms, status_401, status_429, status_503) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT (minute, worker) DO UPDATE SET "
                "requests = requests + excluded.requests, p95_ms = max(COALESCE(p95_ms, 0), excluded.p95_ms), "
                "status_401 = status_401 + excluded.status_401, status_429 = status_429 + excluded.status_429, "
                "status_503 = status_503 + excluded.status_503", rows)
            conn.execute("DELETE FROM api_metrics_minute WHERE minute < strftime('%Y-%m-%dT%H:%M', 'now', '-7 days')")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
