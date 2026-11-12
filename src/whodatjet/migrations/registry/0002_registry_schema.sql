-- Registry schema (plan: "Storage and historical model"). The database is derived from the
-- retained archives and can be rebuilt by replaying them in order.

CREATE TABLE archive_store (
  id INTEGER PRIMARY KEY,
  role TEXT NOT NULL CHECK (role IN ('primary','replica','retired')),
  kind TEXT NOT NULL CHECK (kind IN ('filesystem','s3')),
  location TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

-- One row per unique publication content. Snapshots are ordered by (source_date, downloaded_at).
CREATE TABLE snapshot (
  id INTEGER PRIMARY KEY,
  source_date TEXT NOT NULL,
  source_date_basis TEXT NOT NULL CHECK (source_date_basis IN
    ('http_last_modified','zip_member_mtime','download_date')),
  downloaded_at TEXT NOT NULL,
  archive_store_id INTEGER NOT NULL REFERENCES archive_store(id),
  archive_key TEXT NOT NULL,
  archive_bytes INTEGER NOT NULL,
  archive_sha256 TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE,
  import_status TEXT NOT NULL CHECK (import_status IN ('pending','imported','failed')),
  imported_at TEXT,
  schema_version TEXT,
  master_rows INTEGER, dereg_rows INTEGER, acftref_rows INTEGER,
  stats_json TEXT,
  error_message TEXT
);
CREATE INDEX snapshot_order ON snapshot(source_date, downloaded_at);

-- Every import attempt (Phase 5 extends this to scheduled download attempts).
CREATE TABLE ingest_run (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  run_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','imported','duplicate','failed')),
  snapshot_id INTEGER REFERENCES snapshot(id),
  http_status INTEGER,
  http_last_modified TEXT,
  error_message TEXT
);

CREATE TABLE archive_copy (
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  archive_store_id INTEGER NOT NULL REFERENCES archive_store(id),
  verified_at TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, archive_store_id)
);

-- Versioned ACFTREF rows. last_seen_snapshot_id NULL = present in the latest snapshot.
CREATE TABLE aircraft_model (
  id INTEGER PRIMARY KEY,
  mfr_mdl_code TEXT NOT NULL,
  manufacturer_name TEXT,
  model_name TEXT,
  aircraft_type TEXT,
  engine_type TEXT,
  aircraft_category TEXT,
  engine_count INTEGER,
  seats INTEGER,
  weight_class TEXT,
  row_hash TEXT NOT NULL,
  first_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  last_seen_snapshot_id INTEGER REFERENCES snapshot(id)
);
CREATE INDEX aircraft_model_code ON aircraft_model(mfr_mdl_code, first_seen_snapshot_id);
CREATE UNIQUE INDEX aircraft_model_open ON aircraft_model(row_hash) WHERE last_seen_snapshot_id IS NULL;

CREATE TABLE aircraft (
  id INTEGER PRIMARY KEY,
  faa_unique_id TEXT UNIQUE,
  mfr_mdl_code TEXT NOT NULL,
  serial_number_raw TEXT,
  serial_number_norm TEXT,
  identity_basis TEXT NOT NULL CHECK (identity_basis IN ('unique_id','model_serial','source_row')),
  identity_key TEXT NOT NULL UNIQUE,
  first_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id)
);
CREATE INDEX aircraft_model_serial ON aircraft(mfr_mdl_code, serial_number_norm);

CREATE TABLE identity_event (
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  kind TEXT NOT NULL CHECK (kind IN
    ('unique_id_attributes_changed','model_serial_new_unique_id','ambiguous_dereg_match')),
  aircraft_id INTEGER REFERENCES aircraft(id),
  detail_json TEXT NOT NULL
);
CREATE INDEX identity_event_snapshot ON identity_event(snapshot_id, kind);

CREATE TABLE tail_number (
  id INTEGER PRIMARY KEY,
  n_number TEXT NOT NULL UNIQUE
);

-- Versioned MASTER and DEREG rows. A new version is inserted only when the row hash (over the
-- stored fields) changes; a version is closed when its row changes or disappears.
CREATE TABLE registration_record (
  id INTEGER PRIMARY KEY,
  source_file TEXT NOT NULL CHECK (source_file IN ('master','dereg')),
  aircraft_id INTEGER NOT NULL REFERENCES aircraft(id),
  tail_number_id INTEGER REFERENCES tail_number(id),
  n_number_raw TEXT,
  faa_unique_id TEXT,
  mfr_mdl_code TEXT,
  serial_number_raw TEXT,
  year_mfr TEXT,
  status_code TEXT,
  registrant_type TEXT,
  registrant_name TEXT,
  street TEXT, street2 TEXT, city TEXT, state TEXT, zip_code TEXT, country TEXT,
  certificate_issue_date TEXT,
  expiration_date TEXT,
  cancel_date TEXT,
  last_action_date TEXT,
  mode_s_code_hex TEXT,
  row_hash TEXT NOT NULL,
  first_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  last_seen_snapshot_id INTEGER REFERENCES snapshot(id)
);
CREATE INDEX registration_record_aircraft ON registration_record(aircraft_id, first_seen_snapshot_id);
CREATE INDEX registration_record_tail ON registration_record(tail_number_id, first_seen_snapshot_id);
CREATE INDEX registration_record_hash ON registration_record(source_file, row_hash);
CREATE INDEX registration_record_open ON registration_record(source_file, row_hash)
  WHERE last_seen_snapshot_id IS NULL;

-- Interval table, populated by reconciliation (plan Phase 3).
CREATE TABLE tail_number_assignment (
  id INTEGER PRIMARY KEY,
  tail_number_id INTEGER NOT NULL REFERENCES tail_number(id),
  aircraft_id INTEGER NOT NULL REFERENCES aircraft(id),
  basis TEXT NOT NULL CHECK (basis IN ('observed','dereg_record')),
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  certificate_issue_date TEXT,
  first_seen_snapshot_id INTEGER REFERENCES snapshot(id),
  last_seen_snapshot_id INTEGER REFERENCES snapshot(id),
  first_absent_snapshot_id INTEGER REFERENCES snapshot(id),
  source_record_id INTEGER REFERENCES registration_record(id)
);
CREATE INDEX tail_number_assignment_tail ON tail_number_assignment(tail_number_id, valid_from, valid_to);
CREATE INDEX tail_number_assignment_aircraft ON tail_number_assignment(aircraft_id, valid_from);
