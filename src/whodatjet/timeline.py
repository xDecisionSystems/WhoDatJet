"""Tail-number assignment intervals (plan: "Reconciliation rules", step 4).

Runs inside the import transaction, after registration records are applied, so a snapshot's
records and intervals commit together.

Observed assignments (MASTER only) are keyed by (tail number, aircraft, certificate issue date):
- present and open        -> stays open (valid_to NULL; it is current as of the latest snapshot)
- present, no open match  -> new interval, valid_from = this snapshot's source date
- open but absent         -> closed: valid_to = previous snapshot's source date (last verified present),
                             last_seen = previous snapshot, first_absent = this snapshot.
  A changed certificate issue date therefore closes one interval and opens another.

DEREG assignments: one per distinct DEREG registration (tail, aircraft, certificate issue date, cancel
date) with all four present and cancel >= issue; valid_from/valid_to are the FAA's dates.
"""

from __future__ import annotations

import sqlite3
from collections import Counter


def reconcile(conn: sqlite3.Connection, snapshot_id: int, prev_id: int | None, stats: Counter[str]) -> None:
    sdate = conn.execute("SELECT source_date FROM snapshot WHERE id = ?", (snapshot_id,)).fetchone()[0]
    pdate = (conn.execute("SELECT source_date FROM snapshot WHERE id = ?", (prev_id,)).fetchone()[0]
             if prev_id is not None else None)
    _observed(conn, snapshot_id, sdate, prev_id, pdate, stats)
    _dereg(conn, snapshot_id, stats)


def _observed(conn: sqlite3.Connection, sid: int, sdate: str, prev_id: int | None, pdate: str | None,
              stats: Counter[str]) -> None:
    current: dict[tuple, int] = {}
    for rid, tail, aircraft, cert in conn.execute(
            "SELECT id, tail_number_id, aircraft_id, certificate_issue_date FROM registration_record "
            "WHERE source_file = 'master' AND last_seen_snapshot_id IS NULL AND tail_number_id IS NOT NULL "
            "ORDER BY id"):
        current.setdefault((tail, aircraft, cert), rid)
    open_: dict[tuple, int] = {}
    for aid, tail, aircraft, cert in conn.execute(
            "SELECT id, tail_number_id, aircraft_id, certificate_issue_date FROM tail_number_assignment "
            "WHERE basis = 'observed' AND valid_to IS NULL"):
        open_[(tail, aircraft, cert)] = aid

    gone = [aid for key, aid in open_.items() if key not in current]
    if gone:
        if prev_id is None or pdate is None:
            raise RuntimeError("open observed assignments exist but there is no previous snapshot")
        conn.executemany(
            "UPDATE tail_number_assignment SET valid_to = ?, last_seen_snapshot_id = ?, "
            "first_absent_snapshot_id = ? WHERE id = ? AND valid_to IS NULL",
            [(pdate, prev_id, sid, aid) for aid in sorted(gone)])
    new = [(tail, aircraft, sdate, cert, sid, rid) for (tail, aircraft, cert), rid in current.items()
           if (tail, aircraft, cert) not in open_]
    conn.executemany(
        "INSERT INTO tail_number_assignment (tail_number_id, aircraft_id, basis, valid_from, "
        "certificate_issue_date, first_seen_snapshot_id, source_record_id) VALUES (?, ?, 'observed', ?, ?, ?, ?)",
        new)
    stats["observed_opened"] += len(new)
    stats["observed_closed"] += len(gone)


def _dereg(conn: sqlite3.Connection, sid: int, stats: Counter[str]) -> None:
    existing = {tuple(r) for r in conn.execute(
        "SELECT tail_number_id, aircraft_id, valid_from, valid_to FROM tail_number_assignment "
        "WHERE basis = 'dereg_record'")}
    rows = []
    for rid, tail, aircraft, cert, cancel in conn.execute(
            "SELECT id, tail_number_id, aircraft_id, certificate_issue_date, cancel_date FROM registration_record "
            "WHERE source_file = 'dereg' AND first_seen_snapshot_id = ? ORDER BY id", (sid,)):
        if tail is None or cert is None or cancel is None:
            stats["dereg_interval_incomplete"] += 1
            continue
        if cancel < cert:
            stats["dereg_interval_inverted"] += 1
            continue
        key = (tail, aircraft, cert, cancel)
        if key in existing:
            continue
        existing.add(key)
        rows.append((tail, aircraft, cert, cancel, cert, sid, rid))
    conn.executemany(
        "INSERT INTO tail_number_assignment (tail_number_id, aircraft_id, basis, valid_from, valid_to, "
        "certificate_issue_date, first_seen_snapshot_id, source_record_id) "
        "VALUES (?, ?, 'dereg_record', ?, ?, ?, ?, ?)", rows)
    stats["dereg_intervals"] += len(rows)
