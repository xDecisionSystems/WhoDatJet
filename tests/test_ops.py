from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from datetime import date, datetime

import httpx
import pytest

from tests.fixture_archive import Archive
from whodatjet import backup, db, ingest, logs, notify, status
from whodatjet import collect as collect_mod
from whodatjet.archive_store import ArchiveStoreError, FilesystemArchiveStore
from whodatjet.faa_client import CHICAGO, Download

MASTER = "MASTER.txt"


@pytest.fixture
def fcfg(cfg, tmp_path):
    c = replace(cfg, s3=None, data_dir=tmp_path / "data")
    conn = db.connect_writer(c.registry_db_path)
    db.migrate(conn, db.load_migrations("registry"))
    conn.close()
    adm = db.connect_writer(c.admin_db_path)
    db.migrate(adm, db.load_migrations("admin"))
    adm.close()
    return c


@pytest.fixture
def fstore(fcfg):
    return FilesystemArchiveStore(fcfg.archive_dir)


def serve(monkeypatch, arc: Archive | bytes, last_modified: str):
    body = arc if isinstance(arc, bytes) else arc.zip_bytes()

    def fake(url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return Download(dest, 200, last_modified, '"e"', datetime(2026, 11, 12, 0, 20, tzinfo=CHICAGO))
    monkeypatch.setattr(collect_mod, "download", fake)


def runs(c):
    conn = db.connect_reader(c.registry_db_path)
    return [tuple(r) for r in conn.execute("SELECT status, snapshot_id, error_message IS NOT NULL FROM ingest_run "
                                           "ORDER BY id")]


LM1, LM2 = "Thu, 12 Nov 2026 05:58:13 GMT", "Fri, 13 Nov 2026 05:40:00 GMT"


def test_ingest_new_then_duplicate_then_new(fcfg, fstore, monkeypatch):
    a = Archive()
    serve(monkeypatch, a, LM1)
    r1 = ingest.ingest(fcfg, fstore)
    assert r1.ok and r1.collect.status == "stored" and [i.status for i in r1.imports] == ["imported"]
    r2 = ingest.ingest(fcfg, fstore)
    assert r2.ok and r2.collect.status == "duplicate" and r2.imports == []
    b = a.copy()
    b.set(MASTER, b.rows(MASTER)[0], "STATUS CODE", "M")
    serve(monkeypatch, b, LM2)
    r3 = ingest.ingest(fcfg, fstore)
    assert r3.ok and r3.imports[0].stats["master_inserted"] == 1
    assert runs(fcfg) == [("imported", 1, 0), ("duplicate", 1, 0), ("imported", 2, 0)]


def test_failed_download_recorded_and_not_ok(fcfg, fstore, monkeypatch):
    serve(monkeypatch, b"<html>down</html>", LM1)
    r = ingest.ingest(fcfg, fstore)
    assert not r.ok and r.collect.status == "failed"
    assert runs(fcfg) == [("failed", None, 1)]


def test_upload_failure_recovered_next_run(fcfg, fstore, monkeypatch):
    a = Archive()
    serve(monkeypatch, a, LM1)
    real = fstore.put_verified
    monkeypatch.setattr(fstore, "put_verified", lambda *x, **k: (_ for _ in ()).throw(ArchiveStoreError("nas down")))
    assert not ingest.ingest(fcfg, fstore).ok
    monkeypatch.setattr(fstore, "put_verified", real)
    r = ingest.ingest(fcfg, fstore)  # retries the pending upload; today's download is then a duplicate
    assert r.ok and [i.status for i in r.imports] == ["imported"]
    assert r.collect.retried_pending[0]["status"] == "stored"


def test_ingest_skips_when_locked(fcfg, fstore):
    with collect_mod.ingest_lock(fcfg.lock_path), pytest.raises(collect_mod.LockBusy):
        ingest.ingest(fcfg, fstore)


def test_ingest_requires_migrated_registry(cfg, tmp_path, monkeypatch):
    c = replace(cfg, s3=None, data_dir=tmp_path / "fresh")
    serve(monkeypatch, Archive(), LM1)
    r = ingest.ingest(c, FilesystemArchiveStore(c.archive_dir))
    assert not r.ok and "migrate" in r.error
    assert r.collect.status == "stored"  # the archive is safe even though import could not run


def test_backup_consistent_and_pruned(fcfg, tmp_path):
    dest = tmp_path / "bk"
    for i in range(3):
        files = backup.backup({"whodatjet": fcfg.registry_db_path, "admin": fcfg.admin_db_path}, dest, keep=2,
                              label=f"2026111{i}T000000")
        assert len(files) == 2
    names = sorted(p.name for p in dest.iterdir())
    assert names == ["admin-20261111T000000.db", "admin-20261112T000000.db",
                     "whodatjet-20261111T000000.db", "whodatjet-20261112T000000.db"]
    conn = sqlite3.connect(dest / "whodatjet-20261112T000000.db")
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] >= 2
    assert oct((dest / names[0]).stat().st_mode & 0o777) == "0o600"


HOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


def test_notify_posts_without_secrets(monkeypatch, caplog):
    sent = []
    monkeypatch.setenv("WHODATJET_SLACK_WEBHOOK_URL", HOOK)
    transport = httpx.MockTransport(lambda req: sent.append(json.loads(req.content)) or httpx.Response(200))
    assert notify.send("ingest_failed", "HTTP 503 from FAA", transport=transport, backoff_s=0)
    assert len(sent) == 1 and "Ingest failed" in sent[0]["text"] and "HTTP 503" in sent[0]["text"]
    assert HOOK not in sent[0]["text"] and HOOK not in caplog.text


def test_notify_retries_then_gives_up(monkeypatch):
    calls = []
    monkeypatch.setenv("WHODATJET_SLACK_WEBHOOK_URL", HOOK)
    transport = httpx.MockTransport(lambda req: calls.append(1) or httpx.Response(500))
    assert not notify.send("ingest_failed", "x", transport=transport, backoff_s=0)
    assert len(calls) == 3


def test_notify_rejects_non_slack_webhook(monkeypatch):
    monkeypatch.setenv("WHODATJET_SLACK_WEBHOOK_URL", "https://evil.example/hook")
    transport = httpx.MockTransport(lambda req: pytest.fail("must not send"))
    assert not notify.send("ingest_failed", "x", transport=transport)


def test_notify_without_webhook_only_logs(monkeypatch, caplog):
    monkeypatch.delenv("WHODATJET_SLACK_WEBHOOK_URL", raising=False)
    with caplog.at_level(logging.WARNING):
        assert notify.send("unit_failed", "whodatjet-api.service failed")
    assert "whodatjet-api.service failed" in caplog.text


def test_json_log_format(capsys):
    logs.configure(fmt="json")
    logging.getLogger("whodatjet.test").info("hello", extra={"event": "ingest", "summary": {"ok": True}})
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["msg"] == "hello" and line["event"] == "ingest" and line["summary"] == {"ok": True}
    logs.configure(fmt="text")


def test_status_summary(fcfg, fstore, monkeypatch):
    serve(monkeypatch, Archive(), LM1)
    ingest.ingest(fcfg, fstore)
    s = status.gather(fcfg, date(2026, 11, 12))
    assert s["coverage_end"] == "2026-11-11" and s["stale"] is False and s["snapshots"] == 1
    assert s["last_run"]["status"] == "imported" and s["active_api_keys"] == 0
    text = status.render(s)
    assert "coverage: 2026-11-11 .. 2026-11-11" in text and "fresh" in text


def test_backup_private_during_copy_and_preserves_previous_on_failure(fcfg, tmp_path, monkeypatch):
    dest = tmp_path / "backup.db"
    dest.write_bytes(b"previous backup")
    real_connect = sqlite3.connect

    def connect(path, *args, **kwargs):
        if not kwargs.get("uri"):
            assert path.stat().st_mode & 0o777 == 0o600
            raise sqlite3.OperationalError("simulated destination failure")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(backup.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        backup._backup_one(fcfg.admin_db_path, dest)
    assert dest.read_bytes() == b"previous backup"
    assert list(tmp_path.glob(".backup.db.*.tmp")) == []


def test_backup_source_path_with_uri_characters(tmp_path):
    src = tmp_path / "registry?#.db"
    with sqlite3.connect(src) as conn:
        conn.execute("CREATE TABLE example (value TEXT)")
        conn.execute("INSERT INTO example VALUES ('retained')")
    dest = tmp_path / "backup.db"
    backup._backup_one(src, dest)
    with sqlite3.connect(dest) as conn:
        assert conn.execute("SELECT value FROM example").fetchone()[0] == "retained"
