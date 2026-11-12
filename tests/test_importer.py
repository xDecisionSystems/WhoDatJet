from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from tests.fixture_archive import Archive
from whodatjet import db, importer
from whodatjet.archive_store import FilesystemArchiveStore, daily_key
from whodatjet.importer import SnapshotMeta, import_archive, import_pending

MASTER, DEREG = "MASTER.txt", "DEREG.txt"


counter = iter(range(1, 10_000))


def run(conn, store_id, tmp_path, archive: Archive, day: str):
    path, sha = archive.write(tmp_path / f"a{next(counter)}.zip")
    content = hashlib.sha256(archive.zip_bytes()).hexdigest()  # stand-in for the content hash
    meta = SnapshotMeta(day, "http_last_modified", f"{day}T23:59:00-06:00", f"daily/{day}/{content}.zip",
                        path.stat().st_size, sha, content)
    return import_archive(conn, path, meta, store_id)


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_first_import(conn, store_id, tmp_path):
    a = Archive()
    r = run(conn, store_id, tmp_path, a, "2026-11-11")
    assert r.status == "imported", r.error
    assert one(conn, "SELECT count(*) FROM registration_record WHERE source_file='master'") == len(a.rows(MASTER))
    assert one(conn, "SELECT count(*) FROM registration_record WHERE source_file='dereg'") == len(a.rows(DEREG))
    assert one(conn, "SELECT count(*) FROM aircraft_model") == len(a.rows("ACFTREF.txt"))
    assert one(conn, "SELECT count(*) FROM aircraft WHERE identity_basis='unique_id'") == len(a.rows(MASTER))
    assert one(conn, "SELECT import_status FROM snapshot") == "imported"
    assert one(conn, "SELECT status FROM ingest_run") == "imported"
    # every MASTER N-number is valid and stored with its N prefix
    assert one(conn, "SELECT count(*) FROM tail_number WHERE n_number NOT LIKE 'N%'") == 0


def test_mfr3_serial_collision_stays_distinct(conn, store_id, tmp_path):
    run(conn, store_id, tmp_path, Archive(), "2026-11-11")
    # Phase 0: several amateur-built aircraft share prefix 056 and serial "1"
    n = one(conn, "SELECT count(DISTINCT aircraft_id) FROM registration_record "
                  "WHERE source_file='master' AND mfr_mdl_code LIKE '056%' AND trim(serial_number_raw)='1'")
    assert n >= 4


def test_reimport_is_duplicate(conn, store_id, tmp_path):
    a = Archive()
    run(conn, store_id, tmp_path, a, "2026-11-11")
    before = one(conn, "SELECT count(*) FROM registration_record")
    r = run(conn, store_id, tmp_path, a, "2026-11-12")
    assert r.status == "duplicate"
    assert one(conn, "SELECT count(*) FROM registration_record") == before


def test_unchanged_rows_create_no_versions(conn, store_id, tmp_path):
    a = Archive()
    run(conn, store_id, tmp_path, a, "2026-11-11")
    b = a.copy()
    row = b.rows(MASTER)[0]
    b.set(MASTER, row, "STATUS CODE", "M")  # one change
    r = run(conn, store_id, tmp_path, b, "2026-11-12")
    assert r.status == "imported"
    assert r.stats["master_inserted"] == 1 and r.stats["master_closed"] == 1
    assert r.stats.get("dereg_inserted", 0) == 0
    assert r.stats.get("aircraft_new_unique_id", 0) == 0
    s1, s2 = (x[0] for x in conn.execute("SELECT id FROM snapshot ORDER BY id"))
    uid = b.get(MASTER, row, "UNIQUE ID")
    versions = conn.execute("SELECT status_code, first_seen_snapshot_id, last_seen_snapshot_id FROM "
                            "registration_record WHERE faa_unique_id=? ORDER BY id", (uid,)).fetchall()
    assert [tuple(v) for v in versions] == [(a.get(MASTER, a.rows(MASTER)[0], "STATUS CODE"), s1, s1), ("M", s2, None)]


def test_disappeared_row_is_closed(conn, store_id, tmp_path):
    a = Archive()
    run(conn, store_id, tmp_path, a, "2026-11-11")
    b = a.copy()
    gone = b.rows(MASTER)[3]
    b.remove(MASTER, gone)
    r = run(conn, store_id, tmp_path, b, "2026-11-12")
    assert r.stats["master_closed"] == 1 and r.stats.get("master_inserted", 0) == 0
    assert one(conn, "SELECT last_seen_snapshot_id FROM registration_record WHERE faa_unique_id=?",
               b.get(MASTER, gone, "UNIQUE ID")) == 1


def test_dereg_links_to_master_aircraft_by_full_code_and_serial(conn, store_id, tmp_path):
    a = Archive()
    m = next(r for r in a.rows(MASTER) if a.get(MASTER, r, "SERIAL NUMBER") not in ("", "0", "00", "1"))
    d = list(a.rows(DEREG)[0])
    a.set(DEREG, d, "MFR-MDL-CODE", a.get(MASTER, m, "MFR MDL CODE"))
    a.set(DEREG, d, "SERIAL-NUMBER", a.get(MASTER, m, "SERIAL NUMBER") + "  ")  # padding ignored
    a.set(DEREG, d, "N-NUMBER", "995ZZ")
    a.add(DEREG, d)
    r = run(conn, store_id, tmp_path, a, "2026-11-11")
    assert r.stats["dereg_linked"] == 1
    assert one(conn, """SELECT ac.faa_unique_id FROM registration_record d JOIN aircraft ac ON ac.id = d.aircraft_id
                        WHERE d.source_file='dereg' AND d.n_number_raw='995ZZ'""") == a.get(MASTER, m, "UNIQUE ID")


def test_dereg_with_changed_series_code_is_not_linked(conn, store_id, tmp_path):
    # Phase 0: model codes get reassigned, but looser keys collide too often to link automatically.
    a = Archive()
    m = next(r for r in a.rows(MASTER) if a.get(MASTER, r, "SERIAL NUMBER") not in ("", "0", "00", "1"))
    d = list(a.rows(DEREG)[0])
    a.set(DEREG, d, "MFR-MDL-CODE", a.get(MASTER, m, "MFR MDL CODE")[:5] + "ZZ")
    a.set(DEREG, d, "SERIAL-NUMBER", a.get(MASTER, m, "SERIAL NUMBER"))
    a.set(DEREG, d, "N-NUMBER", "995ZZ")
    a.add(DEREG, d)
    run(conn, store_id, tmp_path, a, "2026-11-11")
    assert one(conn, """SELECT ac.identity_basis FROM registration_record d JOIN aircraft ac ON ac.id = d.aircraft_id
                        WHERE d.source_file='dereg' AND d.n_number_raw='995ZZ'""") == "model_serial"


def test_reregistered_dereg_only_aircraft_gets_unique_id(conn, store_id, tmp_path):
    a = Archive()
    # a DEREG-only aircraft with a usable serial
    d = next(r for r in a.rows(DEREG) if a.get(DEREG, r, "SERIAL-NUMBER") not in ("", "0")
             and not any(a.get(MASTER, m, "SERIAL NUMBER") == a.get(DEREG, r, "SERIAL-NUMBER") for m in a.rows(MASTER)))
    run(conn, store_id, tmp_path, a, "2026-11-11")
    code, serial = a.get(DEREG, d, "MFR-MDL-CODE"), a.get(DEREG, d, "SERIAL-NUMBER")
    aid = one(conn, "SELECT aircraft_id FROM registration_record WHERE source_file='dereg' AND mfr_mdl_code=? "
                    "AND serial_number_raw=?", code, serial)
    b = a.copy()
    new = list(b.rows(MASTER)[0])
    b.set(MASTER, new, "N-NUMBER", "998ZZ")
    b.set(MASTER, new, "UNIQUE ID", "99999999")
    b.set(MASTER, new, "MFR MDL CODE", code)
    b.set(MASTER, new, "SERIAL NUMBER", serial)
    b.add(MASTER, new)
    r = run(conn, store_id, tmp_path, b, "2026-11-12")
    assert r.stats["aircraft_unique_id_attached"] == 1
    assert one(conn, "SELECT faa_unique_id FROM aircraft WHERE id=?", aid) == "99999999"


def test_unique_id_attribute_change_records_event(conn, store_id, tmp_path):
    a = Archive()
    run(conn, store_id, tmp_path, a, "2026-11-11")
    b = a.copy()
    row = b.rows(MASTER)[0]
    b.set(MASTER, row, "SERIAL NUMBER", "CHANGED-1")
    r = run(conn, store_id, tmp_path, b, "2026-11-12")
    assert r.stats["event_unique_id_attributes_changed"] == 1
    assert r.stats.get("aircraft_new_unique_id", 0) == 0  # same UNIQUE ID -> same aircraft


def test_new_unique_id_for_known_airframe_records_event(conn, store_id, tmp_path):
    a = Archive()
    run(conn, store_id, tmp_path, a, "2026-11-11")
    b = a.copy()
    row = b.rows(MASTER)[0]
    b.set(MASTER, row, "UNIQUE ID", "88888888")  # old UNIQUE ID disappears, same code + serial
    r = run(conn, store_id, tmp_path, b, "2026-11-12")
    assert r.stats["event_model_serial_new_unique_id"] == 1
    ev = json.loads(one(conn, "SELECT detail_json FROM identity_event"))
    assert ev["previous_unique_ids"] == [a.get(MASTER, a.rows(MASTER)[0], "UNIQUE ID")]


def test_ambiguous_dereg_match_records_event(conn, store_id, tmp_path):
    a = Archive()
    m = a.rows(MASTER)[0]
    twin = list(m)
    a.set(MASTER, twin, "N-NUMBER", "997ZZ")
    a.set(MASTER, twin, "UNIQUE ID", "77777777")
    a.add(MASTER, twin)
    d = list(a.rows(DEREG)[0])
    a.set(DEREG, d, "MFR-MDL-CODE", a.get(MASTER, m, "MFR MDL CODE"))
    a.set(DEREG, d, "SERIAL-NUMBER", a.get(MASTER, m, "SERIAL NUMBER"))
    a.set(DEREG, d, "N-NUMBER", "996ZZ")
    a.add(DEREG, d)
    r = run(conn, store_id, tmp_path, a, "2026-11-11")
    assert r.stats["event_ambiguous_dereg_match"] >= 1


def test_schema_mismatch_fails_cleanly(conn, store_id, tmp_path):
    a = Archive()
    a.drop_column(MASTER, "UNIQUE ID")
    r = run(conn, store_id, tmp_path, a, "2026-11-11")
    assert r.status == "failed" and "SchemaMismatch" in r.error and "UNIQUE ID" in r.error
    assert one(conn, "SELECT count(*) FROM registration_record") == 0
    assert one(conn, "SELECT import_status FROM snapshot") == "failed"
    assert one(conn, "SELECT status FROM ingest_run") == "failed"


def test_failure_mid_transaction_rolls_back_and_retry_succeeds(conn, store_id, tmp_path, monkeypatch):
    a = Archive()
    path, sha = a.write(tmp_path / "x.zip")
    meta = SnapshotMeta("2026-11-11", "http_last_modified", "2026-11-11T23:59:00-06:00", "daily/x.zip",
                        path.stat().st_size, sha, "c" * 64)
    real = importer._Apply.records

    def boom(self, source, rows, identity):
        real(self, source, rows, identity)
        if source == "dereg":
            raise RuntimeError("disk full")

    monkeypatch.setattr(importer._Apply, "records", boom)
    r = import_archive(conn, path, meta, store_id)
    assert r.status == "failed"
    for t in ("registration_record", "aircraft", "aircraft_model", "tail_number"):
        assert one(conn, f"SELECT count(*) FROM {t}") == 0, t
    monkeypatch.setattr(importer._Apply, "records", real)
    r2 = import_archive(conn, path, meta, store_id)
    assert r2.status == "imported" and r2.snapshot_id == r.snapshot_id
    assert one(conn, "SELECT count(*) FROM snapshot") == 1


def test_out_of_order_archive_refused(conn, store_id, tmp_path):
    a = Archive()
    run(conn, store_id, tmp_path, a, "2026-11-12")
    b = a.copy()
    b.set(MASTER, b.rows(MASTER)[0], "STATUS CODE", "M")
    r = run(conn, store_id, tmp_path, b, "2026-11-11")
    assert r.status == "out_of_order" and "rebuild" in r.error


def test_unmigrated_database_refused(tmp_path):
    c = db.connect_writer(tmp_path / "empty.db")
    with pytest.raises(importer.ImportError_, match="migrate"):
        importer.check_schema(c)


def test_import_pending_from_filesystem_store_in_order(conn, tmp_path):
    store = FilesystemArchiveStore(tmp_path / "archives")
    a = Archive()
    b = a.copy()
    b.set(MASTER, b.rows(MASTER)[0], "STATUS CODE", "M")
    for arc, day in ((b, "2026-11-12"), (a, "2026-11-11")):  # stored newest first
        p, sha = arc.write(tmp_path / f"{day}.zip")
        content = hashlib.sha256(arc.zip_bytes()).hexdigest()
        store.put_verified(daily_key(date.fromisoformat(day), content), p, sha, {
            "source-date": day, "source-date-basis": "http_last_modified", "downloaded-at": f"{day}T23:59:00-06:00"})
    results = import_pending(conn, store, tmp_path / "staging")
    assert [r.status for r in results] == ["imported", "imported"]
    assert [r[0] for r in conn.execute("SELECT source_date FROM snapshot ORDER BY id")] == ["2026-11-11", "2026-11-12"]
    assert import_pending(conn, store, tmp_path / "staging") == []
    assert not list((tmp_path / "staging").iterdir())
