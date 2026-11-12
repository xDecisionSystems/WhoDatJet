"""Import retained archives into the registry database (plan: "Reconciliation rules").

Each archive is applied relative to the previous imported snapshot in ONE transaction:
aircraft models, aircraft identities, tail numbers and registration-record versions.
Tail-number assignment intervals are reconciled in `timeline` (plan Phase 3).

Snapshots are ordered by (source_date, downloaded_at). An archive that would sort before the
latest imported snapshot is refused; adding it requires `rebuild` (replay all archives in order).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import sqlite3
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from whodatjet import timeline
from whodatjet.archive_store import ArchiveStore, StoredArchive, sha256_file
from whodatjet.faa_client import CHICAGO
from whodatjet.normalize import (
    clean,
    faa_date,
    is_placeholder_serial,
    normalize_serial,
    to_int,
    try_normalize_n_number,
)

log = logging.getLogger(__name__)
REQUIRED_SCHEMA_VERSION = 2

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "MASTER.txt": ("N-NUMBER", "SERIAL NUMBER", "MFR MDL CODE", "YEAR MFR", "TYPE REGISTRANT", "NAME",
                   "STREET", "STREET2", "CITY", "STATE", "ZIP CODE", "COUNTRY", "LAST ACTION DATE",
                   "CERT ISSUE DATE", "STATUS CODE", "EXPIRATION DATE", "UNIQUE ID", "MODE S CODE HEX"),
    "DEREG.txt": ("N-NUMBER", "SERIAL-NUMBER", "MFR-MDL-CODE", "STATUS-CODE", "NAME", "STREET-MAIL",
                  "STREET2-MAIL", "CITY-MAIL", "STATE-ABBREV-MAIL", "ZIP-CODE-MAIL", "COUNTRY-MAIL",
                  "YEAR-MFR", "CANCEL-DATE", "LAST-ACT-DATE", "CERT-ISSUE-DATE", "MODE S CODE HEX"),
    "ACFTREF.txt": ("CODE", "MFR", "MODEL", "TYPE-ACFT", "TYPE-ENG", "AC-CAT", "NO-ENG", "NO-SEATS", "AC-WEIGHT"),
}

# Stored registration_record columns, in the order produced by the row builders below.
RECORD_COLUMNS = ("n_number_raw", "faa_unique_id", "mfr_mdl_code", "serial_number_raw", "year_mfr", "status_code",
                  "registrant_type", "registrant_name", "street", "street2", "city", "state", "zip_code",
                  "country", "certificate_issue_date", "expiration_date", "cancel_date", "last_action_date",
                  "mode_s_code_hex")


class ImportError_(RuntimeError):  # noqa: N801 - avoid shadowing builtin ImportError
    pass


class SchemaMismatch(ImportError_):
    """A required file or column is missing (FAA format changed)."""


class OutOfOrder(ImportError_):
    pass


@dataclass(frozen=True)
class SnapshotMeta:
    source_date: str
    source_date_basis: str
    downloaded_at: str
    archive_key: str
    archive_bytes: int
    archive_sha256: str
    content_sha256: str
    http_last_modified: str | None = None

    @property
    def order_key(self) -> tuple[str, str]:
        return (self.source_date, self.downloaded_at)


@dataclass
class ImportResult:
    archive_key: str
    status: str  # imported | duplicate | failed | out_of_order
    snapshot_id: int | None = None
    error: str | None = None
    stats: dict[str, int] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(CHICAGO).isoformat(timespec="seconds")


def _hash(fields: tuple) -> str:
    return hashlib.sha256("\x1f".join("" if f is None else str(f) for f in fields).encode()).hexdigest()[:32]


# ---------------------------------------------------------------- parsing

def _read_csv(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    members = {i.filename.upper(): i.filename for i in zf.infolist()}
    if name.upper() not in members:
        raise SchemaMismatch(f"required file missing: {name}")
    with zf.open(members[name.upper()]) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.reader(text)
        header = [h.strip() for h in next(reader)]
        missing = [c for c in REQUIRED_COLUMNS[name] if c not in header]
        if missing:
            raise SchemaMismatch(f"{name} is missing required columns: {', '.join(missing)}")
        idx = {h: i for i, h in enumerate(header) if h}
        for row in reader:
            if not row:
                continue
            yield {h: (row[i] if i < len(row) else "") for h, i in idx.items()}


def master_record(r: dict[str, str]) -> tuple:
    return (clean(r["N-NUMBER"]), clean(r["UNIQUE ID"]), clean(r["MFR MDL CODE"]), clean(r["SERIAL NUMBER"]),
            clean(r["YEAR MFR"]), clean(r["STATUS CODE"]), clean(r["TYPE REGISTRANT"]), clean(r["NAME"]),
            clean(r["STREET"]), clean(r["STREET2"]), clean(r["CITY"]), clean(r["STATE"]), clean(r["ZIP CODE"]),
            clean(r["COUNTRY"]), faa_date(r["CERT ISSUE DATE"]), faa_date(r["EXPIRATION DATE"]), None,
            faa_date(r["LAST ACTION DATE"]), clean(r["MODE S CODE HEX"]))


def dereg_record(r: dict[str, str]) -> tuple:
    return (clean(r["N-NUMBER"]), None, clean(r["MFR-MDL-CODE"]), clean(r["SERIAL-NUMBER"]), clean(r["YEAR-MFR"]),
            clean(r["STATUS-CODE"]), None, clean(r["NAME"]), clean(r["STREET-MAIL"]), clean(r["STREET2-MAIL"]),
            clean(r["CITY-MAIL"]), clean(r["STATE-ABBREV-MAIL"]), clean(r["ZIP-CODE-MAIL"]),
            clean(r["COUNTRY-MAIL"]), faa_date(r["CERT-ISSUE-DATE"]), None, faa_date(r["CANCEL-DATE"]),
            faa_date(r["LAST-ACT-DATE"]), clean(r["MODE S CODE HEX"]))


def model_row(r: dict[str, str]) -> tuple:
    return (clean(r["CODE"]), clean(r["MFR"]), clean(r["MODEL"]), clean(r["TYPE-ACFT"]), clean(r["TYPE-ENG"]),
            clean(r["AC-CAT"]), to_int(r["NO-ENG"]), to_int(r["NO-SEATS"]), clean(r["AC-WEIGHT"]))


R = {c: i for i, c in enumerate(RECORD_COLUMNS)}


# ---------------------------------------------------------------- apply

class _Apply:
    """State for applying one snapshot inside an open transaction."""

    def __init__(self, conn: sqlite3.Connection, snapshot_id: int, prev_id: int | None):
        self.conn, self.sid, self.prev = conn, snapshot_id, prev_id
        self.stats: Counter[str] = Counter()
        self.tails = dict(conn.execute("SELECT n_number, id FROM tail_number").fetchall())
        self.by_uid: dict[str, int] = {}
        self.by_key: dict[str, int] = {}
        self.by_ms: dict[tuple[str, str], list[int]] = {}
        self.attrs: dict[int, tuple[str, str, str | None]] = {}  # id -> (code, serial_norm, uid)
        for aid, uid, code, sn, key in conn.execute(
                "SELECT id, faa_unique_id, mfr_mdl_code, serial_number_norm, identity_key FROM aircraft"):
            self._index(aid, uid, code, sn, key)

    def _index(self, aid: int, uid: str | None, code: str, sn: str | None, key: str) -> None:
        self.by_key[key] = aid
        if uid:
            self.by_uid[uid] = aid
        if sn and not is_placeholder_serial(sn):
            self.by_ms.setdefault((code, sn), []).append(aid)
        self.attrs[aid] = (code, sn or "", uid)

    def _new_aircraft(self, uid: str | None, code: str, serial_raw: str | None, sn: str, basis: str,
                      key: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO aircraft (faa_unique_id, mfr_mdl_code, serial_number_raw, serial_number_norm, "
            "identity_basis, identity_key, first_seen_snapshot_id) VALUES (?,?,?,?,?,?,?)",
            (uid, code, serial_raw, sn or None, basis, key, self.sid))
        aid = int(cur.lastrowid or 0)
        self._index(aid, uid, code, sn, key)
        self.stats[f"aircraft_new_{basis}"] += 1
        return aid

    def _event(self, kind: str, aircraft_id: int | None, **detail) -> None:
        self.conn.execute("INSERT INTO identity_event (snapshot_id, kind, aircraft_id, detail_json) VALUES (?,?,?,?)",
                          (self.sid, kind, aircraft_id, json.dumps(detail, sort_keys=True)))
        self.stats[f"event_{kind}"] += 1

    def _tail(self, raw: str | None) -> int | None:
        n = try_normalize_n_number(raw or "")
        if n is None:
            self.stats["invalid_n_number"] += 1
            return None
        if n not in self.tails:
            self.tails[n] = int(self.conn.execute("INSERT INTO tail_number (n_number) VALUES (?)", (n,)).lastrowid or 0)
        return self.tails[n]

    # -- models
    def models(self, rows: list[tuple]) -> None:
        self._versioned("aircraft_model", "row_hash",
                        ("mfr_mdl_code", "manufacturer_name", "model_name", "aircraft_type", "engine_type",
                         "aircraft_category", "engine_count", "seats", "weight_class"),
                        rows, where="1=1", stat="model")

    # -- master
    def master_identity(self, rec: tuple, current_uids: set[str]) -> int:
        uid, code, serial_raw = rec[R["faa_unique_id"]], rec[R["mfr_mdl_code"]] or "", rec[R["serial_number_raw"]]
        sn = normalize_serial(serial_raw or "")
        if not uid:  # not observed in Phase 0; fall back to a source-row identity
            self.stats["master_missing_unique_id"] += 1
            key = f"R|{rec[R['n_number_raw']]}|{rec[R['certificate_issue_date']]}|{code}"
            return self.by_key.get(key) or self._new_aircraft(None, code, serial_raw, sn, "source_row", key)
        if uid in self.by_uid:
            aid = self.by_uid[uid]
            old_code, old_sn, _ = self.attrs[aid]
            if (old_code, old_sn) != (code, sn):
                self._event("unique_id_attributes_changed", aid, unique_id=uid, old=[old_code, old_sn],
                            new=[code, sn])
            return aid
        candidates = [] if is_placeholder_serial(sn) else self.by_ms.get((code, sn), [])
        unlinked = [a for a in candidates if self.attrs[a][2] is None]
        if len(unlinked) == 1:  # a DEREG-only aircraft registered again: attach its UNIQUE ID
            aid = unlinked[0]
            self.conn.execute("UPDATE aircraft SET faa_unique_id = ? WHERE id = ? AND faa_unique_id IS NULL",
                              (uid, aid))
            self.by_uid[uid] = aid
            self.attrs[aid] = (self.attrs[aid][0], self.attrs[aid][1], uid)
            self.stats["aircraft_unique_id_attached"] += 1
            return aid
        vanished = [a for a in candidates if (u := self.attrs[a][2]) and u not in current_uids]
        aid = self._new_aircraft(uid, code, serial_raw, sn, "unique_id", f"U|{uid}")
        if vanished:
            self._event("model_serial_new_unique_id", aid, unique_id=uid, code=code, serial=sn,
                        previous_aircraft=vanished, previous_unique_ids=[self.attrs[a][2] for a in vanished])
        return aid

    def dereg_identity(self, rec: tuple) -> int:
        code, serial_raw = rec[R["mfr_mdl_code"]] or "", rec[R["serial_number_raw"]]
        sn = normalize_serial(serial_raw or "")
        row_key = f"R|{rec[R['n_number_raw']]}|{rec[R['certificate_issue_date']]}|{code}"
        if is_placeholder_serial(sn):
            return self.by_key.get(row_key) or self._new_aircraft(None, code, serial_raw, sn, "source_row", row_key)
        candidates = self.by_ms.get((code, sn), [])
        if len(candidates) == 1:
            self.stats["dereg_linked"] += 1
            return candidates[0]
        if not candidates:
            return self._new_aircraft(None, code, serial_raw, sn, "model_serial", f"S|{code}|{sn}")
        if row_key in self.by_key:
            return self.by_key[row_key]
        aid = self._new_aircraft(None, code, serial_raw, sn, "source_row", row_key)
        self._event("ambiguous_dereg_match", aid, code=code, serial=sn, candidates=candidates)
        return aid

    def records(self, source: str, rows: list[tuple], identity: Callable[[tuple], int]) -> None:
        hashes = [_hash(r) for r in rows]
        open_hashes = {h for (h,) in self.conn.execute(
            "SELECT row_hash FROM registration_record WHERE source_file = ? AND last_seen_snapshot_id IS NULL",
            (source,))}
        present = set(hashes)
        self.stats[f"{source}_duplicate_rows"] += len(hashes) - len(present)
        # Close versions that are no longer present.
        gone = open_hashes - present
        if gone:
            self.conn.executemany(
                "UPDATE registration_record SET last_seen_snapshot_id = ? "
                "WHERE source_file = ? AND row_hash = ? AND last_seen_snapshot_id IS NULL",
                [(self.prev, source, h) for h in gone])
        self.stats[f"{source}_closed"] += len(gone)
        cols = ", ".join(RECORD_COLUMNS)
        sql = (f"INSERT INTO registration_record (source_file, aircraft_id, tail_number_id, {cols}, row_hash, "
               f"first_seen_snapshot_id) VALUES (?, ?, ?, {', '.join('?' * len(RECORD_COLUMNS))}, ?, ?)")
        seen: set[str] = set()
        batch = []
        for rec, h in zip(rows, hashes, strict=True):
            if h in open_hashes or h in seen:
                continue
            seen.add(h)
            batch.append((source, identity(rec), self._tail(rec[R["n_number_raw"]]), *rec, h, self.sid))
            if len(batch) >= 5000:
                self.conn.executemany(sql, batch)
                batch.clear()
        if batch:
            self.conn.executemany(sql, batch)
        self.stats[f"{source}_inserted"] += len(seen)

    def _versioned(self, table: str, hash_col: str, cols: tuple[str, ...], rows: list[tuple], *, where: str,
                   stat: str) -> None:
        hashes = [_hash(r) for r in rows]
        open_hashes = {h for (h,) in self.conn.execute(
            f"SELECT {hash_col} FROM {table} WHERE {where} AND last_seen_snapshot_id IS NULL")}
        present = set(hashes)
        gone = open_hashes - present
        self.conn.executemany(f"UPDATE {table} SET last_seen_snapshot_id = ? WHERE {hash_col} = ? "
                              "AND last_seen_snapshot_id IS NULL", [(self.prev, h) for h in gone])
        new: dict[str, tuple] = {}
        for r, h in zip(rows, hashes, strict=True):
            if h not in open_hashes:
                new.setdefault(h, r)
        self.conn.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}, {hash_col}, first_seen_snapshot_id) "
            f"VALUES ({', '.join('?' * len(cols))}, ?, ?)", [(*r, h, self.sid) for h, r in new.items()])
        self.stats[f"{stat}_inserted"] += len(new)
        self.stats[f"{stat}_closed"] += len(gone)


# ---------------------------------------------------------------- orchestration

def check_schema(conn: sqlite3.Connection) -> None:
    try:
        v = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
    except sqlite3.OperationalError:
        v = None
    if (v or 0) < REQUIRED_SCHEMA_VERSION:
        raise ImportError_("registry database is not migrated; run `whodatjet migrate`")


def ensure_store_row(conn: sqlite3.Connection, store: ArchiveStore) -> int:
    row = conn.execute("SELECT id FROM archive_store WHERE location = ?", (store.location,)).fetchone()
    if row:
        return int(row[0])
    has_primary = conn.execute("SELECT 1 FROM archive_store WHERE role = 'primary'").fetchone()
    cur = conn.execute("INSERT INTO archive_store (role, kind, location, created_at) VALUES (?,?,?,?)",
                       ("replica" if has_primary else "primary", store.kind, store.location, _now()))
    return int(cur.lastrowid or 0)


def latest_imported(conn: sqlite3.Connection) -> tuple[int, str, str] | None:
    row = conn.execute("SELECT id, source_date, downloaded_at FROM snapshot WHERE import_status = 'imported' "
                       "ORDER BY source_date DESC, downloaded_at DESC LIMIT 1").fetchone()
    return (int(row[0]), row[1], row[2]) if row else None


def import_archive(conn: sqlite3.Connection, zip_path: Path, meta: SnapshotMeta, store_id: int) -> ImportResult:
    """Import one local archive copy. Never raises for data problems; returns a failed result instead."""
    check_schema(conn)
    result = ImportResult(meta.archive_key, "failed")
    run_id = int(conn.execute("INSERT INTO ingest_run (started_at, run_date, status, http_last_modified) "
                              "VALUES (?,?,?,?)", (_now(), datetime.now(CHICAGO).date().isoformat(), "running",
                                                   meta.http_last_modified)).lastrowid or 0)
    try:
        existing = conn.execute("SELECT id, import_status FROM snapshot WHERE content_sha256 = ?",
                                (meta.content_sha256,)).fetchone()
        if existing and existing[1] == "imported":
            result.status, result.snapshot_id = "duplicate", int(existing[0])
            return result
        latest = latest_imported(conn)
        if latest and meta.order_key <= (latest[1], latest[2]):
            raise OutOfOrder(f"{meta.archive_key} ({meta.source_date}) sorts before the latest imported snapshot "
                             f"({latest[1]}); run `whodatjet rebuild` to include it")
        if existing:
            sid = int(existing[0])
            conn.execute("UPDATE snapshot SET import_status = 'pending', error_message = NULL WHERE id = ?", (sid,))
        else:
            sid = int(conn.execute(
                "INSERT INTO snapshot (source_date, source_date_basis, downloaded_at, archive_store_id, archive_key, "
                "archive_bytes, archive_sha256, content_sha256, import_status) VALUES (?,?,?,?,?,?,?,?, 'pending')",
                (meta.source_date, meta.source_date_basis, meta.downloaded_at, store_id, meta.archive_key,
                 meta.archive_bytes, meta.archive_sha256, meta.content_sha256)).lastrowid or 0)
        result.snapshot_id = sid
        conn.execute("UPDATE ingest_run SET snapshot_id = ? WHERE id = ?", (sid, run_id))

        with zipfile.ZipFile(zip_path) as zf:
            models = [model_row(r) for r in _read_csv(zf, "ACFTREF.txt")]
            master = [master_record(r) for r in _read_csv(zf, "MASTER.txt")]
            dereg = [dereg_record(r) for r in _read_csv(zf, "DEREG.txt")]

        conn.execute("BEGIN IMMEDIATE")
        try:
            ap = _Apply(conn, sid, latest[0] if latest else None)
            ap.models(models)
            current_uids = {u for r in master if (u := r[R["faa_unique_id"]])}
            ap.records("master", master, lambda rec: ap.master_identity(rec, current_uids))
            ap.records("dereg", dereg, ap.dereg_identity)
            timeline.reconcile(conn, sid, latest[0] if latest else None, ap.stats)
            conn.execute("INSERT OR IGNORE INTO archive_copy (snapshot_id, archive_store_id, verified_at) "
                         "VALUES (?,?,?)", (sid, store_id, _now()))
            conn.execute(
                "UPDATE snapshot SET import_status = 'imported', imported_at = ?, schema_version = ?, "
                "master_rows = ?, dereg_rows = ?, acftref_rows = ?, stats_json = ? WHERE id = ?",
                (_now(), "faa-2024", len(master), len(dereg), len(models), json.dumps(dict(ap.stats)), sid))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        result.status, result.stats = "imported", dict(ap.stats)
        return result
    except Exception as exc:
        result.status = "out_of_order" if isinstance(exc, OutOfOrder) else "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        if result.snapshot_id:
            conn.execute("UPDATE snapshot SET import_status = 'failed', error_message = ? WHERE id = ?",
                         (result.error, result.snapshot_id))
        log.error("import of %s failed: %s", meta.archive_key, result.error)
        return result
    finally:
        conn.execute("UPDATE ingest_run SET status = ?, finished_at = ?, error_message = ? WHERE id = ?",
                     ({"out_of_order": "failed"}.get(result.status, result.status), _now(), result.error, run_id))


def meta_for(store: ArchiveStore, archive: StoredArchive) -> SnapshotMeta:
    md = {k.lower(): v for k, v in store.metadata(archive.key).items()}
    if "archive-sha256" not in md:
        raise ImportError_(f"archive {archive.key} has no archive-sha256 metadata")
    return SnapshotMeta(
        source_date=md.get("source-date") or archive.source_date.isoformat(),
        source_date_basis=md.get("source-date-basis") or "download_date",
        downloaded_at=md.get("downloaded-at") or "",
        archive_key=archive.key,
        archive_bytes=archive.size,
        archive_sha256=md["archive-sha256"],
        content_sha256=archive.content_sha256,
        http_last_modified=md.get("http-last-modified") or None,
    )


def import_pending(conn: sqlite3.Connection, store: ArchiveStore, staging_dir: Path) -> list[ImportResult]:
    """Import every archive in the store that is not yet imported, in snapshot order."""
    check_schema(conn)
    store_id = ensure_store_row(conn, store)
    done = {r[0] for r in conn.execute("SELECT content_sha256 FROM snapshot WHERE import_status = 'imported'")}
    todo = [a for a in store.list_daily() if a.content_sha256 not in done]
    metas = sorted((meta_for(store, a) for a in todo), key=lambda m: m.order_key)
    results = []
    for meta in metas:
        local = staging_dir / f"import-{meta.content_sha256}.zip"
        try:
            store.get(meta.archive_key, local)
            if sha256_file(local) != meta.archive_sha256:
                results.append(ImportResult(meta.archive_key, "failed", error="archive checksum mismatch in store"))
                continue
            results.append(import_archive(conn, local, meta, store_id))
        finally:
            local.unlink(missing_ok=True)
    return results
