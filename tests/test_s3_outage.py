"""What happens when S3 is unreachable during collection/ingest (nothing lost; upload retried next run)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from botocore.exceptions import EndpointConnectionError

from tests.conftest import FAA_FILES, make_zip
from tests.fixture_archive import Archive
from whodatjet import collect as collect_mod
from whodatjet import db, ingest
from whodatjet.faa_client import CHICAGO, Download

LM1, LM2 = "Thu, 12 Nov 2026 05:58:13 GMT", "Fri, 13 Nov 2026 05:40:00 GMT"
DAY2 = make_zip({**FAA_FILES, "MASTER.txt": FAA_FILES["MASTER.txt"] + "54321,9,2072738,1,\r\n"})


class FlakyS3:
    """Wraps a boto3 client; while `down` every call fails like an unreachable endpoint."""

    def __init__(self, client):
        self._client = client
        self.down = False
        self.calls_while_down = 0

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*a, **kw):
            if self.down:
                self.calls_while_down += 1
                raise EndpointConnectionError(endpoint_url="https://s3.test")
            return attr(*a, **kw)
        return call


@pytest.fixture
def flaky(store):
    store.s3 = FlakyS3(store.s3)
    return store.s3


@pytest.fixture
def migrated(cfg):
    conn = db.connect_writer(cfg.registry_db_path)
    db.migrate(conn, db.load_migrations("registry"))
    conn.close()
    return cfg


def serve(monkeypatch, body: bytes, last_modified: str):
    def fake(url, dest: Path, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return Download(dest, 200, last_modified, '"e"', datetime(2026, 11, 12, 0, 20, tzinfo=CHICAGO))
    monkeypatch.setattr(collect_mod, "download", fake)


def pending(cfg):
    return sorted(cfg.staging_dir.glob("pending-*.zip"))


def test_outage_keeps_download_and_records_failure(cfg, store, flaky, monkeypatch):
    serve(monkeypatch, make_zip(), LM1)
    flaky.down = True
    r = collect_mod.collect(cfg, store)
    assert r.status == "failed" and "EndpointConnectionError" in r.error
    assert flaky.calls_while_down >= 1
    assert len(pending(cfg)) == 1                                     # the only copy is kept
    sidecar = json.loads(pending(cfg)[0].with_suffix(".json").read_text())
    assert sidecar["last_modified"] == LM1                             # source-date evidence preserved
    assert json.loads(cfg.log_path.read_text().splitlines()[-1])["status"] == "failed"


def test_recovery_uploads_pending_with_original_source_date(cfg, store, flaky, monkeypatch):
    serve(monkeypatch, make_zip(), LM1)
    flaky.down = True
    collect_mod.collect(cfg, store)
    flaky.down = False
    serve(monkeypatch, make_zip(), LM1)                               # FAA still serving the same file
    r = collect_mod.collect(cfg, store)
    assert r.status == "duplicate"                                    # pending was uploaded first; today's is a dup
    assert r.retried_pending[0]["status"] == "stored"
    assert [a.source_date.isoformat() for a in store.list_daily()] == ["2026-11-11"]
    assert not list(cfg.staging_dir.iterdir())


def test_multi_day_outage_stores_every_day_in_order(cfg, store, flaky, monkeypatch):
    flaky.down = True
    serve(monkeypatch, make_zip(), LM1)
    assert collect_mod.collect(cfg, store).status == "failed"
    serve(monkeypatch, DAY2, LM2)
    assert collect_mod.collect(cfg, store).status == "failed"
    assert len(pending(cfg)) == 2                                     # both days waiting, nothing overwritten
    flaky.down = False
    serve(monkeypatch, DAY2, LM2)
    r = collect_mod.collect(cfg, store)
    assert [p["status"] for p in r.retried_pending] == ["stored", "stored"]
    assert [a.source_date.isoformat() for a in store.list_daily()] == ["2026-11-11", "2026-11-12"]
    assert not list(cfg.staging_dir.iterdir())


def test_outage_during_upload_leaves_no_partial_object(cfg, store, flaky, monkeypatch):
    """Listing works but the PUT fails: the archive stays staged and nothing half-written is in the bucket."""
    serve(monkeypatch, make_zip(), LM1)
    real_put = flaky._client.put_object

    def failing_put(**kw):
        raise EndpointConnectionError(endpoint_url="https://s3.test")
    flaky._client.put_object = failing_put
    r = collect_mod.collect(cfg, store)
    assert r.status == "failed" and len(pending(cfg)) == 1
    assert store.list_daily() == []
    flaky._client.put_object = real_put
    assert collect_mod.collect(cfg, store).retried_pending[0]["status"] == "stored"
    assert len(store.list_daily()) == 1


def test_ingest_during_outage_fails_cleanly_and_recovers(migrated, store, flaky, monkeypatch):
    cfg = migrated
    serve(monkeypatch, Archive().zip_bytes(), LM1)
    flaky.down = True
    r1 = ingest.ingest(cfg, store, backoff_s=0)
    assert not r1.ok and r1.collect.status == "failed"
    conn = db.connect_reader(cfg.registry_db_path)
    assert conn.execute("SELECT count(*) FROM snapshot").fetchone()[0] == 0          # nothing imported
    assert [tuple(r) for r in conn.execute("SELECT status, snapshot_id FROM ingest_run")] == [("failed", None)]
    conn.close()
    assert len(pending(cfg)) == 1

    flaky.down = False
    serve(monkeypatch, Archive().zip_bytes(), LM1)
    r2 = ingest.ingest(cfg, store, backoff_s=0)
    assert r2.ok and [i.status for i in r2.imports] == ["imported"]
    conn = db.connect_reader(cfg.registry_db_path)
    assert conn.execute("SELECT count(*) FROM snapshot").fetchone()[0] == 1
    assert conn.execute("SELECT source_date FROM snapshot").fetchone()[0] == "2026-11-11"
    conn.close()
    assert not list(cfg.staging_dir.iterdir())


def test_api_keeps_serving_during_outage(migrated, store, flaky):
    from fastapi.testclient import TestClient

    from whodatjet import auth
    from whodatjet.api import create_app

    adm = db.connect_writer(migrated.admin_db_path)
    db.migrate(adm, db.load_migrations("admin"))
    key = auth.create_key(adm, name="k", created_by="t").key
    adm.close()
    flaky.down = True
    with TestClient(create_app(migrated, start_flusher=False)) as api:                # the API never touches S3
        assert api.get("/v1/snapshots", headers={"X-API-Key": key}).status_code == 200
        assert api.get("/healthz").status_code == 200
    assert flaky.calls_while_down == 0
