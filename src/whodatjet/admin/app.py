"""Admin console (plan: "Admin console"): a separate FastAPI app, one worker, never mounted on the API port.

Server-rendered Jinja2 pages, no front-end build. State lives in admin.db; the console is read-only for
registry data. Forms are parsed by hand (urlencoded), so no multipart dependency is needed.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jinja2
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from whodatjet import __version__, alerts, auth, db, notify, status
from whodatjet.admin import accounts
from whodatjet.config import Config, load_config, load_env_file
from whodatjet.faa_client import CHICAGO

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
COOKIE = "wdj_session"
LOGIN_COOKIE = "wdj_login"
OPEN_PATHS = ("/login", "/healthz")
MAX_FORM_BYTES = 64 * 1024


async def read_form(request: Request) -> dict[str, str]:
    if request.method != "POST":
        return {}
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return {}
    return {k: v[-1] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


def create_app(cfg: Config, *, transport: httpx.BaseTransport | None = None,
               announce: Callable[[str, str], None] | None = None, prefix: str = "") -> FastAPI:
    """`transport` and `announce` are injection points for tests (Slack HTTP and fire-and-forget alerts).

    `prefix` (e.g. "/admin") serves the whole console under a path, for a reverse proxy that publishes it as
    https://host/admin. All links, redirects and cookie paths include it; the proxy forwards the path unchanged."""
    prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    inner = FastAPI(title="WhoDatJet admin", docs_url=None, redoc_url=None, openapi_url=None)
    app = inner
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @contextmanager
    def conn_ctx():
        conn = db.connect_writer(cfg.admin_db_path)
        try:
            db.migrate(conn, db.load_migrations("admin"))
            accounts.ensure_default_admin(conn)
            yield conn
        finally:
            conn.close()

    def notify_event(event: str, detail: str) -> None:
        """Fire-and-forget one-shot message (never blocks the request, never raises)."""
        if announce is not None:
            announce(event, detail)
            return

        def run() -> None:
            try:
                c = db.connect_writer(cfg.admin_db_path)
                try:
                    if alerts.event_enabled(c, event):
                        notify.send(event, detail, admin_db=cfg.admin_db_path, transport=transport)
                finally:
                    c.close()
            except Exception:
                log.exception("could not send %s notification", event)
        threading.Thread(target=run, daemon=True).start()

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; form-action 'self'"})
        return response

    def render(request: Request, name: str, sess: accounts.Session | None = None, status_code: int = 200,
               **ctx) -> HTMLResponse:
        tmpl = env.get_template(name)
        page = {"user": sess.username if sess else None, "version": __version__, "base": prefix,
                "path": request.url.path.removeprefix(prefix) or "/",
                "csrf": accounts.csrf_token(sess.token) if sess else "", **ctx}
        return HTMLResponse(tmpl.render(**page), status_code=status_code)

    def redirect(location: str, *, set_cookie: tuple[str, str] | None = None, clear: str | None = None
                 ) -> RedirectResponse:
        r = RedirectResponse(prefix + location if location.startswith("/") else location, status_code=303)
        if set_cookie:
            r.set_cookie(set_cookie[0], set_cookie[1], httponly=True, secure=True, samesite="strict",
                         path=prefix or "/")
        if clear:
            r.delete_cookie(clear, path=prefix + "/login" if clear == LOGIN_COOKIE else (prefix or "/"),
                            secure=True, httponly=True, samesite="strict")
        return r

    class Gate:
        """Resolves the session for a request; returns a redirect/403 response instead when not allowed."""

        def __init__(self, request: Request, form: dict[str, str], conn: sqlite3.Connection,
                     allow_unchanged: bool = False):
            self.response: Response | None = None
            self.session = accounts.get_session(conn, request.cookies.get(COOKIE))
            if self.session is None:
                self.response = redirect("/login", clear=COOKIE)
            elif self.session.must_change_password and not allow_unchanged:
                self.response = redirect("/password")
            elif request.method == "POST" and not accounts.csrf_ok(self.session.token, form.get("csrf")):
                self.response = Response("invalid or missing CSRF token", status_code=403)

    # ------------------------------------------------------------------ open routes

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "admin", "version": __version__}

    @app.get("/login")
    def login_page(request: Request):
        token = secrets.token_urlsafe(24)
        r = render(request, "login.html", csrf=token, error=None)
        r.set_cookie(LOGIN_COOKIE, token, httponly=True, secure=True, samesite="strict",
                     path=prefix + "/login", max_age=1800)
        return r

    @app.post("/login")
    def login_submit(request: Request, form: dict = Depends(read_form)):
        cookie = request.cookies.get(LOGIN_COOKIE, "")
        if not cookie or not secrets.compare_digest(cookie, form.get("csrf", "")):
            return Response("invalid or missing CSRF token", status_code=403)
        remote = request.client.host if request.client else ""
        with conn_ctx() as conn:
            result = accounts.login(conn, form.get("username", "").strip(), form.get("password", ""), remote=remote)
            if not result.ok:
                token = secrets.token_urlsafe(24)
                resp = render(request, "login.html", csrf=token, status_code=401,
                              error="Invalid credentials, or the account is temporarily locked.")
                resp.set_cookie(LOGIN_COOKIE, token, httponly=True, secure=True, samesite="strict",
                                path=prefix + "/login", max_age=1800)
                return resp
            session_token = accounts.new_session(conn, result.user_id or 0)
        return redirect("/", set_cookie=(COOKIE, session_token), clear=LOGIN_COOKIE)

    # ------------------------------------------------------------------ session routes

    @app.post("/logout")
    def logout(request: Request, form: dict = Depends(read_form)):
        with conn_ctx() as conn:
            g = Gate(request, form, conn, allow_unchanged=True)
            if g.response is not None:
                return g.response
            accounts.end_session(conn, g.session.token)  # type: ignore[union-attr]
        return redirect("/login", clear=COOKIE)

    @app.get("/password")
    def password_page(request: Request):
        with conn_ctx() as conn:
            g = Gate(request, {}, conn, allow_unchanged=True)
            if g.response is not None:
                return g.response
            return render(request, "password.html", g.session, error=None, forced=g.session.must_change_password)  # type: ignore[union-attr]

    @app.post("/password")
    def password_submit(request: Request, form: dict = Depends(read_form)):
        with conn_ctx() as conn:
            g = Gate(request, form, conn, allow_unchanged=True)
            if g.response is not None:
                return g.response
            sess = g.session
            assert sess is not None
            try:
                if form.get("new", "") != form.get("confirm", ""):
                    raise accounts.AccountError("the two new passwords do not match")
                accounts.change_password(conn, sess.user_id, form.get("current", ""), form.get("new", ""),
                                         keep_token=sess.token)
            except accounts.AccountError as exc:
                return render(request, "password.html", sess, status_code=400, error=str(exc),
                              forced=sess.must_change_password)
        return redirect("/")

    @app.get("/")
    def dashboard(request: Request):
        with conn_ctx() as conn:
            g = Gate(request, {}, conn)
            if g.response is not None:
                return g.response
            sess = g.session
            assert sess is not None
            today = datetime.now(CHICAGO).date()
            s = status.gather(cfg, today)
            health = status.archive_health(cfg)
            m = conn.execute("SELECT COALESCE(SUM(requests),0) r, COALESCE(SUM(status_401),0) a, "
                             "COALESCE(SUM(status_503),0) b, MAX(p95_ms) p FROM api_metrics_minute "
                             "WHERE minute >= strftime('%Y-%m-%dT%H:%M','now','-1 day')").fetchone()
            default_pw = conn.execute("SELECT 1 FROM admin_user WHERE username = 'admin' AND must_change_password = 1 "
                                      "AND disabled_at IS NULL").fetchone() is not None
            return render(request, "dashboard.html", sess, s=s, health=health, m=m, alerts=alerts.active(conn),
                          default_pw=default_pw, disk_days=status.disk_days_remaining(cfg))

    # ------------------------------------------------------------------ API keys

    def key_rows(conn: sqlite3.Connection, filt: str, today: str):
        rows = []
        for k in auth.list_keys(conn):
            state = "revoked" if k["revoked_at"] else ("expired" if k["expires_at"] and k["expires_at"] < today
                                                      else "active")
            if filt in ("active", "revoked", "expired") and state != filt:
                continue
            rows.append({**dict(k), "state": state})
        return rows

    @app.get("/keys")
    def keys_page(request: Request, filter: str = "all"):  # noqa: A002
        with conn_ctx() as conn:
            g = Gate(request, {}, conn)
            if g.response is not None:
                return g.response
            return render(request, "keys.html", g.session, keys=key_rows(conn, filter, date.today().isoformat()),
                          filter=filter, error=None, issued=None)

    @app.post("/keys/create")
    def keys_create(request: Request, form: dict = Depends(read_form)):
        with conn_ctx() as conn:
            g = Gate(request, form, conn)
            if g.response is not None:
                return g.response
            sess = g.session
            assert sess is not None
            try:
                expires = date.fromisoformat(form["expires"]) if form.get("expires") else None
                limit = int(form["rate_limit"]) if form.get("rate_limit") else None
                if limit is not None and limit < 1:
                    raise ValueError("the rate limit must be a positive number")
                issued = auth.create_key(conn, name=form.get("name", ""), scope=form.get("scope", "query"),
                                         created_by=sess.username, owner_contact=form.get("owner") or None,
                                         expires_at=expires, rate_limit_per_min=limit, note=form.get("note") or None)
            except (ValueError, sqlite3.Error) as exc:
                return render(request, "keys.html", sess, status_code=400, keys=key_rows(conn, "all", ""),
                              filter="all", error=str(exc), issued=None)
            accounts.audit(conn, sess.username, "key_created", issued.prefix, id=issued.id,
                           scope=form.get("scope", "query"), name=form.get("name", ""))
            notify_event("key_activity", f"API key {issued.prefix}… ({form.get('name', '')}) issued by {sess.username}")
            return render(request, "keys.html", sess, keys=key_rows(conn, "all", date.today().isoformat()),
                          filter="all", error=None, issued=issued)

    @app.post("/keys/{key_id}/revoke")
    def keys_revoke(key_id: int, request: Request, form: dict = Depends(read_form)):
        with conn_ctx() as conn:
            g = Gate(request, form, conn)
            if g.response is not None:
                return g.response
            sess = g.session
            assert sess is not None
            row = conn.execute("SELECT key_prefix FROM api_key WHERE id = ?", (key_id,)).fetchone()
            if row and auth.revoke_key(conn, str(key_id), revoked_by=sess.username):
                accounts.audit(conn, sess.username, "key_revoked", row["key_prefix"], id=key_id)
                notify_event("key_activity", f"API key {row['key_prefix']}… revoked by {sess.username}")
        return redirect("/keys")

    # ------------------------------------------------------------------ notifications

    def mask(url: str | None) -> str | None:
        return f"https://hooks.slack.com/services/…{url[-4:]}" if url else None

    def notif_ctx(conn: sqlite3.Connection) -> dict:
        return {"webhook": mask(alerts.get_setting(conn, "slack_webhook_url")),
                "events": [{"key": e, "title": notify.EVENTS[e], "on": alerts.event_enabled(conn, e)}
                           for e in alerts.TOGGLEABLE],
                "repeat": alerts.repeat_hours(conn)}

    @app.get("/notifications")
    def notifications_page(request: Request):
        with conn_ctx() as conn:
            g = Gate(request, {}, conn)
            if g.response is not None:
                return g.response
            return render(request, "notifications.html", g.session, message=None, error=None, **notif_ctx(conn))

    @app.post("/notifications")
    def notifications_save(request: Request, form: dict = Depends(read_form)):
        with conn_ctx() as conn:
            g = Gate(request, form, conn)
            if g.response is not None:
                return g.response
            sess = g.session
            assert sess is not None
            url = form.get("webhook", "").strip()
            try:
                if url and not notify.valid_webhook(url):
                    raise ValueError("the webhook must look like https://hooks.slack.com/services/…")
                try:
                    repeat = float(form.get("repeat", "") or alerts.DEFAULT_REPEAT_HOURS)
                except ValueError as exc:
                    raise ValueError("the repeat interval must be a number of hours") from exc
                if not 0.1 <= repeat <= 168:
                    raise ValueError("the repeat interval must be between 0.1 and 168 hours")
            except ValueError as exc:
                return render(request, "notifications.html", sess, status_code=400, message=None, error=str(exc),
                              **notif_ctx(conn))
            changed = []
            if form.get("remove_webhook"):
                alerts.delete_setting(conn, "slack_webhook_url")
                changed.append("webhook_removed")
            elif url:
                alerts.set_setting(conn, "slack_webhook_url", url, sess.username)
                changed.append("webhook_replaced")
            for e in alerts.TOGGLEABLE:
                on = "1" if form.get(f"event_{e}") else "0"
                if alerts.get_setting(conn, f"event.{e}") != on:
                    alerts.set_setting(conn, f"event.{e}", on, sess.username)
                    changed.append(f"event.{e}={on}")
            if alerts.get_setting(conn, "repeat_hours") != str(repeat):
                alerts.set_setting(conn, "repeat_hours", str(repeat), sess.username)
                changed.append(f"repeat_hours={repeat}")
            accounts.audit(conn, sess.username, "settings_changed", "notifications", changes=changed)  # no URL logged
            return render(request, "notifications.html", sess, message="Saved.", error=None, **notif_ctx(conn))

    @app.post("/notifications/test")
    def notifications_test(request: Request, form: dict = Depends(read_form)):
        with conn_ctx() as conn:
            g = Gate(request, form, conn)
            if g.response is not None:
                return g.response
            sess = g.session
            assert sess is not None
            if not notify.webhook_url(cfg.admin_db_path):
                msg, err = None, "No webhook is configured."
            elif notify.send("test", f"Test message requested by {sess.username}.", admin_db=cfg.admin_db_path,
                             transport=transport, attempts=1):
                msg, err = "Test message sent.", None
                accounts.audit(conn, sess.username, "notification_test", None)
            else:
                msg, err = None, "Slack did not accept the message; see the service log."
            return render(request, "notifications.html", sess, message=msg, error=err,
                          status_code=200 if not err else 502, **notif_ctx(conn))

    # ------------------------------------------------------------------ monitoring helper

    @app.get("/monitoring")
    def monitoring_page(request: Request):
        with conn_ctx() as conn:
            g = Gate(request, {}, conn)
            if g.response is not None:
                return g.response
            return render(request, "monitoring.html", g.session, yaml=None, issued=None)

    @app.post("/monitoring/issue")
    def monitoring_issue(request: Request, form: dict = Depends(read_form)):
        with conn_ctx() as conn:
            g = Gate(request, form, conn)
            if g.response is not None:
                return g.response
            sess = g.session
            assert sess is not None
            issued = auth.create_key(conn, name=f"homepage-{secrets.token_hex(2)}", scope="monitor",
                                     created_by=sess.username, note="issued from the Monitoring page")
            accounts.audit(conn, sess.username, "key_created", issued.prefix, id=issued.id, scope="monitor")
            notify_event("key_activity", f"monitor API key {issued.prefix}… issued by {sess.username}")
            return render(request, "monitoring.html", sess, issued=issued, yaml=services_yaml(request))

    def services_yaml(request: Request) -> str:
        admin_url = os.environ.get("WHODATJET_ADMIN_URL") or ""
        api_url = os.environ.get("WHODATJET_API_URL") or ""
        host = (request.headers.get("host") or "whodatjet.example.org").strip()
        if not admin_url:
            admin_url = f"https://{host}{prefix}"
        if not api_url:
            api_url = f"https://{host}" if prefix else "https://" + (urlparse(admin_url).hostname or host)
        return f"""- Research:
    - WhoDatJet:
        icon: mdi-airplane
        href: {admin_url}
        description: FAA aircraft registry history
        siteMonitor: {api_url}/healthz?strict=true
        widget:
          type: customapi
          url: {api_url}/v1/status
          refreshInterval: 60000
          headers:
            X-API-Key: "{{{{HOMEPAGE_VAR_WHODATJET_KEY}}}}"
          mappings:
            - field: status
              label: Status
            - field: last_coverage_date
              label: Data as of
              format: date
            - field: active_alerts
              label: Alerts
              format: number
            - field: disk_free_pct
              label: Disk free
              format: percent
"""

    # ------------------------------------------------------------------ admins

    @app.get("/admins")
    def admins_page(request: Request):
        with conn_ctx() as conn:
            g = Gate(request, {}, conn)
            if g.response is not None:
                return g.response
            users = conn.execute("SELECT username, created_at, created_by, last_login_at, disabled_at, "
                                 "must_change_password, locked_until FROM admin_user ORDER BY id").fetchall()
            return render(request, "admins.html", g.session, users=users, error=None)

    @app.post("/admins/create")
    def admins_create(request: Request, form: dict = Depends(read_form)):
        with conn_ctx() as conn:
            g = Gate(request, form, conn)
            if g.response is not None:
                return g.response
            sess = g.session
            assert sess is not None
            try:
                accounts.create_admin(conn, form.get("username", ""), form.get("password", ""),
                                      created_by=sess.username)
            except accounts.AccountError as exc:
                users = conn.execute("SELECT username, created_at, created_by, last_login_at, disabled_at, "
                                     "must_change_password, locked_until FROM admin_user ORDER BY id").fetchall()
                return render(request, "admins.html", sess, status_code=400, users=users, error=str(exc))
            notify_event("security", f"admin account {form.get('username', '').strip()} added by {sess.username}")
        return redirect("/admins")

    @app.post("/admins/{username}/{action}")
    def admins_toggle(username: str, action: str, request: Request, form: dict = Depends(read_form)):
        with conn_ctx() as conn:
            g = Gate(request, form, conn)
            if g.response is not None:
                return g.response
            sess = g.session
            assert sess is not None
            if action not in ("disable", "enable"):
                return Response("not found", status_code=404)
            try:
                if action == "disable" and username == sess.username:
                    raise accounts.AccountError("you cannot disable your own account")
                accounts.set_disabled(conn, username, action == "disable", actor=sess.username)
            except accounts.AccountError as exc:
                users = conn.execute("SELECT username, created_at, created_by, last_login_at, disabled_at, "
                                     "must_change_password, locked_until FROM admin_user ORDER BY id").fetchall()
                return render(request, "admins.html", sess, status_code=400, users=users, error=str(exc))
            notify_event("security", f"admin account {username} {action}d by {sess.username}")
        return redirect("/admins")

    @app.get("/audit")
    def audit_page(request: Request, page: int = 0):
        with conn_ctx() as conn:
            g = Gate(request, {}, conn)
            if g.response is not None:
                return g.response
            page = max(0, page)
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 101 OFFSET ?",
                                (page * 100,)).fetchall()
            return render(request, "audit.html", g.session, rows=rows[:100], page=page, more=len(rows) > 100)

    if not prefix:
        return inner
    outer = FastAPI(title="WhoDatJet admin", docs_url=None, redoc_url=None, openapi_url=None)
    outer.mount(prefix, inner)
    return outer


def create_app_from_env() -> FastAPI:
    env_file = os.environ.get("WHODATJET_ENV_FILE")
    if env_file:
        load_env_file(Path(env_file))
    return create_app(load_config(), prefix=os.environ.get("WHODATJET_ADMIN_PREFIX", ""))


