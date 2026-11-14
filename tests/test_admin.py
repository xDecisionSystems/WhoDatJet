"""Admin console (Phase 6): authentication, CSRF, forced password change, keys, notifications, alerts, watchdog."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from whodatjet import alerts, auth, db, watchdog
from whodatjet.admin import accounts
from whodatjet.admin.app import create_app
from whodatjet.api import create_app as create_api

GOOD = "correct horse battery"
HOOK = "https://hooks.slack.com/services/T000/B000/abcdWXYZ"


@pytest.fixture
def acfg(cfg, tmp_path):
    return replace(cfg, s3=None, data_dir=tmp_path / "data")


class Slack:
    """Records Slack webhook posts."""

    def __init__(self, status=200):
        self.posts: list[str] = []
        self.status = status
        self.transport = httpx.MockTransport(self.handler)

    def handler(self, request):
        self.posts.append(request.read().decode())
        return httpx.Response(self.status, text="ok")


@pytest.fixture
def slack():
    return Slack()


@pytest.fixture
def events():
    return []


@pytest.fixture
def client(acfg, slack, events):
    app = create_app(acfg, transport=slack.transport, announce=lambda e, d: events.append((e, d)))
    with TestClient(app, base_url="https://admin.test", follow_redirects=False) as tc:
        yield tc


def form(**kw):
    return {"content": urlencode(kw), "headers": {"content-type": "application/x-www-form-urlencoded"}}


def post(client, path, **fields):
    return client.post(path, content=urlencode(fields), headers={"content-type": "application/x-www-form-urlencoded"})


def csrf_from(client, path="/password"):
    html = client.get(path).text
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def login(client, user="admin", password="admin"):
    page = client.get("/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return post(client, "/login", username=user, password=password, csrf=token)


def signed_in(client, new_password=GOOD):
    assert login(client).status_code == 303
    r = post(client, "/password", current="admin", new=new_password, confirm=new_password, csrf=csrf_from(client))
    assert r.status_code == 303
    return client


def admin_conn(cfg):
    return db.connect_writer(cfg.admin_db_path)


# ------------------------------------------------------------------ authentication

def test_default_admin_forces_password_change_on_every_route(client):
    assert login(client).status_code == 303
    for path in ("/", "/keys", "/notifications", "/monitoring", "/admins", "/audit"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/password", path
    assert client.get("/password").status_code == 200          # the change page itself works
    assert client.get("/healthz").status_code == 200


def test_posts_are_blocked_while_password_change_pending(client):
    login(client)
    r = post(client, "/keys/create", name="x", csrf=csrf_from(client))
    assert r.status_code == 303 and r.headers["location"] == "/password"


def test_password_rules(client):
    login(client)
    tok = csrf_from(client)
    for bad in ("short", "admin", "administrator-pw-x", "x" * 11):
        r = post(client, "/password", current="admin", new=bad, confirm=bad, csrf=tok)
        assert r.status_code == 400, bad
    r = post(client, "/password", current="admin", new="adminadminadmin", confirm="adminadminadmin", csrf=tok)
    assert r.status_code == 400                                 # contains the username
    r = post(client, "/password", current="nope", new=GOOD, confirm=GOOD, csrf=tok)
    assert r.status_code == 400
    r = post(client, "/password", current="admin", new=GOOD, confirm="different-one-x", csrf=tok)
    assert r.status_code == 400
    assert post(client, "/password", current="admin", new=GOOD, confirm=GOOD, csrf=tok).status_code == 303
    assert client.get("/").status_code == 200                   # forced flag cleared


def test_unauthenticated_redirects_to_login(client):
    for path in ("/", "/keys", "/audit"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_failure_is_uniform_and_audited(client, acfg):
    r1 = login(client, "admin", "wrong")
    r2 = login(client, "nobody", "wrong")
    assert r1.status_code == r2.status_code == 401
    assert "Invalid credentials" in r1.text and r1.text == r2.text or "Invalid credentials" in r2.text
    conn = admin_conn(acfg)
    reasons = {r[0] for r in conn.execute("SELECT detail_json FROM audit_log WHERE action = 'login_failed'")}
    assert any("bad_password" in x for x in reasons) and any("unknown_user" in x for x in reasons)


def test_lockout_after_five_failures(client, acfg):
    for _ in range(5):
        assert login(client, "admin", "wrong").status_code == 401
    assert login(client, "admin", "admin").status_code == 401   # correct password, but locked
    conn = admin_conn(acfg)
    conn.execute("UPDATE admin_user SET locked_until = ? WHERE username = 'admin'",
                 ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
    assert login(client, "admin", "admin").status_code == 303   # lock expired


def test_login_requires_csrf(client):
    client.get("/login")
    r = post(client, "/login", username="admin", password="admin", csrf="forged")
    assert r.status_code == 403


def test_csrf_enforced_on_state_changes(client):
    signed_in(client)
    for path in ("/keys/create", "/notifications", "/notifications/test", "/monitoring/issue", "/admins/create",
                 "/logout", "/keys/1/revoke"):
        assert post(client, path, name="x").status_code == 403, path
        assert post(client, path, name="x", csrf="0" * 64).status_code == 403, path


def test_session_cookie_flags_and_security_headers(client):
    r = login(client)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    h = client.get("/login").headers
    assert h["x-frame-options"] == "DENY" and "default-src 'self'" in h["content-security-policy"]


def test_session_idle_timeout(client, acfg):
    signed_in(client)
    assert client.get("/").status_code == 200
    conn = admin_conn(acfg)
    conn.execute("UPDATE admin_session SET expires_at = ?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_logout_ends_session(client):
    signed_in(client)
    assert post(client, "/logout", csrf=csrf_from(client, "/")).status_code == 303
    assert client.get("/").headers["location"] == "/login"


def test_password_change_invalidates_other_sessions(acfg, slack):
    app = create_app(acfg, transport=slack.transport, announce=lambda e, d: None)
    with TestClient(app, base_url="https://admin.test", follow_redirects=False) as a, \
            TestClient(app, base_url="https://admin.test", follow_redirects=False) as b:
        signed_in(a)
        assert login(b, "admin", GOOD).status_code == 303
        assert b.get("/").status_code == 200
        post(a, "/password", current=GOOD, new="another long passphrase", confirm="another long passphrase",
             csrf=csrf_from(a))
        assert b.get("/").headers["location"] == "/login"        # b's session was dropped
        assert a.get("/").status_code == 200                     # a keeps its own


def test_passwords_hashed_with_argon2id(client, acfg):
    signed_in(client)
    h = admin_conn(acfg).execute("SELECT password_hash FROM admin_user").fetchone()[0]
    assert h.startswith("$argon2id$") and GOOD not in h


def test_cli_reset_password(client, acfg):
    signed_in(client)
    conn = admin_conn(acfg)
    assert accounts.reset_password(conn, "admin", actor="cli:test")
    assert accounts.reset_password(conn, "ghost", actor="cli:test") is False
    assert login(client, "admin", "admin").status_code == 303
    assert client.get("/").headers["location"] == "/password"    # change forced again


# ------------------------------------------------------------------ API keys

def test_key_shown_once_and_stored_hashed(client, acfg):
    signed_in(client)
    r = post(client, "/keys/create", name="lab", scope="query", owner="me@example.org", expires="2030-01-01",
             rate_limit="60", note="n", csrf=csrf_from(client, "/keys"))
    key = re.search(r"(wdj_[0-9A-Za-z]{43})", r.text).group(1)
    assert r.status_code == 200
    assert key not in client.get("/keys").text                    # never shown again
    assert key not in client.get("/audit").text
    row = admin_conn(acfg).execute("SELECT * FROM api_key").fetchone()
    assert row["key_hash"] == auth.hash_key(key) and key not in " ".join(str(v) for v in tuple(row))
    assert row["key_prefix"] == key[:12] and row["rate_limit_per_min"] == 60 and row["created_by"] == "admin"


def test_key_validation_and_revoke_and_filter(client, acfg):
    signed_in(client)
    tok = csrf_from(client, "/keys")
    assert post(client, "/keys/create", name="", csrf=tok).status_code == 400
    assert post(client, "/keys/create", name="a", rate_limit="0", csrf=tok).status_code == 400
    assert post(client, "/keys/create", name="a", expires="not-a-date", csrf=tok).status_code == 400
    post(client, "/keys/create", name="one", csrf=tok)
    post(client, "/keys/create", name="two", scope="monitor", csrf=tok)
    assert post(client, "/keys/1/revoke", csrf=tok).status_code == 303
    assert "one" not in client.get("/keys?filter=active").text and "two" in client.get("/keys?filter=active").text
    assert "one" in client.get("/keys?filter=revoked").text
    actions = [r[0] for r in admin_conn(acfg).execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions.count("key_created") == 2 and actions.count("key_revoked") == 1


def test_revoked_key_stops_working_on_api(client, acfg):
    signed_in(client)
    tok = csrf_from(client, "/keys")
    key = re.search(r"(wdj_[0-9A-Za-z]{43})", post(client, "/keys/create", name="k", csrf=tok).text).group(1)
    reg = db.connect_writer(acfg.registry_db_path)
    db.migrate(reg, db.load_migrations("registry"))
    reg.close()
    api = create_api(acfg, start_flusher=False)
    with TestClient(api) as ac:
        assert ac.get("/v1/snapshots", headers={"X-API-Key": key}).status_code != 401
    post(client, "/keys/1/revoke", csrf=tok)
    with TestClient(create_api(acfg, start_flusher=False)) as ac:
        assert ac.get("/v1/snapshots", headers={"X-API-Key": key}).status_code == 401


def test_key_activity_notification_event(client, events):
    signed_in(client)
    post(client, "/keys/create", name="k", csrf=csrf_from(client, "/keys"))
    assert events and events[-1][0] == "key_activity" and "wdj_" in events[-1][1]
    assert not re.search(r"wdj_[0-9A-Za-z]{43}", events[-1][1])  # prefix only, never the key


# ------------------------------------------------------------------ admins

def test_add_disable_admins(client, acfg, events):
    signed_in(client)
    tok = csrf_from(client, "/admins")
    assert post(client, "/admins/create", username="bad name!", password="temporary1", csrf=tok).status_code == 400
    assert post(client, "/admins/create", username="bob", password="short", csrf=tok).status_code == 400
    assert post(client, "/admins/create", username="bob", password="temporary1", csrf=tok).status_code == 303
    assert post(client, "/admins/create", username="bob", password="temporary1", csrf=tok).status_code == 400
    assert post(client, "/admins/admin/disable", csrf=tok).status_code == 400    # not yourself
    assert post(client, "/admins/bob/disable", csrf=tok).status_code == 303
    assert login(TestClient(client.app, base_url="https://admin.test", follow_redirects=False), "bob",
                 "temporary1").status_code == 401
    assert post(client, "/admins/bob/enable", csrf=tok).status_code == 303
    new = TestClient(client.app, base_url="https://admin.test", follow_redirects=False)
    assert login(new, "bob", "temporary1").status_code == 303
    assert new.get("/").headers["location"] == "/password"                       # temp password -> forced change
    assert [e for e, _ in events if e == "security"]
    actions = [r[0] for r in admin_conn(acfg).execute("SELECT action FROM audit_log")]
    assert {"admin_created", "admin_disabled", "admin_enabled"} <= set(actions)


def test_cannot_disable_last_admin(acfg):
    conn = admin_conn(acfg)
    db.migrate(conn, db.load_migrations("admin"))
    accounts.ensure_default_admin(conn)
    with pytest.raises(accounts.AccountError):
        accounts.set_disabled(conn, "admin", True, actor="t")


# ------------------------------------------------------------------ notifications

def test_webhook_validation_masking_and_never_logged(client, acfg, slack):
    signed_in(client)
    tok = csrf_from(client, "/notifications")
    for bad in ("http://hooks.slack.com/services/T/B/x", "https://evil.example/services/T/B/x",
                "https://hooks.slack.com.evil.example/services/T/B/x", "https://hooks.slack.com/other"):
        assert post(client, "/notifications", webhook=bad, repeat="6", csrf=tok).status_code == 400, bad
    r = post(client, "/notifications", webhook=HOOK, repeat="6", csrf=tok)
    assert r.status_code == 200 and "Saved" in r.text
    page = client.get("/notifications").text
    assert HOOK not in page and "WXYZ" in page and "T000" not in page          # masked to the last 4
    audit = client.get("/audit").text
    assert "WXYZ" not in audit and "T000" not in audit
    # blank field keeps the stored URL; the remove box clears it
    post(client, "/notifications", webhook="", repeat="6", csrf=tok)
    assert alerts.get_setting(admin_conn(acfg), "slack_webhook_url") == HOOK
    post(client, "/notifications", webhook="", remove_webhook="1", repeat="6", csrf=tok)
    assert alerts.get_setting(admin_conn(acfg), "slack_webhook_url") is None


def test_send_test_message(client, slack):
    signed_in(client)
    tok = csrf_from(client, "/notifications")
    assert "No webhook" in post(client, "/notifications/test", csrf=tok).text and not slack.posts
    post(client, "/notifications", webhook=HOOK, repeat="6", csrf=tok)
    r = post(client, "/notifications/test", csrf=tok)
    assert "Test message sent" in r.text and len(slack.posts) == 1 and "test message" in slack.posts[0]
    assert HOOK not in slack.posts[0]
    slack.status = 500
    assert post(client, "/notifications/test", csrf=tok).status_code == 502


def test_event_toggles_and_repeat_interval(client, acfg):
    signed_in(client)
    tok = csrf_from(client, "/notifications")
    post(client, "/notifications", repeat="2", event_security="1", event_key_activity="1", csrf=tok)
    conn = admin_conn(acfg)
    assert alerts.event_enabled(conn, "security") and alerts.event_enabled(conn, "key_activity")
    assert not alerts.event_enabled(conn, "ingest_failed")
    assert alerts.repeat_hours(conn) == 2.0
    assert post(client, "/notifications", repeat="abc", csrf=tok).status_code == 400
    assert post(client, "/notifications", repeat="0", csrf=tok).status_code == 400


def test_defaults_per_plan(acfg):
    conn = admin_conn(acfg)
    db.migrate(conn, db.load_migrations("admin"))
    for e in ("ingest_failed", "format_changed", "data_stale", "coverage_gap", "api_unhealthy", "disk_low",
              "update_result", "security"):
        assert alerts.event_enabled(conn, e), e
    assert not alerts.event_enabled(conn, "key_activity")


def test_monitoring_page_issues_monitor_key_and_yaml(client, acfg):
    signed_in(client)
    r = post(client, "/monitoring/issue", csrf=csrf_from(client, "/monitoring"))
    key = re.search(r"(wdj_[0-9A-Za-z]{43})", r.text).group(1)
    assert "siteMonitor: https://admin.test/healthz?strict=true" in r.text
    assert "HOMEPAGE_VAR_WHODATJET_KEY" in r.text and key in r.text
    yaml_block = r.text.split('<pre id="yaml">')[1].split("</pre>")[0]
    assert key not in yaml_block                                  # the key is not embedded in the YAML
    assert admin_conn(acfg).execute("SELECT scope FROM api_key").fetchone()[0] == "monitor"


# ------------------------------------------------------------------ audit log

def test_audit_log_is_append_only(acfg):
    conn = admin_conn(acfg)
    db.migrate(conn, db.load_migrations("admin"))
    accounts.audit(conn, "t", "x")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET actor = 'y'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")


# ------------------------------------------------------------------ separation from the API

def test_admin_routes_absent_from_api_port(acfg):
    reg = db.connect_writer(acfg.registry_db_path)
    db.migrate(reg, db.load_migrations("registry"))
    adm = admin_conn(acfg)
    db.migrate(adm, db.load_migrations("admin"))
    with TestClient(create_api(acfg, start_flusher=False)) as api:
        for path in ("/login", "/keys", "/admins", "/audit", "/notifications", "/monitoring", "/password",
                     "/static/style.css"):
            assert api.get(path).status_code == 404, path
        assert api.post("/login", content="x").status_code == 404


def test_api_status_reports_active_alerts(acfg, slack):
    reg = db.connect_writer(acfg.registry_db_path)
    db.migrate(reg, db.load_migrations("registry"))
    adm = admin_conn(acfg)
    db.migrate(adm, db.load_migrations("admin"))
    key = auth.create_key(adm, name="m", scope="monitor", created_by="t").key
    alerts.fire(acfg.admin_db_path, "disk_low", "disk_low", "x", transport=slack.transport, attempts=1)
    with TestClient(create_api(acfg, start_flusher=False)) as api:
        j = api.get("/v1/status", headers={"X-API-Key": key}).json()
    assert j["active_alerts"] == 1


# ------------------------------------------------------------------ alerts: de-duplication and resolution

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def fire(cfg, slack, key="k", event="ingest_failed", when=T0, detail="boom"):
    return alerts.fire(cfg.admin_db_path, key, event, detail, now=when, transport=slack.transport, attempts=1)


@pytest.fixture
def hooked(acfg):
    conn = admin_conn(acfg)
    db.migrate(conn, db.load_migrations("admin"))
    alerts.set_setting(conn, "slack_webhook_url", HOOK, "t")
    return acfg


def test_repeat_suppression_then_resend_then_resolved(hooked, slack):
    assert fire(hooked, slack) is True and len(slack.posts) == 1
    assert fire(hooked, slack, when=T0 + timedelta(hours=1)) is False                 # suppressed
    assert fire(hooked, slack, when=T0 + timedelta(hours=5, minutes=59)) is False
    assert len(slack.posts) == 1
    assert fire(hooked, slack, when=T0 + timedelta(hours=6)) is True and len(slack.posts) == 2
    alerts.resolve(hooked.admin_db_path, "k", "all good", now=T0 + timedelta(hours=7), transport=slack.transport,
                   attempts=1)
    assert len(slack.posts) == 3 and "resolved" in slack.posts[-1]
    alerts.resolve(hooked.admin_db_path, "k", now=T0 + timedelta(hours=8), transport=slack.transport, attempts=1)
    assert len(slack.posts) == 3                                                       # exactly one resolved message
    assert fire(hooked, slack, when=T0 + timedelta(hours=9)) is True                   # a new episode alerts again
    assert len(slack.posts) == 4


def test_custom_repeat_interval(hooked, slack):
    alerts.set_setting(admin_conn(hooked), "repeat_hours", "1", "t")
    fire(hooked, slack)
    assert fire(hooked, slack, when=T0 + timedelta(minutes=59)) is False
    assert fire(hooked, slack, when=T0 + timedelta(hours=1)) is True


def test_disabled_event_recorded_but_not_sent(hooked, slack):
    alerts.set_setting(admin_conn(hooked), "event.ingest_failed", "0", "t")
    assert fire(hooked, slack) is False and not slack.posts
    conn = admin_conn(hooked)
    assert len(alerts.active(conn)) == 1
    alerts.resolve(hooked.admin_db_path, "k", now=T0, transport=slack.transport, attempts=1)
    assert not slack.posts and not alerts.active(conn)                                # nothing sent, nothing to resolve


def test_failed_post_is_retried_next_run(hooked, slack):
    slack.status = 500
    assert fire(hooked, slack) is False
    slack.status = 200
    assert fire(hooked, slack, when=T0 + timedelta(minutes=10)) is True                # not waiting 6 h


def test_messages_contain_no_secrets(hooked, slack):
    fire(hooked, slack, detail="plain detail")
    assert HOOK not in slack.posts[0] and "wdj_" not in slack.posts[0]


# ------------------------------------------------------------------ watchdog

def seed_default_admin(cfg, created_at=None):
    conn = admin_conn(cfg)
    db.migrate(conn, db.load_migrations("admin"))
    accounts.ensure_default_admin(conn)
    if created_at:
        conn.execute("UPDATE admin_user SET created_at = ?", (created_at.isoformat(timespec="seconds"),))
    return conn


def wd(cfg, slack, now=T0, api_ok=True, today=None, **kw):
    def handler(request):
        return httpx.Response(200 if api_ok else 503)
    # the Slack mock and the health mock share one transport that routes by host
    def route(request):
        return slack.handler(request) if request.url.host == "hooks.slack.com" else handler(request)
    return watchdog.run(cfg, now=now, today=today or T0.date(), transport=httpx.MockTransport(route),
                        api_url="http://127.0.0.1:8000/healthz", **kw)


def problems(checks):
    return {c.key for c in checks if c.problem}


def test_watchdog_api_needs_two_failures(hooked, slack, tmp_path):
    sf = tmp_path / "state.json"
    assert "api_unhealthy:API" not in problems(wd(hooked, slack, api_ok=False, state_file=sf))   # first blip
    assert "api_unhealthy:API" in problems(wd(hooked, slack, api_ok=False, state_file=sf))        # second
    assert any("API is" in p or "API health" in p for p in slack.posts)
    n = len(slack.posts)
    wd(hooked, slack, api_ok=False, state_file=sf)  # still failing: suppressed
    assert len(slack.posts) == n
    wd(hooked, slack, api_ok=True, state_file=sf)
    assert "resolved" in slack.posts[-1]


def test_watchdog_default_password_alert_after_24h(hooked, slack, tmp_path):
    seed_default_admin(hooked, created_at=T0 - timedelta(hours=23))
    assert "security:default_password" not in problems(wd(hooked, slack, state_file=tmp_path / "s"))
    seed_default_admin(hooked).execute("UPDATE admin_user SET created_at = ?",
                                       ((T0 - timedelta(hours=25)).isoformat(timespec="seconds"),))
    assert "security:default_password" in problems(wd(hooked, slack, state_file=tmp_path / "s"))


def test_watchdog_failed_logins(hooked, slack, tmp_path):
    conn = seed_default_admin(hooked)
    for _ in range(5):
        conn.execute("INSERT INTO audit_log (at, actor, action) VALUES (?,?,?)",
                     ((T0 - timedelta(minutes=3)).isoformat(timespec="seconds"), "admin", "login_failed"))
    assert "security:login_failures" in problems(wd(hooked, slack, state_file=tmp_path / "s"))
    later = T0 + timedelta(minutes=30)
    assert "security:login_failures" not in problems(wd(hooked, slack, now=later, state_file=tmp_path / "s"))


def test_watchdog_stale_data(hooked, slack, tmp_path):
    from tests.test_api import World, build_registry
    w = World()
    build_registry(hooked.registry_db_path, w.archives)
    from datetime import date
    assert "data_stale" not in problems(wd(hooked, slack, today=date(2026, 11, 10), state_file=tmp_path / "s"))
    assert "data_stale" in problems(wd(hooked, slack, today=date(2026, 11, 20), state_file=tmp_path / "s"))


# ------------------------------------------------------------------ served under a path prefix (https://host/admin)

@pytest.fixture
def pclient(acfg, slack, events):
    app = create_app(acfg, transport=slack.transport, announce=lambda e, d: events.append((e, d)), prefix="/admin")
    with TestClient(app, base_url="https://site.test", follow_redirects=False) as tc:
        yield tc


def test_prefix_redirects_links_and_cookies_stay_under_admin(pclient):
    r = pclient.get("/admin/")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    page = pclient.get("/admin/login")
    assert page.status_code == 200 and 'action="/admin/login"' in page.text
    assert 'href="/admin/static/style.css"' in page.text and pclient.get("/admin/static/style.css").status_code == 200
    assert "wdj_login=" in page.headers["set-cookie"] and "Path=/admin/login" in page.headers["set-cookie"]
    assert pclient.get("/login").status_code == 404                                   # nothing outside the prefix


def test_prefix_full_login_flow(pclient):
    token = re.search(r'name="csrf" value="([^"]+)"', pclient.get("/admin/login").text).group(1)
    r = post(pclient, "/admin/login", username="admin", password="admin", csrf=token)
    assert r.status_code == 303 and r.headers["location"] == "/admin/"
    assert "Path=/admin" in r.headers["set-cookie"]
    assert pclient.get("/admin/").headers["location"] == "/admin/password"            # forced change, still prefixed
    tok = re.search(r'name="csrf" value="([0-9a-f]+)"', pclient.get("/admin/password").text).group(1)
    r = post(pclient, "/admin/password", current="admin", new=GOOD, confirm=GOOD, csrf=tok)
    assert r.status_code == 303 and r.headers["location"] == "/admin/"
    dash = pclient.get("/admin/")
    assert dash.status_code == 200 and 'href="/admin/keys"' in dash.text and 'action="/admin/logout"' in dash.text
    assert 'href="/keys"' not in dash.text
    assert post(pclient, "/admin/logout", csrf=csrf_from(pclient, "/admin/")).headers["location"] == "/admin/login"


def test_prefix_monitoring_yaml_uses_site_urls(pclient):
    token = re.search(r'name="csrf" value="([^"]+)"', pclient.get("/admin/login").text).group(1)
    post(pclient, "/admin/login", username="admin", password="admin", csrf=token)
    tok = re.search(r'name="csrf" value="([0-9a-f]+)"', pclient.get("/admin/password").text).group(1)
    post(pclient, "/admin/password", current="admin", new=GOOD, confirm=GOOD, csrf=tok)
    r = post(pclient, "/admin/monitoring/issue", csrf=csrf_from(pclient, "/admin/monitoring"))
    assert "href: https://site.test/admin" in r.text
    assert "siteMonitor: https://site.test/healthz?strict=true" in r.text


def test_one_shot_announce_is_not_deduplicated_and_honors_toggle(hooked, slack):
    assert alerts.announce(hooked.admin_db_path, "update_result", "updated v1 to v2", transport=slack.transport,
                           attempts=1)
    assert alerts.announce(hooked.admin_db_path, "update_result", "updated v2 to v3", transport=slack.transport,
                           attempts=1)
    assert len(slack.posts) == 2                                         # both sent: no repeat suppression
    assert not alerts.active(admin_conn(hooked))                          # and nothing left "active"
    alerts.set_setting(admin_conn(hooked), "event.update_result", "0", "t")
    assert alerts.announce(hooked.admin_db_path, "update_result", "again", transport=slack.transport, attempts=1)
    assert len(slack.posts) == 2                                         # switched off: logged only
