-- Admin console (plan Phase 6): accounts, sessions, settings, alert state, audit log.

CREATE TABLE admin_user (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  password_changed_at TEXT,
  last_login_at TEXT,
  disabled_at TEXT,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT
);

CREATE TABLE admin_session (
  token_hash TEXT PRIMARY KEY,          -- sha256 of the cookie value
  admin_user_id INTEGER NOT NULL REFERENCES admin_user(id),
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL              -- idle timeout, pushed forward on use
);
CREATE INDEX admin_session_user ON admin_session (admin_user_id);

CREATE TABLE setting (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL
);

CREATE TABLE alert_state (
  alert_key TEXT PRIMARY KEY,
  event TEXT NOT NULL,
  first_fired_at TEXT NOT NULL,
  last_sent_at TEXT,
  resolved_at TEXT,
  detail TEXT
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  detail_json TEXT
);
CREATE INDEX audit_log_action_at ON audit_log (action, at);
-- Append-only.
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
