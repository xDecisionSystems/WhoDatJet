from __future__ import annotations

import hashlib
from datetime import date

from tests.fixture_archive import Archive
from tests.test_importer import DEREG, MASTER, one, run
from whodatjet import importer
from whodatjet.archive_store import FilesystemArchiveStore, daily_key
from whodatjet.importer import import_pending
from whodatjet.normalize import faa_date
from whodatjet.rebuild import rebuild

D1, D2, D3 = "2026-11-11", "2026-11-12", "2026-11-13"


def assignments(conn, n_number, basis="observed"):
    return [tuple(r) for r in conn.execute(
        """SELECT a.aircraft_id, a.valid_from, a.valid_to, a.certificate_issue_date,
                  a.last_seen_snapshot_id, a.first_absent_snapshot_id
           FROM tail_number_assignment a JOIN tail_number t ON t.id = a.tail_number_id
           WHERE t.n_number = ? AND a.basis = ? ORDER BY a.valid_from, a.id""", (n_number, basis))]


def aircraft_of(conn, uid):
    return one(conn, "SELECT id FROM aircraft WHERE faa_unique_id = ?", uid)


def pick(a: Archive, i=0):
    row = a.rows(MASTER)[i]
    return row, "N" + a.get(MASTER, row, "N-NUMBER"), a.get(MASTER, row, "UNIQUE ID")


def test_first_snapshot_opens_one_interval_per_master_row(conn, store_id, tmp_path):
    a = Archive()
    r = run(conn, store_id, tmp_path, a, D1)
    assert r.stats["observed_opened"] == len(a.rows(MASTER))
    assert one(conn, "SELECT count(*) FROM tail_number_assignment WHERE basis='observed' AND valid_to IS NULL") \
        == len(a.rows(MASTER))


def test_unchanged_and_attribute_changes_keep_interval_open(conn, store_id, tmp_path):
    a = Archive()
    _, n, uid = pick(a)
    run(conn, store_id, tmp_path, a, D1)
    b = a.copy()
    b.set(MASTER, b.rows(MASTER)[0], "STATUS CODE", "M")   # attribute change, same pair + cert date
    r = run(conn, store_id, tmp_path, b, D2)
    assert r.stats.get("observed_opened", 0) == 0 and r.stats.get("observed_closed", 0) == 0
    cert = faa_date(a.get(MASTER, a.rows(MASTER)[0], "CERT ISSUE DATE"))
    assert assignments(conn, n) == [(aircraft_of(conn, uid), D1, None, cert, None, None)]


def test_changed_n_number_closes_old_and_opens_new(conn, store_id, tmp_path):
    a = Archive()
    _, old_n, uid = pick(a)
    run(conn, store_id, tmp_path, a, D1)
    b = a.copy()
    b.set(MASTER, b.rows(MASTER)[0], "N-NUMBER", "994ZZ")
    run(conn, store_id, tmp_path, b, D2)
    ac = aircraft_of(conn, uid)
    old = assignments(conn, old_n)
    assert [(x[0], x[1], x[2], x[4], x[5]) for x in old] == [(ac, D1, D1, 1, 2)]
    assert [(x[0], x[1], x[2]) for x in assignments(conn, "N994ZZ")] == [(ac, D2, None)]


def test_reassignment_to_different_aircraft(conn, store_id, tmp_path):
    a = Archive()
    _, n, uid = pick(a)
    run(conn, store_id, tmp_path, a, D1)
    b = a.copy()
    row = b.rows(MASTER)[0]
    b.set(MASTER, row, "UNIQUE ID", "55555555")
    b.set(MASTER, row, "SERIAL NUMBER", "OTHER-AIRFRAME-9")
    run(conn, store_id, tmp_path, b, D2)
    hist = assignments(conn, n)
    assert len(hist) == 2
    assert (hist[0][0], hist[0][2]) == (aircraft_of(conn, uid), D1)
    assert (hist[1][0], hist[1][1], hist[1][2]) == (aircraft_of(conn, "55555555"), D2, None)


def test_deregistration_closes_observed_and_adds_dereg_interval(conn, store_id, tmp_path):
    a = Archive()
    row, n, uid = pick(a)
    run(conn, store_id, tmp_path, a, D1)
    b = a.copy()
    b.remove(MASTER, b.rows(MASTER)[0])
    d = list(b.rows(DEREG)[0])
    b.set(DEREG, d, "N-NUMBER", n[1:])
    b.set(DEREG, d, "MFR-MDL-CODE", a.get(MASTER, row, "MFR MDL CODE"))
    b.set(DEREG, d, "SERIAL-NUMBER", a.get(MASTER, row, "SERIAL NUMBER"))
    b.set(DEREG, d, "CERT-ISSUE-DATE", "20200101")
    b.set(DEREG, d, "CANCEL-DATE", "20261112")
    b.add(DEREG, d)
    r = run(conn, store_id, tmp_path, b, D2)
    assert r.stats["observed_closed"] == 1
    ac = aircraft_of(conn, uid)
    assert [(x[0], x[2]) for x in assignments(conn, n)] == [(ac, D1)]
    assert [(x[0], x[1], x[2]) for x in assignments(conn, n, "dereg_record")] == [(ac, "2020-01-01", "2026-11-12")]


def test_reregistration_of_same_pair_with_new_certificate(conn, store_id, tmp_path):
    a = Archive()
    _, n, uid = pick(a, 1)
    run(conn, store_id, tmp_path, a, D1)
    b = a.copy()
    b.set(MASTER, b.rows(MASTER)[1], "CERT ISSUE DATE", "20261112")
    run(conn, store_id, tmp_path, b, D2)
    hist = assignments(conn, n)
    ac = aircraft_of(conn, uid)
    assert [(x[0], x[1], x[2], x[3]) for x in hist][1] == (ac, D2, None, "2026-11-12")
    assert hist[0][2] == D1 and hist[0][0] == ac


def test_dereg_intervals_skip_incomplete_and_inverted(conn, store_id, tmp_path):
    a = Archive()
    r = run(conn, store_id, tmp_path, a, D1)
    rows = a.rows(DEREG)
    complete = [x for x in rows if a.get(DEREG, x, "CERT-ISSUE-DATE") and a.get(DEREG, x, "CANCEL-DATE")]
    inverted = [x for x in complete if a.get(DEREG, x, "CANCEL-DATE") < a.get(DEREG, x, "CERT-ISSUE-DATE")]
    assert r.stats["dereg_interval_inverted"] == len(inverted) > 0
    assert r.stats["dereg_interval_incomplete"] == len(rows) - len(complete) > 0
    assert r.stats["dereg_intervals"] <= len(complete) - len(inverted)


def test_low_confidence_identity_gets_interval(conn, store_id, tmp_path):
    a = Archive()
    d = list(a.rows(DEREG)[0])
    a.set(DEREG, d, "N-NUMBER", "993ZZ")
    a.set(DEREG, d, "SERIAL-NUMBER", "0")
    a.set(DEREG, d, "CERT-ISSUE-DATE", "19900101")
    a.set(DEREG, d, "CANCEL-DATE", "19950101")
    a.add(DEREG, d)
    run(conn, store_id, tmp_path, a, D1)
    hist = assignments(conn, "N993ZZ", "dereg_record")
    assert len(hist) == 1
    assert one(conn, "SELECT identity_basis FROM aircraft WHERE id = ?", hist[0][0]) == "source_row"


def test_failed_import_does_not_close_intervals(conn, store_id, tmp_path, monkeypatch):
    a = Archive()
    _, n, _ = pick(a)
    run(conn, store_id, tmp_path, a, D1)
    b = a.copy()
    b.remove(MASTER, b.rows(MASTER)[0])
    real = importer.timeline.reconcile

    def boom(*args):
        real(*args)
        raise RuntimeError("crash after reconcile")

    monkeypatch.setattr(importer.timeline, "reconcile", boom)
    assert run(conn, store_id, tmp_path, b, D2).status == "failed"
    assert assignments(conn, n)[0][2] is None  # still open


def _dump(conn):
    tables = {
        "aircraft": "*", "aircraft_model": "*", "tail_number": "*", "identity_event": "*",
        "registration_record": "*", "tail_number_assignment": "*",
        "snapshot": "id, source_date, downloaded_at, archive_key, content_sha256, import_status, stats_json",
    }
    return {t: [tuple(r) for r in conn.execute(f"SELECT {c} FROM {t} ORDER BY id")] for t, c in tables.items()}


def test_rebuild_matches_incremental(conn, tmp_path, monkeypatch):
    store = FilesystemArchiveStore(tmp_path / "archives")
    a = Archive()
    b = a.copy()
    b.set(MASTER, b.rows(MASTER)[0], "N-NUMBER", "994ZZ")          # N-number change
    b.remove(MASTER, b.rows(MASTER)[2])                              # deregistration
    c = b.copy()
    c.set(MASTER, c.rows(MASTER)[1], "CERT ISSUE DATE", "20261113")  # re-registration
    c.set(MASTER, c.rows(MASTER)[3], "UNIQUE ID", "44444444")        # new UNIQUE ID, same airframe

    def put(arc, day):
        p, sha = arc.write(tmp_path / f"{day}.zip")
        content = hashlib.sha256(arc.zip_bytes()).hexdigest()
        store.put_verified(daily_key(date.fromisoformat(day), content), p, sha, {
            "source-date": day, "source-date-basis": "http_last_modified", "downloaded-at": f"{day}T23:59:00-06:00"})

    # Incremental, with a failed attempt on the second archive that is retried later.
    put(a, D1)
    import_pending(conn, store, tmp_path / "staging")
    put(b, D2)
    real = importer.timeline.reconcile
    monkeypatch.setattr(importer.timeline, "reconcile", lambda *x: (_ for _ in ()).throw(RuntimeError("flaky")))
    assert [r.status for r in import_pending(conn, store, tmp_path / "staging")] == ["failed"]
    monkeypatch.setattr(importer.timeline, "reconcile", real)
    assert [r.status for r in import_pending(conn, store, tmp_path / "staging")] == ["imported"]
    put(c, D3)
    import_pending(conn, store, tmp_path / "staging")
    incremental = _dump(conn)
    assert incremental["identity_event"], "expected a model_serial_new_unique_id event"

    live = tmp_path / "rebuilt" / "whodatjet.db"
    live.parent.mkdir()
    results = rebuild(live, store, tmp_path / "staging", tmp_path / "backups")
    assert [r.status for r in results] == ["imported"] * 3
    from whodatjet import db
    rebuilt = _dump(db.connect_reader(live))
    for table in incremental:
        if table == "identity_event":
            strip = lambda rows: [r[:3] + r[4:] for r in rows]  # noqa: E731 - ids of events are stable anyway
            assert strip(rebuilt[table]) == strip(incremental[table])
        else:
            assert rebuilt[table] == incremental[table], table


def test_rebuild_replaces_live_db_and_keeps_backup(tmp_path):
    store = FilesystemArchiveStore(tmp_path / "archives")
    a = Archive()
    p, sha = a.write(tmp_path / "a.zip")
    store.put_verified(daily_key(date(2026, 11, 11), hashlib.sha256(a.zip_bytes()).hexdigest()), p, sha,
                       {"source-date": D1, "source-date-basis": "http_last_modified",
                        "downloaded-at": f"{D1}T23:59:00-06:00"})
    from whodatjet import db
    live = tmp_path / "whodatjet.db"
    c = db.connect_writer(live)
    db.migrate(c, db.load_migrations("registry"))
    c.close()
    rebuild(live, store, tmp_path / "staging", tmp_path / "backups")
    assert len(list((tmp_path / "backups").glob("whodatjet.db.pre-rebuild-*"))) == 1
    r = db.connect_reader(live)
    assert r.execute("SELECT count(*) FROM snapshot").fetchone()[0] == 1
    assert r.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert not list(tmp_path.glob("whodatjet.db.rebuild-*"))
