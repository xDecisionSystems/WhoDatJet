from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from tests.test_api import TODAY, env, world  # noqa: F401  (fixtures)
from whodatjet import db, pipeline
from whodatjet.api import create_app
from whodatjet.faa_client import CHICAGO

NOW = datetime(2026, 11, 10, 7, 0, tzinfo=CHICAGO)


def log_run(cfg, status="stored", finished="2026-11-10T06:16:00-06:00", sha="a" * 64, error=None, key="daily/k.zip"):
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg.log_path.open("a") as fh:
        fh.write(json.dumps({"status": status, "finished_at": finished, "archive_sha256": sha, "error": error,
                             "key": key, "http_last_modified": "Mon, 09 Nov 2026 23:30:00 GMT"}) + "\n")


def report(env, **kw):
    cfg, _ = env
    conn = db.connect_reader(cfg.registry_db_path)
    try:
        return pipeline.pipeline_status(cfg, conn, NOW, TODAY, **kw)
    finally:
        conn.close()


def states(r):
    return r["download"]["status"], r["s3"]["status"], r["processed"]["status"]


def test_all_ok(env):
    log_run(env[0])
    r = report(env)
    assert states(r) == ("ok", "ok", "ok") and r["status"] == "ok"
    assert r["s3"]["key"] == "daily/k.zip" and r["processed"]["coverage_date"] == "2026-11-09"


def test_duplicate_run_counts_as_success(env):
    log_run(env[0], status="duplicate")
    assert report(env)["status"] == "ok"


def test_no_runs_is_unknown(env):
    r = report(env)
    assert states(r)[:2] == ("unknown", "unknown") and r["status"] == "degraded"


def test_download_failure(env):
    log_run(env[0], status="failed", sha=None, error="ConnectError: boom", key=None)
    r = report(env)
    assert states(r)[:2] == ("failed", "failed") and "boom" in r["download"]["error"]


def test_upload_failure_downloaded_but_not_saved(env):
    log_run(env[0], status="failed", error="EndpointConnectionError", key=None)
    r = report(env)
    assert states(r)[:2] == ("ok", "failed")


def test_pending_upload_in_staging_fails_s3(env):
    cfg, _ = env
    log_run(cfg)
    cfg.staging_dir.mkdir(parents=True, exist_ok=True)
    (cfg.staging_dir / "pending-abc.zip").write_bytes(b"x")
    r = report(env)
    assert r["s3"]["status"] == "failed" and r["s3"]["pending_uploads"] == 1


def test_old_success_is_stale(env):
    log_run(env[0], finished="2026-11-08T06:16:00-06:00")
    assert states(report(env))[:2] == ("stale", "stale")


def test_failure_after_earlier_success_reports_both_times(env):
    cfg, _ = env
    log_run(cfg, finished="2026-11-10T00:16:00-06:00")
    log_run(cfg, status="failed", sha=None, error="x", finished="2026-11-10T06:16:00-06:00")
    d = report(env)["download"]
    assert d["status"] == "failed" and d["last_success_at"] == "2026-11-10T00:16:00-06:00"


def test_snapshot_not_imported(env):
    cfg, _ = env
    log_run(cfg)
    conn = db.connect_writer(cfg.registry_db_path)
    conn.execute("UPDATE snapshot SET import_status='failed', error_message='bad schema' WHERE id=3")
    conn.commit()
    conn.close()
    r = report(env)
    assert r["processed"]["status"] == "failed" and r["processed"]["error"] == "bad schema"


def test_stale_processing(env):
    log_run(env[0])
    cfg, _ = env
    conn = db.connect_reader(cfg.registry_db_path)
    r = pipeline.pipeline_status(cfg, conn, NOW, date(2026, 11, 20))
    conn.close()
    assert r["processed"]["status"] == "stale"


def test_torn_log_line_is_ignored(env):
    cfg, _ = env
    log_run(cfg)
    with cfg.log_path.open("a") as fh:
        fh.write('{"status": "sto')
    assert report(env)["status"] == "ok"


@pytest.fixture
def client(env):
    with TestClient(create_app(env[0], today=lambda: TODAY, start_flusher=False)) as tc:
        yield tc


def test_endpoint_open_and_strict(client, env):
    r = client.get("/healthz/pipeline")  # no API key needed
    assert r.status_code == 200 and r.json()["download"]["status"] == "unknown"
    assert client.get("/healthz/pipeline", params={"strict": "true"}).status_code == 503
