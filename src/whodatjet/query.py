"""Read-side queries: as-of lookups, history, aircraft, snapshots (plan: "API").

As-of order for `lookup(n, D)`:
1. An observed assignment covering D.
   Flagged `stale` when D is more than `stale_days` after the latest coverage date (one day past it is
   normal: the FAA publishes nightly).
2. D falls between the last snapshot an assignment was seen and the first snapshot it was absent:
   return that (old) assignment, flagged `bracketed_change`, with both bracketing dates.
3. D is before observed coverage: the current registration from its certificate issue date, if that
   assignment was present in the first snapshot (flag `certificate_basis`).
4. A DEREG assignment covering D (flag `dereg_record`).
5. Otherwise not found, with the reason.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import wraps
from typing import Concatenate

from whodatjet import coverage


def consistent_read[**P, T](fn: Callable[Concatenate[sqlite3.Connection, P], T]
                    ) -> Callable[Concatenate[sqlite3.Connection, P], T]:
    """Hold one WAL snapshot for all SELECTs in a response; preserve caller transactions."""
    @wraps(fn)
    def wrapped(conn: sqlite3.Connection, /, *args: P.args, **kwargs: P.kwargs) -> T:
        if conn.in_transaction:
            return fn(conn, *args, **kwargs)
        conn.execute("BEGIN")
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.rollback()
    return wrapped


FIELDS: tuple[str, ...] = (
    "aircraft_id", "faa_unique_id", "identity_basis", "serial_number", "mfr_mdl_code", "manufacturer_name",
    "model_name", "aircraft_type", "engine_type", "aircraft_category", "engine_count", "seats", "year_mfr",
    "status_code", "registrant_type", "registrant_name", "street", "street2", "city", "state", "zip_code",
    "country", "certificate_issue_date", "expiration_date", "cancel_date", "last_action_date", "mode_s_code_hex",
    "valid_from", "valid_to", "last_seen_date", "first_absent_date",
)
METADATA: tuple[str, ...] = ("n_number", "date", "observation_date", "snapshot_id", "archive_sha256", "basis",
                             "data_confidence", "data_quality_flags")
FLAGS = ("low_identity", "gap", "stale", "dereg_record", "bracketed_change", "certificate_basis")


class QueryError(ValueError):
    """Invalid request parameters (HTTP 400)."""


@dataclass(frozen=True)
class NotFound:
    n_number: str
    date: str
    reason: str  # no_records | before_observed_coverage | in_gap | unassigned
    coverage_start: str | None
    coverage_end: str | None


def parse_fields(raw: str | None) -> tuple[str, ...]:
    if raw is None or raw.strip() in ("", "*"):
        return FIELDS
    names = tuple(dict.fromkeys(f.strip() for f in raw.split(",") if f.strip()))
    unknown = [f for f in names if f not in FIELDS]
    if unknown:
        raise QueryError(f"unknown field(s): {', '.join(unknown)}; accepted: {', '.join(FIELDS)}")
    return names


def confidence(flags: list[str]) -> str:
    if "low_identity" in flags or "gap" in flags:
        return "low"
    return "medium" if flags else "high"


class Snapshots:
    """Imported snapshots in import order (ids increase with snapshot order; see importer)."""

    def __init__(self, conn: sqlite3.Connection):
        rows = conn.execute("SELECT id, source_date, archive_sha256 FROM snapshot WHERE import_status = 'imported' "
                            "ORDER BY id").fetchall()
        self.ids = [r[0] for r in rows]
        self.date = {r[0]: r[1] for r in rows}
        self.sha = {r[0]: r[2] for r in rows}
        self.dates = sorted({date.fromisoformat(r[1]) for r in rows})

    @property
    def start(self) -> str | None:
        return self.date[self.ids[0]] if self.ids else None

    @property
    def end(self) -> str | None:
        return self.date[self.ids[-1]] if self.ids else None

    def on_or_before(self, d: str) -> int | None:
        best = None
        for sid in self.ids:
            if self.date[sid] <= d:
                best = sid
        return best

    def gap_around(self, d: date, threshold: int) -> tuple[date, date] | None:
        for a, b in zip(self.dates, self.dates[1:], strict=False):
            if a < d < b:
                return (a, b) if coverage.span_has_gap(a, b, threshold) else None
        return None


_RECORD_COLS = ("r.id AS record_id, r.mfr_mdl_code, r.serial_number_raw AS serial_number, r.year_mfr, r.status_code, "
                "r.registrant_type, r.registrant_name, r.street, r.street2, r.city, r.state, r.zip_code, r.country, "
                "r.certificate_issue_date AS record_certificate_issue_date, r.expiration_date, r.cancel_date, "
                "r.last_action_date, r.mode_s_code_hex, r.first_seen_snapshot_id AS record_first_seen")


def _record_at(conn, aircraft_id: int, tail_id: int, snapshot_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_RECORD_COLS} FROM registration_record r WHERE r.source_file = 'master' AND r.aircraft_id = ? "
        "AND r.tail_number_id = ? AND r.first_seen_snapshot_id <= ? "
        "AND (r.last_seen_snapshot_id IS NULL OR r.last_seen_snapshot_id >= ?) ORDER BY r.id DESC LIMIT 1",
        (aircraft_id, tail_id, snapshot_id, snapshot_id)).fetchone()


def _record_by_id(conn, record_id: int) -> sqlite3.Row | None:
    return conn.execute(f"SELECT {_RECORD_COLS} FROM registration_record r WHERE r.id = ?", (record_id,)).fetchone()


def _model_at(conn, code: str | None, snapshot_id: int) -> sqlite3.Row | None:
    if not code:
        return None
    return conn.execute(
        "SELECT manufacturer_name, model_name, aircraft_type, engine_type, aircraft_category, engine_count, seats "
        "FROM aircraft_model WHERE mfr_mdl_code = ? AND first_seen_snapshot_id <= ? "
        "AND (last_seen_snapshot_id IS NULL OR last_seen_snapshot_id >= ?) ORDER BY id DESC LIMIT 1",
        (code, snapshot_id, snapshot_id)).fetchone()


_ASSIGN_COLS = ("a.id, a.aircraft_id, a.basis, a.valid_from, a.valid_to, a.certificate_issue_date, "
                "a.first_seen_snapshot_id, a.last_seen_snapshot_id, a.first_absent_snapshot_id, a.source_record_id, "
                "ac.faa_unique_id, ac.identity_basis")


def _assignment(conn, where: str, args: tuple, order: str) -> sqlite3.Row | None:
    return conn.execute(f"SELECT {_ASSIGN_COLS} FROM tail_number_assignment a "
                        f"JOIN aircraft ac ON ac.id = a.aircraft_id "
                        f"WHERE {where} ORDER BY {order} LIMIT 1", args).fetchone()


@consistent_read
def lookup(conn: sqlite3.Connection, n_number: str, d: date, *, today: date, gap_threshold: int = 2,
           stale_days: int = coverage.DEFAULT_STALE_DAYS) -> dict | NotFound:
    """`n_number` must already be normalized (with N prefix)."""
    if d > today:
        raise QueryError(f"date {d} is in the future")
    ds = d.isoformat()
    snaps = Snapshots(conn)
    row = conn.execute("SELECT id FROM tail_number WHERE n_number = ?", (n_number,)).fetchone()
    if row is None:
        return NotFound(n_number, ds, "no_records", snaps.start, snaps.end)
    tail = row[0]
    flags: list[str] = []
    a = rec = None
    obs_snapshot: int | None = None
    basis = None

    if snaps.start and ds >= snaps.start:
        a = _assignment(conn, "a.tail_number_id = ? AND a.basis = 'observed' AND a.valid_from <= ? "
                        "AND (a.valid_to IS NULL OR a.valid_to >= ?)", (tail, ds, ds), "a.valid_from DESC")
        if a is not None:
            obs_snapshot = snaps.on_or_before(ds)
        else:
            a = conn.execute(
                f"SELECT {_ASSIGN_COLS} FROM tail_number_assignment a JOIN aircraft ac ON ac.id = a.aircraft_id "
                "JOIN snapshot s ON s.id = a.first_absent_snapshot_id WHERE a.tail_number_id = ? "
                "AND a.basis = 'observed' AND a.valid_to < ? AND s.source_date > ? ORDER BY a.valid_to DESC LIMIT 1",
                (tail, ds, ds)).fetchone()
            if a is not None:
                flags.append("bracketed_change")
                obs_snapshot = a["last_seen_snapshot_id"]
        if a is not None:
            basis = "observed"
            rec = _record_at(conn, a["aircraft_id"], tail, obs_snapshot) if obs_snapshot else None
            if snaps.end and coverage.is_stale(date.fromisoformat(snaps.end), d, stale_days):
                flags.append("stale")
            if snaps.gap_around(d, gap_threshold):
                flags.append("gap")

    if a is None and snaps.start and ds < snaps.start:
        a = _assignment(conn, "a.tail_number_id = ? AND a.basis = 'observed' AND a.first_seen_snapshot_id = ? "
                        "AND a.certificate_issue_date IS NOT NULL AND a.certificate_issue_date <= ?",
                        (tail, snaps.ids[0], ds), "a.certificate_issue_date DESC")
        if a is not None:
            basis, flags = "certificate", ["certificate_basis"]
            obs_snapshot = snaps.ids[0]
            rec = _record_by_id(conn, a["source_record_id"])

    if a is None:
        a = _assignment(conn, "a.tail_number_id = ? AND a.basis = 'dereg_record' AND a.valid_from <= ? "
                        "AND a.valid_to >= ?", (tail, ds, ds), "a.valid_from DESC")
        if a is not None:
            basis, flags = "dereg_record", ["dereg_record"]
            rec = _record_by_id(conn, a["source_record_id"])
            obs_snapshot = rec["record_first_seen"] if rec else None

    if a is None:
        if snaps.start is None or ds < snaps.start:
            reason = "before_observed_coverage"
        elif snaps.gap_around(d, gap_threshold):
            reason = "in_gap"
        else:
            reason = "unassigned"
        return NotFound(n_number, ds, reason, snaps.start, snaps.end)

    if a["identity_basis"] == "source_row":
        flags.insert(0, "low_identity")
    model = _model_at(conn, rec["mfr_mdl_code"] if rec else None, obs_snapshot or 0)
    out: dict = {
        "n_number": n_number, "date": ds,
        "observation_date": snaps.date.get(obs_snapshot) if obs_snapshot else None,
        "snapshot_id": obs_snapshot, "archive_sha256": snaps.sha.get(obs_snapshot) if obs_snapshot else None,
        "basis": basis, "data_quality_flags": flags, "data_confidence": confidence(flags),
        "aircraft_id": a["aircraft_id"], "faa_unique_id": a["faa_unique_id"], "identity_basis": a["identity_basis"],
        "valid_from": a["valid_from"], "valid_to": a["valid_to"],
        "certificate_issue_date": a["certificate_issue_date"],
        "last_seen_date": snaps.date.get(a["last_seen_snapshot_id"]),
        "first_absent_date": snaps.date.get(a["first_absent_snapshot_id"]),
    }
    if rec is not None:
        for k in ("mfr_mdl_code", "serial_number", "year_mfr", "status_code", "registrant_type", "registrant_name",
                  "street", "street2", "city", "state", "zip_code", "country", "expiration_date", "cancel_date",
                  "last_action_date", "mode_s_code_hex"):
            out[k] = rec[k]
    for k in ("manufacturer_name", "model_name", "aircraft_type", "engine_type", "aircraft_category", "engine_count",
              "seats"):
        out[k] = model[k] if model is not None else None
    return out


def select_fields(result: dict, fields: tuple[str, ...]) -> dict:
    return {k: result.get(k) for k in METADATA + fields}


@consistent_read
def history(conn: sqlite3.Connection, n_number: str, *, limit: int, offset: int, gap_threshold: int = 2) -> dict | None:
    row = conn.execute("SELECT id FROM tail_number WHERE n_number = ?", (n_number,)).fetchone()
    if row is None:
        return None
    snaps = Snapshots(conn)
    rows = conn.execute(
        f"""SELECT {_ASSIGN_COLS}, r.mfr_mdl_code, r.serial_number_raw
            FROM tail_number_assignment a JOIN aircraft ac ON ac.id = a.aircraft_id
            LEFT JOIN registration_record r ON r.id = a.source_record_id
            WHERE a.tail_number_id = ? ORDER BY a.valid_from DESC, a.id DESC LIMIT ? OFFSET ?""",
        (row[0], limit + 1, offset)).fetchall()
    items = []
    for a in rows[:limit]:
        model = _model_at(conn, a["mfr_mdl_code"], a["first_seen_snapshot_id"] or 0)
        flags = []
        if a["identity_basis"] == "source_row":
            flags.append("low_identity")
        if a["basis"] == "dereg_record":
            flags.append("dereg_record")
        ls, fa = snaps.date.get(a["last_seen_snapshot_id"]), snaps.date.get(a["first_absent_snapshot_id"])
        if ls and fa and coverage.span_has_gap(date.fromisoformat(ls), date.fromisoformat(fa), gap_threshold):
            flags.append("gap")
        items.append({
            "basis": a["basis"], "aircraft_id": a["aircraft_id"], "faa_unique_id": a["faa_unique_id"],
            "identity_basis": a["identity_basis"], "serial_number": a["serial_number_raw"],
            "mfr_mdl_code": a["mfr_mdl_code"],
            "manufacturer_name": model["manufacturer_name"] if model else None,
            "model_name": model["model_name"] if model else None,
            "valid_from": a["valid_from"], "valid_to": a["valid_to"],
            "certificate_issue_date": a["certificate_issue_date"],
            "first_seen_date": snaps.date.get(a["first_seen_snapshot_id"]),
            "last_seen_date": ls, "first_absent_date": fa,
            "data_quality_flags": flags, "data_confidence": confidence(flags),
        })
    return {"n_number": n_number, "items": items, "next_offset": offset + limit if len(rows) > limit else None}


@consistent_read
def aircraft(conn: sqlite3.Connection, aircraft_id: int) -> dict | None:
    ac = conn.execute("SELECT * FROM aircraft WHERE id = ?", (aircraft_id,)).fetchone()
    if ac is None:
        return None
    snaps = Snapshots(conn)
    models = conn.execute(
        """SELECT r.mfr_mdl_code, MIN(r.first_seen_snapshot_id) AS first_seen, m.manufacturer_name, m.model_name
           FROM registration_record r
           LEFT JOIN aircraft_model m ON m.mfr_mdl_code = r.mfr_mdl_code AND m.last_seen_snapshot_id IS NULL
           WHERE r.aircraft_id = ? GROUP BY r.mfr_mdl_code ORDER BY first_seen""", (aircraft_id,)).fetchall()
    tails = conn.execute(
        """SELECT t.n_number, a.basis, a.valid_from, a.valid_to, a.certificate_issue_date
           FROM tail_number_assignment a JOIN tail_number t ON t.id = a.tail_number_id
           WHERE a.aircraft_id = ? ORDER BY a.valid_from""", (aircraft_id,)).fetchall()
    flags = ["low_identity"] if ac["identity_basis"] == "source_row" else []
    return {
        "aircraft_id": ac["id"], "faa_unique_id": ac["faa_unique_id"], "identity_basis": ac["identity_basis"],
        "serial_number": ac["serial_number_raw"], "mfr_mdl_code": ac["mfr_mdl_code"],
        "first_seen_date": snaps.date.get(ac["first_seen_snapshot_id"]),
        "data_quality_flags": flags, "data_confidence": confidence(flags),
        "model_history": [{"mfr_mdl_code": m["mfr_mdl_code"], "manufacturer_name": m["manufacturer_name"],
                           "model_name": m["model_name"], "first_seen_date": snaps.date.get(m["first_seen"])}
                          for m in models],
        "n_number_history": [dict(t) for t in tails],
    }


@consistent_read
def snapshots(conn: sqlite3.Connection, *, gap_threshold: int = 2, limit: int = 1000) -> dict:
    snaps = conn.execute(
        "SELECT id, source_date, source_date_basis, downloaded_at, import_status, imported_at, archive_key, "
        "archive_bytes, archive_sha256, master_rows, dereg_rows, acftref_rows, error_message "
        "FROM snapshot ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    runs = conn.execute("SELECT id, started_at, finished_at, run_date, status, snapshot_id, error_message "
                        "FROM ingest_run ORDER BY id DESC LIMIT 50").fetchall()
    gaps = coverage.find_gaps(coverage.coverage_dates(conn), gap_threshold)
    return {
        "snapshots": [dict(s) for s in snaps],
        "ingest_runs": [dict(r) for r in runs],
        "gaps": [{"after": g.after.isoformat(), "before": g.before.isoformat(),
                  "missed_publication_days": g.missed_publication_days} for g in gaps],
    }
