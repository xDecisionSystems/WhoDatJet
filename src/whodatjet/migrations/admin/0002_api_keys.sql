-- API keys and usage (plan: "API keys", "Admin database"). Admin accounts, sessions,
-- settings, alerts and audit log are added with the admin console (Phase 6).

CREATE TABLE api_key (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  owner_contact TEXT,
  key_prefix TEXT NOT NULL UNIQUE,      -- display prefix, e.g. wdj_AbC12345
  key_hash TEXT NOT NULL UNIQUE,        -- sha256 hex of the full key
  scope TEXT NOT NULL CHECK (scope IN ('query','monitor')),
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  expires_at TEXT,                      -- ISO date (inclusive) or NULL
  revoked_at TEXT,
  revoked_by TEXT,
  rate_limit_per_min INTEGER CHECK (rate_limit_per_min IS NULL OR rate_limit_per_min > 0),
  last_used_at TEXT,
  note TEXT
);

CREATE TABLE api_usage_daily (
  api_key_id INTEGER NOT NULL REFERENCES api_key(id),
  day TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (api_key_id, day)
);

-- One row per worker flush per minute; aggregate with SUM/MAX when reading.
CREATE TABLE api_metrics_minute (
  minute TEXT NOT NULL,                 -- UTC, YYYY-MM-DDTHH:MM
  worker TEXT NOT NULL,
  requests INTEGER NOT NULL,
  p95_ms REAL,
  status_401 INTEGER NOT NULL DEFAULT 0,
  status_429 INTEGER NOT NULL DEFAULT 0,
  status_503 INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (minute, worker)
);
