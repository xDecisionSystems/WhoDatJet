from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixture_archive import Archive
from whodatjet import auth, db
from whodatjet.api import create_app
from whodatjet.auth import KeyCache
from whodatjet.importer import SnapshotMeta, import_archive

MASTER, DEREG = "MASTER.txt", "DEREG.txt"
D1, D2, D3 = "2026-11-02", "2026-11-03", "2026-11-09"  # Wed-Fri Nov 4-6 missing: a gap
TODAY = date(2026, 11, 10)


def build_registry(path: Path, archives: list[tuple[Archive, str]]) -> None:
    conn = db.connect_writer(path)
    db.migrate(conn, db.load_migrations("registry"))
    sid = int(conn.execute("INSERT INTO archive_store (role, kind, location, created_at) "
                           "VALUES ('primary','filesystem','/t','now')").lastrowid)
    for i, (arc, day) in enumerate(archives):
        p, sha = arc.write(path.parent / f"src-{i}.zip")
        content = hashlib.sha256(arc.zip_bytes()).hexdigest()
        r = import_archive(conn, p, SnapshotMeta(day, "http_last_modified", f"{day}T23:59:00-06:00",
                                                 f"daily/{day}/{content}.zip", p.stat().st_size, sha, content), sid)
        assert r.status == "imported", r.error
    conn.close()


class World:
    """Three snapshots exercising every lookup path."""

    def __init__(self) -> None:
        a = Archive()
        self.row0, self.row1, self.row2 = a.rows(MASTER)[0], a.rows(MASTER)[1], a.rows(MASTER)[2]
        self.n0 = "N" + a.get(MASTER, self.row0, "N-NUMBER")
        self.n1 = "N" + a.get(MASTER, self.row1, "N-NUMBER")
        self.n2 = "N" + a.get(MASTER, self.row2, "N-NUMBER")
        self.cert2 = a.get(MASTER, self.row2, "CERT ISSUE DATE")
        # low-identity DEREG record
        d = list(a.rows(DEREG)[0])
        for col, val in (("N-NUMBER", "993ZZ"), ("SERIAL-NUMBER", "0"), ("CERT-ISSUE-DATE", "19900101"),
                         ("CANCEL-DATE", "19950101")):
            a.set(DEREG, d, col, val)
        a.add(DEREG, d)
        # normal DEREG record
        e = list(a.rows(DEREG)[1])
        for col, val in (("N-NUMBER", "992ZZ"), ("SERIAL-NUMBER", "XYZ-42"), ("CERT-ISSUE-DATE", "20000101"),
                         ("CANCEL-DATE", "20100101")):
            a.set(DEREG, e, col, val)
        a.add(DEREG, e)
        b = a.copy()
        b.set(MASTER, b.rows(MASTER)[0], "N-NUMBER", "994ZZ")   # N-number change at D2
        c = b.copy()
        c.remove(MASTER, c.rows(MASTER)[1])                      # row1 gone at D3 (across the gap)
        self.archives = [(a, D1), (b, D2), (c, D3)]


@pytest.fixture
def world():
    return World()


@pytest.fixture
def env(tmp_path, cfg, world):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    c = replace(cfg, data_dir=data, s3=None)
    build_registry(c.registry_db_path, world.archives)
    adm = db.connect_writer(c.admin_db_path)
    db.migrate(adm, db.load_migrations("admin"))
    keys = {
        "query": auth.create_key(adm, name="q", created_by="test").key,
        "monitor": auth.create_key(adm, name="m", scope="monitor", created_by="test").key,
        "limited": auth.create_key(adm, name="l", created_by="test", rate_limit_per_min=2).key,
        "expired": auth.create_key(adm, name="e", created_by="test", expires_at=date(2026, 11, 9)).key,
    }
    adm.close()
    return c, keys


@pytest.fixture
def client(env):
    c, _ = env
    with TestClient(create_app(c, today=lambda: TODAY, start_flusher=False)) as tc:
        yield tc


def get(client, env, path, key="query", **params):
    return client.get(path, params=params, headers={"Authorization": f"Bearer {env[1][key]}"})


# ---------------------------------------------------------------- lookups

def test_observed_lookup_high_confidence(client, env, world):
    r = get(client, env, f"/v1/tail-numbers/{world.n2}", date=D2)
    assert r.status_code == 200
    j = r.json()
    assert j["basis"] == "observed" and j["data_confidence"] == "high" and j["data_quality_flags"] == []
    assert j["observation_date"] == D2 and j["snapshot_id"] == 2 and len(j["archive_sha256"]) == 64
    assert j["n_number"] == world.n2 and j["serial_number"] and j["manufacturer_name"]


def test_n_number_formats_normalized(client, env, world):
    raw = world.n2[1:].lower()
    assert get(client, env, f"/v1/tail-numbers/{raw}", date=D2).json()["n_number"] == world.n2


def test_changed_n_number(client, env, world):
    assert get(client, env, f"/v1/tail-numbers/{world.n0}", date=D1).json()["valid_to"] == D1
    j = get(client, env, "/v1/tail-numbers/N994ZZ", date=D2).json()
    assert j["valid_from"] == D2 and j["aircraft_id"] == get(client, env, f"/v1/tail-numbers/{world.n0}",
                                                             date=D1).json()["aircraft_id"]
    r = get(client, env, f"/v1/tail-numbers/{world.n0}", date=D2)
    assert r.status_code == 404 and r.json()["reason"] == "unassigned"


def test_bracketed_change_across_gap(client, env, world):
    j = get(client, env, f"/v1/tail-numbers/{world.n1}", date="2026-11-05").json()
    assert j["data_quality_flags"] == ["bracketed_change", "gap"]
    assert j["data_confidence"] == "low"
    assert (j["last_seen_date"], j["first_absent_date"]) == (D2, D3)
    assert j["observation_date"] == D2


def test_open_interval_inside_gap_is_flagged(client, env, world):
    j = get(client, env, f"/v1/tail-numbers/{world.n2}", date="2026-11-05").json()
    assert j["basis"] == "observed" and j["data_quality_flags"] == ["gap"]


def test_one_day_past_coverage_is_not_stale(client, env, world):
    j = get(client, env, f"/v1/tail-numbers/{world.n2}").json()  # defaults to today (Nov 10)
    assert j["date"] == TODAY.isoformat() and j["data_quality_flags"] == []
    assert j["data_confidence"] == "high"


def test_stale_beyond_threshold(env, world):
    c, keys = env
    with TestClient(create_app(c, today=lambda: date(2026, 11, 20), start_flusher=False)) as tc:
        j = tc.get(f"/v1/tail-numbers/{world.n2}", headers={"X-API-Key": keys["query"]}).json()
        assert j["data_quality_flags"] == ["stale"] and j["data_confidence"] == "medium"


def test_certificate_basis_before_coverage(client, env, world):
    cert = f"{world.cert2[:4]}-{world.cert2[4:6]}-{world.cert2[6:]}"
    j = get(client, env, f"/v1/tail-numbers/{world.n2}", date=cert).json()
    assert j["basis"] == "certificate" and j["data_quality_flags"] == ["certificate_basis"]
    assert j["data_confidence"] == "medium" and j["observation_date"] == D1


def test_dereg_fallback(client, env):
    j = get(client, env, "/v1/tail-numbers/N992ZZ", date="2005-06-01").json()
    assert j["basis"] == "dereg_record" and j["data_quality_flags"] == ["dereg_record"]
    assert (j["valid_from"], j["valid_to"]) == ("2000-01-01", "2010-01-01")


def test_low_identity(client, env):
    j = get(client, env, "/v1/tail-numbers/N993ZZ", date="1992-01-01").json()
    assert j["data_quality_flags"] == ["low_identity", "dereg_record"] and j["data_confidence"] == "low"


def test_not_found_reasons(client, env, world):
    r = get(client, env, "/v1/tail-numbers/N992ZZ", date="2015-01-01")
    assert r.status_code == 404 and r.json()["reason"] == "before_observed_coverage"
    assert r.json()["coverage_start"] == D1 and r.json()["coverage_end"] == D3
    assert get(client, env, "/v1/tail-numbers/N99999", date=D1).json()["reason"] == "no_records"


def test_field_selection(client, env, world):
    j = get(client, env, f"/v1/tail-numbers/{world.n2}", date=D2, fields="manufacturer_name,model_name").json()
    assert set(j) == {"n_number", "date", "observation_date", "snapshot_id", "archive_sha256", "basis",
                      "data_confidence", "data_quality_flags", "manufacturer_name", "model_name"}
    assert "registrant_name" in get(client, env, f"/v1/tail-numbers/{world.n2}", date=D2, fields="*").json()
    r = get(client, env, f"/v1/tail-numbers/{world.n2}", fields="model_name,secret_column")
    assert r.status_code == 400 and "secret_column" in r.json()["detail"] and "accepted" in r.json()["detail"]


@pytest.mark.parametrize("path,params", [
    ("/v1/tail-numbers/0ABC", {}),
    ("/v1/tail-numbers/N12I", {}),
    ("/v1/tail-numbers/N100", {"date": "2026-13-01"}),
    ("/v1/tail-numbers/N100", {"date": "2026-11-11"}),  # future relative to TODAY
])
def test_validation(client, env, path, params):
    assert get(client, env, path, **params).status_code == 400


def test_history_paginates(client, env, world):
    j = get(client, env, f"/v1/tail-numbers/{world.n0}/history").json()
    assert [i["basis"] for i in j["items"]] and j["next_offset"] is None
    j1 = get(client, env, f"/v1/tail-numbers/{world.n1}/history", limit=1).json()
    assert len(j1["items"]) == 1
    gap_item = next(i for i in j1["items"] if i["basis"] == "observed")
    assert "gap" in gap_item["data_quality_flags"]
    assert get(client, env, "/v1/tail-numbers/N99999/history").status_code == 404


def test_aircraft_endpoint(client, env, world):
    aid = get(client, env, "/v1/tail-numbers/N994ZZ", date=D2).json()["aircraft_id"]
    j = get(client, env, f"/v1/aircraft/{aid}").json()
    assert [h["n_number"] for h in j["n_number_history"]] == [world.n0, "N994ZZ"]
    assert j["identity_basis"] == "unique_id" and j["model_history"]
    assert get(client, env, "/v1/aircraft/999999999").status_code == 404


def test_snapshots_endpoint_reports_gap(client, env):
    j = get(client, env, "/v1/snapshots").json()
    assert [s["source_date"] for s in j["snapshots"]] == [D3, D2, D1]
    assert j["gaps"] == [{"after": D2, "before": D3, "missed_publication_days": 3}]


# ---------------------------------------------------------------- health and status

def test_healthz_open_and_strict(client, env):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["last_coverage_date"] == D3
    assert r.json()["problems"] == []
    assert client.get("/docs").status_code == 200 and client.get("/openapi.json").status_code == 200


def test_healthz_strict_503_when_stale(env):
    c, _ = env
    with TestClient(create_app(c, today=lambda: date(2026, 11, 20), start_flusher=False)) as tc:
        assert tc.get("/healthz").status_code == 200
        r = tc.get("/healthz", params={"strict": "true"})
        assert r.status_code == 503 and "stale" in r.json()["problems"]


STATUS_KEYS = {"status", "version", "profile", "last_import_at", "last_import_result", "last_coverage_date",
               "data_age_days", "snapshots", "open_gaps", "active_alerts", "requests_24h", "p95_ms_24h",
               "errors_503_24h", "disk_free_pct", "archive_store", "archive_store_ok", "archive_replica_lag",
               "disk_days_remaining"}


def test_status_contract(client, env):
    j = get(client, env, "/v1/status", key="monitor").json()
    assert set(j) == STATUS_KEYS  # Homepage mappings depend on these names
    assert all(not isinstance(v, (dict, list)) for v in j.values())  # flat
    assert j["snapshots"] == 3 and j["open_gaps"] == 1 and j["last_coverage_date"] == D3
    assert j["data_age_days"] == 1 and isinstance(j["disk_free_pct"], float)


# ---------------------------------------------------------------- authentication

def test_missing_and_invalid_keys(client, world):
    for headers in ({}, {"Authorization": "Bearer wdj_nope"}, {"X-API-Key": "garbage"},
                    {"Authorization": "Basic abc"}):
        r = client.get(f"/v1/tail-numbers/{world.n2}", headers=headers)
        assert r.status_code == 401 and r.json()["detail"] == "missing, invalid, expired, or revoked API key"


def test_x_api_key_header_accepted(client, env, world):
    assert client.get("/v1/snapshots", headers={"X-API-Key": env[1]["query"]}).status_code == 200


def test_expired_key(client, env):
    assert get(client, env, "/v1/snapshots", key="expired").status_code == 401


def test_monitor_scope(client, env, world):
    assert get(client, env, "/v1/status", key="monitor").status_code == 200
    for path in (f"/v1/tail-numbers/{world.n2}", "/v1/snapshots", "/v1/aircraft/1",
                 f"/v1/tail-numbers/{world.n2}/history"):
        assert get(client, env, path, key="monitor").status_code == 403


def test_rate_limit(client, env):
    codes = [get(client, env, "/v1/snapshots", key="limited").status_code for _ in range(3)]
    assert codes[:2] == [200, 200]
    if codes[2] == 429:  # (could pass a minute boundary; then the window resets)
        r = get(client, env, "/v1/snapshots", key="limited")
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_rate_limit_split_across_workers_has_floor():
    rl = auth.RateLimiter(workers=8)
    assert rl.per_worker_limit(5) == 1 and rl.per_worker_limit(80) == 10


def test_revocation_takes_effect_after_refresh(env):
    c, keys = env
    t = [0.0]
    cache = KeyCache(c.admin_db_path, clock=lambda: t[0])
    assert cache.verify(keys["query"], TODAY) is not None
    adm = db.connect_writer(c.admin_db_path)
    prefix = keys["query"][:auth.PREFIX_LEN]
    assert auth.revoke_key(adm, prefix, revoked_by="test") == 1
    t[0] = 29.0
    assert cache.verify(keys["query"], TODAY) is not None   # cached
    t[0] = 30.5
    assert cache.verify(keys["query"], TODAY) is None       # within 30 s


def test_keys_stored_only_as_hash(env):
    c, keys = env
    dump = "\n".join(db.connect_writer(c.admin_db_path).iterdump())
    assert keys["query"] not in dump and auth.hash_key(keys["query"]) in dump


def test_usage_flush(client, env, world):
    c, keys = env
    for _ in range(3):
        get(client, env, "/v1/snapshots")
    client.get("/v1/snapshots")  # 401
    client.app.state.usage.flush()
    adm = db.connect_reader(c.admin_db_path)
    row = adm.execute("SELECT u.requests, k.last_used_at FROM api_usage_daily u JOIN api_key k ON k.id = u.api_key_id "
                      "WHERE k.name = 'q'").fetchone()
    assert row[0] == 3 and row[1] is not None
    m = adm.execute("SELECT SUM(requests), SUM(status_401) FROM api_metrics_minute").fetchone()
    assert m[0] >= 4 and m[1] >= 1


def test_reopens_database_after_file_swap(client, env, world, tmp_path):
    c, _ = env
    assert len(get(client, env, "/v1/snapshots").json()["snapshots"]) == 3
    other = tmp_path / "other" / "whodatjet.db"
    other.parent.mkdir()
    build_registry(other, world.archives[:1])
    for suffix in ("-wal", "-shm"):
        Path(f"{c.registry_db_path}{suffix}").unlink(missing_ok=True)
    os.replace(other, c.registry_db_path)
    assert len(get(client, env, "/v1/snapshots").json()["snapshots"]) == 1
    shutil.rmtree(other.parent)
