from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import FAA_FILES, make_zip
from whodatjet import collect as collect_mod
from whodatjet.archive_store import ArchiveStoreError
from whodatjet.faa_client import CHICAGO, Download

LM = "Thu, 12 Nov 2026 05:58:13 GMT"


def fake_download(body: bytes, last_modified: str = LM):
    def _dl(url, dest: Path, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return Download(dest, 200, last_modified, '"etag1"', datetime(2026, 11, 12, 0, 20, tzinfo=CHICAGO))
    return _dl


def runs(cfg):
    return [json.loads(line) for line in cfg.log_path.read_text().splitlines()]


def test_first_run_stores_archive(cfg, store, monkeypatch):
    monkeypatch.setattr(collect_mod, "download", fake_download(make_zip()))
    r = collect_mod.collect(cfg, store)
    assert r.status == "stored"
    assert r.key.startswith("2026/2026-11-11_") and r.key.endswith(".zip")
    head = store.s3.head_object(Bucket="archives", Key="faa-aircraft-database/" + r.key)
    assert head["Metadata"]["source-date-basis"] == "http_last_modified"
    assert head["Metadata"]["archive-sha256"] == r.archive_sha256
    assert not list(cfg.staging_dir.iterdir())
    assert runs(cfg)[0]["status"] == "stored"


def test_rerun_and_rezip_are_duplicates(cfg, store, monkeypatch):
    monkeypatch.setattr(collect_mod, "download", fake_download(make_zip()))
    first = collect_mod.collect(cfg, store)
    monkeypatch.setattr(collect_mod, "download", fake_download(make_zip(mtime=(2026, 11, 12, 1, 0, 0))))
    second = collect_mod.collect(cfg, store)
    assert second.status == "duplicate" and second.key == first.key
    assert len(store.list_daily()) == 1


def test_new_content_stored_separately(cfg, store, monkeypatch):
    monkeypatch.setattr(collect_mod, "download", fake_download(make_zip()))
    collect_mod.collect(cfg, store)
    changed = make_zip({**FAA_FILES, "MASTER.txt": FAA_FILES["MASTER.txt"] + "54321,9,2072738,1,\r\n"})
    monkeypatch.setattr(collect_mod, "download", fake_download(changed, "Fri, 13 Nov 2026 05:40:00 GMT"))
    r = collect_mod.collect(cfg, store)
    assert r.status == "stored" and r.key.startswith("2026/2026-11-12_")
    assert [a.source_date.isoformat() for a in store.list_daily()] == ["2026-11-11", "2026-11-12"]


def test_invalid_archive_fails_and_is_discarded(cfg, store, monkeypatch):
    monkeypatch.setattr(collect_mod, "download", fake_download(b"<html>maintenance</html>"))
    r = collect_mod.collect(cfg, store)
    assert r.status == "failed" and "ArchiveError" in r.error
    assert store.list_daily() == []
    assert not list(cfg.staging_dir.iterdir())


def test_upload_failure_keeps_archive_and_next_run_retries(cfg, store, monkeypatch):
    monkeypatch.setattr(collect_mod, "download", fake_download(make_zip()))
    real_put = store.put_verified

    def broken(*a, **kw):
        raise ArchiveStoreError("store unreachable")

    monkeypatch.setattr(store, "put_verified", broken)
    r1 = collect_mod.collect(cfg, store)
    assert r1.status == "failed"
    assert len(list(cfg.staging_dir.glob("pending-*.zip"))) == 1

    # Next run: store is back; FAA now serves new content. Both must end up stored.
    monkeypatch.setattr(store, "put_verified", real_put)
    changed = make_zip({**FAA_FILES, "MASTER.txt": FAA_FILES["MASTER.txt"] + "54321,9,2072738,1,\r\n"})
    monkeypatch.setattr(collect_mod, "download", fake_download(changed, "Fri, 13 Nov 2026 05:40:00 GMT"))
    r2 = collect_mod.collect(cfg, store)
    assert r2.status == "stored"
    assert r2.retried_pending[0]["status"] == "stored"
    assert len(store.list_daily()) == 2
    assert not list(cfg.staging_dir.iterdir())


def test_never_overwrites(cfg, store, tmp_path):
    p = tmp_path / "a.zip"
    p.write_bytes(b"x")
    import hashlib
    sha = hashlib.sha256(b"x").hexdigest()
    store.put_verified("2026/2026-11-11_" + "0" * 64 + ".zip", p, sha, {})
    with pytest.raises(ArchiveStoreError, match="overwrite"):
        store.put_verified("2026/2026-11-11_" + "0" * 64 + ".zip", p, sha, {})


def test_concurrent_run_is_skipped(cfg, store):
    with collect_mod.ingest_lock(cfg.lock_path), pytest.raises(collect_mod.LockBusy):
        collect_mod.collect(cfg, store)
