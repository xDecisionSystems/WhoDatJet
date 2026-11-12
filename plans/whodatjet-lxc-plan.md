# WhoDatJet — Aircraft Registry History Service: Implementation Plan

## Goal

Create one repository that runs in an LXC container and provides a small, API-key-protected HTTP API for historical FAA aircraft-registration lookups. It downloads the FAA Releasable Aircraft Registration Database daily, retains each unique compressed archive, ingests only data that is new or changed, and answers which aircraft was associated with an N-number on a requested date — with explicit provenance and confidence. A separate admin console, on its own port, issues and revokes API keys and configures Slack alerts for when something goes wrong.

## Proposed stack

- Python 3.12, installed and pinned with `uv` (independent of the distribution's system Python); dependencies locked in `uv.lock`
- FastAPI + Uvicorn for the HTTP API and, as a separate process, the admin console (server-rendered Jinja2 templates, minimal vanilla JavaScript, no front-end build step); `argon2-cffi` for admin password hashing
- SQLite (WAL mode, foreign keys enabled, `busy_timeout` set) for application data
- `httpx` for downloads; `boto3` for S3-compatible archive storage; standard-library `zipfile` and `csv` for archive parsing; `holidays` for the U.S. federal working-day calendar
- systemd timer for daily ingestion (no in-process scheduler)
- Debian 13 (trixie) unprivileged LXC guest on Proxmox VE 9, with systemd managing the API and importer
- `pytest`, `ruff`, and `mypy` for verification

This keeps the service lightweight and makes its data directory easy to back up. It avoids Docker-in-LXC and does not require a database server.

## Repository layout

```text
.
├── src/whodatjet/          # Python package; console entry point `whodatjet`
│   ├── api.py                 # FastAPI routes and response schemas
│   ├── config.py
│   ├── db.py                  # connections, migrations, transactions
│   ├── models.py
│   ├── normalize.py           # N-number, serial-number, field normalization
│   ├── coverage.py            # federal working-day calendar, gap/staleness detection
│   ├── faa_client.py          # download, source-date detection, checksums
│   ├── archive_store.py       # archive storage backends: filesystem, S3-compatible; replica + cache
│   ├── importer.py            # ZIP -> staging -> change detection
│   ├── timeline.py            # interval reconciliation
│   ├── auth.py                # API-key verification, cache, usage aggregation
│   ├── notify.py              # Slack webhook sender, alert de-duplication
│   ├── watchdog.py            # periodic health, staleness, and disk checks
│   ├── admin/
│   │   ├── app.py             # admin console FastAPI app (separate process/port)
│   │   ├── sessions.py        # login, sessions, CSRF, lockout
│   │   ├── templates/
│   │   └── static/
│   └── cli.py                 # download, import, rebuild, migrate, serve, keys, admin, notify commands
│   ├── migrations/            # packaged with the release
│   │   ├── registry/          # whodatjet.db (registry data)
│   │   └── admin/             # admin.db (keys, admins, settings, audit)
├── tests/
│   ├── fixtures/              # deliberately small FAA-like data files
│   ├── test_normalize.py
│   ├── test_importer.py
│   ├── test_timeline.py
│   ├── test_coverage.py
│   ├── test_api.py
│   ├── test_auth.py
│   ├── test_admin.py
│   └── test_notify.py
├── deploy/
│   ├── systemd/
│   │   ├── whodatjet-api.service
│   │   ├── whodatjet-admin.service
│   │   ├── whodatjet-ingest.service
│   │   ├── whodatjet-ingest.timer
│   │   ├── whodatjet-watchdog.service
│   │   ├── whodatjet-watchdog.timer
│   │   └── whodatjet-alert@.service   # OnFailure= hook -> whodatjet notify
│   ├── proxmox/
│   │   ├── install.sh         # runs on Proxmox host; creates/provisions LXC
│   │   ├── guest-setup.sh     # runs inside the new guest
│   │   └── templates/         # pct and firewall configuration templates
│   └── update.sh              # guest updater, installed as /usr/local/bin/whodatjet-update
├── data/                      # local development runtime data; ignored by Git
├── .github/workflows/
│   └── release.yml            # builds release assets, manifest, and Sigstore signature
├── pyproject.toml
├── uv.lock
├── README.md
├── docs/
│   └── homepage.md            # Homepage (gethomepage.dev) integration guide and example services.yaml
└── plans/
```

In production, runtime data lives on the persistent volume at `/var/lib/whodatjet/{archives,staging,logs,backups,tls}` plus `whodatjet.db` (registry data) and `admin.db` (API keys, admin accounts, settings, audit log).

## FAA source data

The download (currently `ReleasableAircraft.zip`) contains comma-delimited text files. The relevant ones are:

| File | Use |
|---|---|
| `MASTER.txt` | Currently registered aircraft. Keyed by N-number (stored **without** the leading `N`). Contains serial number, `MFR MDL CODE`, registrant/address fields, status, certificate issue/expiration dates, Mode S code (octal and hex). |
| `ACFTREF.txt` | Aircraft reference table joined via `MFR MDL CODE` (7 characters: 3 manufacturer, 2 model, 2 series) to obtain manufacturer and model names. Contents change over time. |
| `DEREG.txt` | Cumulative deregistered/cancelled registrations with certificate issue and cancel dates, reaching back decades. |
| `ENGINE.txt`, `DEALER.txt`, `DOCINDEX.txt`, `RESERVED.txt` | Retained in the archive; not ingested initially. |

Parsing rules: match columns by header name, never position; strip whitespace, a possible UTF-8 BOM, and trailing empty columns. Required files (`MASTER`, `ACFTREF`, `DEREG`) and required columns are configuration. If any is missing, the import fails with a schema-mismatch error and changes nothing. The FAA publishes on federal working days (stated refresh 11:30 p.m. Central).

## Storage and historical model

Retained archives are the authoritative evidence. The database is a derived index and can be rebuilt by replaying the archives in order (`whodatjet rebuild`).

Two hashes are recorded for every download:

- `archive_sha256` — hash of the ZIP bytes, used for integrity verification of the stored file.
- `content_sha256` — hash of the sorted (member name, member-content hash) list. It is used for de-duplication, because a re-zipped but otherwise identical publication has different ZIP bytes.

Only downloads with new content are retained. They go to the configured archive store (see [Archive storage](#archive-storage)) under the key `daily/<source_date>/<content_sha256>.zip`. Every backend uses the same keys.

History is append-only. The one in-place change allowed is closing an open interval, which sets its end once. Dates labelled **observed** mean "present in FAA downloads verified on these dates", not the legal effective date of a registration change. Dates derived from `DEREG.txt` certificate/cancel fields are labelled with a separate basis.

### Core tables

```sql
-- Every scheduled or manual attempt, including failures and duplicates.
ingest_run (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  run_date DATE NOT NULL,             -- America/Chicago calendar date of the run
  status TEXT NOT NULL CHECK (status IN
    ('running','imported','duplicate','failed')),
  snapshot_id INTEGER REFERENCES snapshot(id),  -- new or matched snapshot
  http_status INTEGER,
  http_last_modified TEXT,
  error_message TEXT
);

-- Configured archive locations (primary and optional replica).
archive_store (
  id INTEGER PRIMARY KEY,
  role TEXT NOT NULL CHECK (role IN ('primary','replica','retired')),
  kind TEXT NOT NULL CHECK (kind IN ('filesystem','s3')),
  location TEXT NOT NULL,             -- path, or s3://bucket/prefix @ endpoint
  created_at TEXT NOT NULL
);

-- Which stores hold a verified copy of each archive.
archive_copy (
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  archive_store_id INTEGER NOT NULL REFERENCES archive_store(id),
  verified_at TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, archive_store_id)
);

-- One row per unique publication content.
snapshot (
  id INTEGER PRIMARY KEY,
  source_date DATE NOT NULL,          -- FAA publication date
  source_date_basis TEXT NOT NULL CHECK (source_date_basis IN
    ('http_last_modified','zip_member_mtime','download_date')),
  downloaded_at TEXT NOT NULL,
  archive_store_id INTEGER NOT NULL REFERENCES archive_store(id),
  archive_key TEXT NOT NULL,          -- e.g. daily/2026-11-05/<content_sha256>.zip
  archive_bytes INTEGER NOT NULL,
  archive_sha256 TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE,
  import_status TEXT NOT NULL CHECK (import_status IN ('pending','imported','failed')),
  schema_version TEXT,
  master_rows INTEGER, dereg_rows INTEGER, acftref_rows INTEGER,
  error_message TEXT
);

aircraft_model (                       -- versioned ACFTREF rows
  id INTEGER PRIMARY KEY,
  mfr_mdl_code TEXT NOT NULL,
  manufacturer_name TEXT, model_name TEXT,
  aircraft_type TEXT, engine_type TEXT, seats INTEGER,
  row_hash TEXT NOT NULL,
  first_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  last_seen_snapshot_id INTEGER REFERENCES snapshot(id)   -- NULL = current
);

aircraft (
  id INTEGER PRIMARY KEY,
  faa_unique_id TEXT UNIQUE,          -- MASTER `UNIQUE ID`; NULL for DEREG-only aircraft; set once
  mfr_mdl_code TEXT NOT NULL,         -- full 7-character code when first seen
  serial_number_raw TEXT,
  serial_number_norm TEXT,            -- uppercase, spaces and hyphens removed
  identity_basis TEXT NOT NULL CHECK (identity_basis IN ('unique_id','model_serial','source_row')),
  identity_key TEXT NOT NULL UNIQUE,  -- 'U|<unique id>', 'S|<code>|<serial>', or 'R|<n-number>|<cert date>|<code>'
  first_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id)
);
CREATE INDEX aircraft_model_serial ON aircraft(mfr_mdl_code, serial_number_norm);

-- Evidence about whether identities behave as assumed (see Identity rules).
identity_event (
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  kind TEXT NOT NULL CHECK (kind IN
    ('unique_id_attributes_changed','model_serial_new_unique_id','ambiguous_dereg_match')),
  aircraft_id INTEGER REFERENCES aircraft(id),
  detail_json TEXT NOT NULL
);

tail_number (
  id INTEGER PRIMARY KEY,
  n_number TEXT NOT NULL UNIQUE        -- normalized with prefix: N123AB
);

-- Versioned registration rows from MASTER and DEREG. A new version is
-- inserted only when the normalized row hash changes; a version is closed
-- when its row changes or disappears.
registration_record (
  id INTEGER PRIMARY KEY,
  source_file TEXT NOT NULL CHECK (source_file IN ('master','dereg')),
  aircraft_id INTEGER NOT NULL REFERENCES aircraft(id),
  tail_number_id INTEGER REFERENCES tail_number(id),
  mfr_mdl_code TEXT,
  status_code TEXT,
  registrant_name TEXT, registrant_type TEXT,
  street TEXT, street2 TEXT, city TEXT, state TEXT, zip_code TEXT, country TEXT,
  certificate_issue_date DATE,
  expiration_date DATE,
  cancel_date DATE,                   -- DEREG only
  mode_s_code_hex TEXT,
  row_hash TEXT NOT NULL,
  first_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  last_seen_snapshot_id INTEGER REFERENCES snapshot(id)   -- NULL = current
);

tail_number_assignment (
  id INTEGER PRIMARY KEY,
  tail_number_id INTEGER NOT NULL REFERENCES tail_number(id),
  aircraft_id INTEGER NOT NULL REFERENCES aircraft(id),
  basis TEXT NOT NULL CHECK (basis IN ('observed','dereg_record')),
  valid_from DATE NOT NULL,           -- observed: first verified date; dereg: certificate issue date
  valid_to DATE,                      -- observed: last verified date present; dereg: cancel date; NULL = ongoing
  certificate_issue_date DATE,        -- distinguishes re-registrations of the same pair
  first_seen_snapshot_id INTEGER REFERENCES snapshot(id),
  last_seen_snapshot_id INTEGER REFERENCES snapshot(id),
  first_absent_snapshot_id INTEGER REFERENCES snapshot(id),  -- bracket for the change
  source_record_id INTEGER REFERENCES registration_record(id)
);
```

Indexes: `registration_record(aircraft_id, first_seen_snapshot_id)`, `registration_record(tail_number_id, first_seen_snapshot_id)`, `registration_record(source_file, row_hash)`, `tail_number_assignment(tail_number_id, valid_from, valid_to)`, `tail_number_assignment(aircraft_id, valid_from)`, `aircraft_model(mfr_mdl_code, first_seen_snapshot_id)`. Store normalized fields plus row hashes. Keep the source ZIP rather than duplicating every raw CSV field.

### Identity rules

Decided after Phase 0. The original rule (3-character manufacturer prefix + serial) produced 7,833 collisions in a single MASTER snapshot, mostly amateur-built aircraft with serial `1`, and 329 of 559 prefixes map to more than one manufacturer name.

- **MASTER rows: FAA `UNIQUE ID`** (`identity_basis = 'unique_id'`). It is present and distinct on every MASTER row (318,990 of 318,990).
- **DEREG rows** have no `UNIQUE ID`. Each is linked to an existing aircraft with the same full `MFR MDL CODE` + normalized serial, if exactly one exists. If none exists, a `model_serial` aircraft is created. If several exist, a new aircraft is created and an `ambiguous_dereg_match` event is recorded. Full code + serial had one collision across all of MASTER.
- **Limitation (Phase 2 finding):** only 82 of 383,756 DEREG rows match a current MASTER aircraft on full code + serial, because the FAA reassigns model/series codes over time. Looser keys collide too often to link automatically: dropping the 2-character series gives 4,574 collisions within MASTER (1,100 even with year of manufacture). Most DEREG history therefore stands as separate `model_serial` aircraft. Tail-number lookups are unaffected, because each DEREG row carries its own serial and model. Cross-N-number airframe history mostly covers time in MASTER. A looser, explicitly flagged "probable link" layer can be added later without changing identities.
- **Re-registration of a DEREG-only aircraft:** when a new MASTER `UNIQUE ID` has the same full code + serial as exactly one `model_serial` aircraft with no `faa_unique_id`, that aircraft's `faa_unique_id` is set (once) instead of creating a new aircraft.
- **Placeholder or empty serials** (`0`, `00`, empty, `NONE`, `UNKNOWN`) never match on serial. DEREG rows with one get a `source_row` identity and are flagged `low_identity`.
- **`UNIQUE ID` stability is not yet proven** (one snapshot cannot show it). The importer records evidence in `identity_event`: a `UNIQUE ID` whose code + serial changes (`unique_id_attributes_changed`), and a code + serial that reappears under a new `UNIQUE ID` after the old one disappears (`model_serial_new_unique_id`). If events show `UNIQUE ID` is not stable, identity rules change and the database is rebuilt from archives.
- Serial normalization (uppercase, spaces and hyphens removed; leading zeros kept, since stripping them would merge 1,149 keys) is unit-tested against the Phase 0 fixtures. Identity changes after launch require a rebuild.

### N-number normalization

Uppercase the input, remove spaces and hyphens, strip a leading `N`, and validate against FAA format: 1–5 characters, first character `1`–`9`, no `I` or `O`, at most two trailing letters. Store and return with the `N` prefix. Invalid input returns `400`.

### Reconciliation rules

1. Download to `staging/`, then validate the ZIP and the required file set. Compute both hashes and determine `source_date`, preferring HTTP `Last-Modified` converted to America/Chicago, then the newest ZIP member mtime, then the download date. Record the basis used.
2. If `content_sha256` already exists, record an `ingest_run` with status `duplicate` linked to the existing snapshot, delete the staged file, and stop. A duplicate on an expected publication day still counts as a verified-coverage date.
3. Otherwise write the ZIP to the primary archive store and verify it: for the filesystem backend, atomic rename plus re-hash; for S3, `PUT` with `x-amz-checksum-sha256`, or a SHA-256 metadata check plus size comparison where the service lacks checksum support. Only then create the `snapshot` row (`pending`) and load MASTER, DEREG, and ACFTREF from the **local staging copy** into temporary staging tables. If the primary store write fails, the run fails and the staging file is kept for the next attempt. Replica copies are made after the import (see [Archive storage](#archive-storage)).
4. In **one transaction**, apply changes relative to the previous imported snapshot:
   - `aircraft_model`, `registration_record`: insert rows whose `(source_file, row_hash)` is not open; close open versions whose hash is no longer present.
   - Observed assignments come from MASTER only. Extend an open assignment when the same N-number ↔ aircraft pair is present **with the same certificate issue date**. Otherwise open a new one. Close an assignment whose pair is absent, or whose certificate issue date changed: set `valid_to` to the last verified-coverage date before this snapshot, and record `last_seen_snapshot_id` and `first_absent_snapshot_id`.
   - DEREG-based assignments are created once for each new DEREG row that has an N-number, certificate issue date, and cancel date (`basis = 'dereg_record'`). They do not overwrite observed assignments.
5. Mark the snapshot `imported` and the run `imported`. On any error, roll back the transaction, mark both `failed`, and keep the archive for re-import. A failed or partial import never closes intervals.
6. Never infer an exact legal transfer time between downloads. Responses report the snapshots that bracket a change.
7. Only one importer may run at a time: `flock` on `/var/lib/whodatjet/ingest.lock`.

### Coverage, gaps, and staleness

- **Expected publication days** are U.S. federal working days, computed with the `holidays` package.
- **Verified-coverage dates** are the source dates of runs that ended `imported` or `duplicate`.
- A **gap** is a span between consecutive verified-coverage dates that contains more than a configurable number of missed expected publication days (default: 2). Changes detected across a gap are reported with `gap` confidence. Weekends and holidays are never gaps.
- Observed coverage starts at the first daily import. Dates before it are answered only from `dereg_record` intervals (see [Daily ingestion](#daily-ingestion)).
- Data is **stale** when the most recent verified-coverage date is older than a configurable threshold (default: 4 calendar days).

## API

Initial API version, documented with OpenAPI at `/docs`. Every `/v1/*` endpoint requires an API key (see [API keys](#api-keys)). `/healthz`, `/docs`, and `/openapi.json` are open and contain no registry data.

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness, database health, last verified-coverage date, data age; `?strict=true` returns `503` when stale or when the last run failed |
| `GET /v1/tail-numbers/{n_number}?date=YYYY-MM-DD` | tail-number association and best FAA record as of the requested date |
| `GET /v1/tail-numbers/{n_number}/history` | all associations with basis, interval, and bracketing-snapshot metadata |
| `GET /v1/aircraft/{aircraft_id}` | canonical aircraft identity, model history, and N-number history |
| `GET /v1/snapshots` | snapshot dates, import state, ingest runs, and detected gaps |
| `GET /v1/status` | flat JSON operational summary for dashboards such as Homepage (see [Homepage monitoring](#homepage-monitoring)); accepts `monitor` or `query` keys |

### As-of semantics for `GET /v1/tail-numbers/{n}?date=D`

1. Normalize the N-number (`400` if invalid). If `D` is in the future, return `400`. If `date` is omitted, use today's date (America/Chicago).
2. If `D` is within observed coverage, find the `observed` assignment covering `D`. Return it with the `registration_record` and `aircraft_model` versions current at the latest snapshot on or before `D`.
3. Otherwise fall back to a `dereg_record` assignment covering `D`.
4. If nothing covers `D`, return `404` with `coverage_start`, `coverage_end`, and whether `D` falls before observed coverage, inside a gap, or in an unassigned period.
5. If `D` is after the last verified-coverage date, answer from the latest data and add the `stale` flag.

Every lookup response contains `observation_date`, `snapshot_id`, `archive_sha256`, `basis`, `data_confidence`, and `data_quality_flags`.

### Data confidence

`data_quality_flags` lists any of the following:

| Flag | Meaning |
|---|---|
| `low_identity` | aircraft identity lacks a usable serial number |
| `gap` | answer or bracketing change spans a coverage gap |
| `stale` | requested date is beyond the last verified-coverage date |
| `dereg_record` | answer comes from DEREG certificate/cancel dates, not daily observation |
| `bracketed_change` | `D` falls between `last_seen` and `first_absent` snapshots of a change |

`data_confidence` is `high` when there are no flags, `low` when `low_identity` or `gap` is present, and `medium` otherwise.

### Field selection

The lookup endpoints accept an optional comma-separated `fields` query parameter, for example:

```text
GET /v1/tail-numbers/N123AB?date=2026-10-01&fields=manufacturer_name,model_name,serial_number,status_code
```

The API validates every requested field against a fixed, documented allow-list rather than accepting SQL column names. When `fields` is absent, empty, or `*`, the response contains every permitted field, including registrant and address fields. When `fields` is present, the response contains only the named fields plus required metadata (`observation_date`, `snapshot_id`, `basis`, `data_confidence`, `data_quality_flags`). An unknown field returns `400` with the accepted field names.

### Access and historical fidelity

This is a research database. The API serves every record exactly as the FAA originally published it. If a registrant's details are later withheld or removed from the FAA file, earlier versions are not changed or hidden. Access control has two layers: the network (tailnet membership or the LAN firewall CIDR), and a per-user API key issued from the admin console. Keys identify who is querying and can be revoked individually. Anyone using registrant/address data is responsible for complying with their institution's data-handling requirements.

### Concurrency and performance

The API serves many simultaneous users. The only writer to the registry database (`whodatjet.db`) is the importer (daily runs and `rebuild`), and the ingest lock guarantees only one runs at a time. Users never write registry data. Admin-console changes and API-key usage counters go to the separate `admin.db` (see [Admin console](#admin-console)), so they never contend with an import. This matches SQLite in WAL mode: any number of readers in parallel, one writer, and readers and the writer never block each other.

- **Snapshot isolation:** an import is one transaction. Queries running during it see the previous committed state, and the next query after the commit sees the complete new snapshot. Users never see a partially imported day.
- **Connections:** each Uvicorn worker process opens its own connections, one per thread, never shared across threads. API connections are read-only (`file:whodatjet.db?mode=ro` URI plus `PRAGMA query_only=ON`).
- **Workers:** the API runs `UVICORN_WORKERS` processes, set from the sizing profile (see the installer section). Endpoints are synchronous `def` handlers, so FastAPI runs them in its thread pool, and `sqlite3` releases the GIL while a query executes.
- **Load shedding:** Uvicorn's `--limit-concurrency` is set per profile, so overload returns a fast `503` instead of every request slowing down.
- **Caching:** set `PRAGMA mmap_size` and `cache_size` from the profile's memory, so that hot indexes stay in RAM.
- **WAL maintenance:** run `PRAGMA wal_checkpoint(TRUNCATE)` after each import. Keep read transactions to one per request, so they never stall checkpoints.
- **Storage requirement:** the databases live on the main data mount point, which must use block-style Proxmox storage (local disk, LVM, ZFS, or Ceph RBD). Never put the databases on an NFS/CIFS share mounted directly: WAL needs shared memory between processes on the same host. Archives have no such restriction and may live on a NAS or object storage (see [Archive storage](#archive-storage)).
- **Large result sets** (history and future bulk endpoints) are paginated, so no request holds a read transaction open for long.
- **Expected performance:** indexed lookups take well under 1 ms. Throughput is limited by Python/FastAPI and JSON serialization, not by SQLite. API-key checks are served from an in-memory cache, so they add no measurable per-request cost.

## Admin console

### Process, port, and exposure

- A separate `whodatjet-admin.service` process with one Uvicorn worker, bound to `127.0.0.1:8001`. The public API process does not include admin routes, so the admin console cannot be reached through the API port.
- **Tailscale mode:** published with `tailscale serve --bg --https=8443 http://127.0.0.1:8001`, giving `https://whodatjet.<tailnet>.ts.net:8443` (the API stays on 443). The admin port can be restricted further with a Tailscale ACL grant to admin users or a group.
- **LAN mode:** binds `0.0.0.0:<admin port>` (default `8001`) with its own Proxmox firewall rule and its own source CIDR, which the installer asks for separately and recommends keeping narrower than the API's. It serves HTTPS using a self-signed certificate generated at install time (in `/var/lib/whodatjet/tls/`, fingerprint printed), or an operator-supplied certificate. Admin passwords never travel over plain HTTP.

### Admin authentication

- **Default credentials:** a new install has one admin account, `admin` / `admin`, flagged `must_change_password`. While that flag is set, every console route except the change-password page and logout redirects to the change-password page. API-key issuing, settings, and all other pages are unusable until the password is changed. The new password must be at least 12 characters and cannot be `admin` or contain the username. Changing it clears the flag and invalidates all other sessions for that account.
- **Default-credential safeguards:** the admin port is reachable only on the tailnet (Tailscale mode) or from the admin CIDR (LAN mode). The dashboard and the installer's completion message state that the default password must be changed. The watchdog raises a **Security** alert if the default password is still unchanged 24 hours after installation, and `whodatjet status` shows it.
- Passwords are hashed with argon2id. Admins created later from the console start with a temporary password and the same forced change at first login.
- Sessions use a random token stored server-side, sent in an `HttpOnly`, `Secure`, `SameSite=Strict` cookie, with an 8-hour idle timeout. Every state-changing form carries a CSRF token.
- After 5 consecutive failed logins, an account is locked for 15 minutes.
- Admins can add and disable other admin accounts. Recovery from inside the guest: `sudo whodatjet admin reset-password <user>` resets the password to `admin` with the forced change set again.

### Pages

| Page | Contents |
|---|---|
| **Dashboard** | Installed version and sizing profile; last ingest run and result; last verified-coverage date and staleness; open gaps; snapshot count; data-volume usage and projected days until full; archive store and replica status (location, reachability, replica lag, last verification); API request rate, p95 latency, `401`/`503` counts over the last 24 h; active alerts |
| **API keys** | List of keys (name, owner/contact, key prefix, created, created by, expiry, last used, 24 h / 30 d request counts, status). Create, revoke, and filter. |
| **Notifications** | Slack webhook URL, per-event toggles, repeat interval, and a **Send test message** button |
| **Monitoring** | Homepage integration helper: issues a `monitor`-scoped key and generates a ready-to-paste `services.yaml` entry with this server's URLs filled in |
| **Admins** | Add or disable admin accounts; change own password |
| **Audit log** | Append-only record of admin logins (success/failure), key creation/revocation, settings changes, and admin-account changes |

The console is read-only for registry data. It does not start imports or updates, which stay on the CLI and systemd, so the admin process needs no root privileges.

### API keys

- **Issuing:** an admin enters a name, a scope (`query` or `monitor`), an owner/contact, an optional expiry date, an optional per-key rate limit (requests/minute), and an optional note. The full key is shown **once**, with a copy button, and cannot be retrieved afterwards. Rotation means issuing a new key and revoking the old one.
- **Format:** `wdj_` followed by 32 random bytes in base62 (about 47 characters total). Only the display prefix and a SHA-256 hash of the full key are stored. A fast hash is appropriate because keys are high-entropy random values, not passwords. Comparison is constant-time.
- **Scopes:** each key has one scope. `query` keys (the default) can call every `/v1/*` endpoint. `monitor` keys can call only `GET /v1/status` and receive `403` everywhere else, so a dashboard credential can never read registry or registrant data.
- **Use:** clients send `Authorization: Bearer <key>`; `X-API-Key: <key>` is also accepted. A missing, unknown, revoked, or expired key gets `401` with one uniform message. A key over its rate limit gets `429` with `Retry-After`.
- **Verification path:** each API worker caches valid key hashes from `admin.db` in memory and refreshes them every 30 seconds, so a revocation takes effect within 30 seconds without a database read per request. Rate limits are enforced per worker (limit ÷ worker count), which is approximate but needs no shared state.
- **Usage tracking:** workers aggregate per-key request/error counts, last-used time, and latency summaries in memory, and flush them to `admin.db` every 60 seconds. That is one small write per worker per minute, never one per request.
- **Bootstrap and scripting:** the installer issues a first key (named `initial`) and prints it once, so the API is usable immediately. `whodatjet keys create|list|revoke` provides the same functions from the guest shell, for automation or if the console is unavailable.

### Admin database (`admin.db`)

Admin state lives in its own SQLite file rather than in `whodatjet.db`:

- The importer holds `whodatjet.db`'s write lock for the length of an import transaction (minutes, or much longer for `rebuild`). A separate file means issuing a key or saving a webhook never waits on an import.
- `whodatjet.db` stays purely derived from archives, so `rebuild` can replace it without touching keys or settings.
- The importer remains the sole writer of registry data. `admin.db` writes are tiny and infrequent.

```sql
admin_user (id, username UNIQUE, password_hash, must_change_password, created_at, created_by,
            password_changed_at, last_login_at, disabled_at, failed_attempts, locked_until);
admin_session (token_hash PRIMARY KEY, admin_user_id, created_at, last_seen_at, expires_at);
api_key (id, name, owner_contact, key_prefix UNIQUE, key_hash UNIQUE, scope,
         created_at, created_by, expires_at, revoked_at, revoked_by,
         rate_limit_per_min, last_used_at, note);
api_usage_daily (api_key_id, day, requests, errors, PRIMARY KEY (api_key_id, day));
api_metrics_minute (minute PRIMARY KEY, requests, p95_ms, status_401, status_429, status_503);
setting (key PRIMARY KEY, value, updated_at, updated_by);   -- includes slack_webhook_url
alert_state (alert_key PRIMARY KEY, first_fired_at, last_sent_at, resolved_at, detail);
audit_log (id, at, actor, action, target, detail_json);      -- append-only
```

`admin.db` is mode `0600`, owned by the service account. Minute-level metrics are kept for 7 days and daily usage indefinitely.

### Slack notifications

- **Configuration:** on the Notifications page, an admin pastes a Slack *incoming webhook* URL. The URL must match `https://hooks.slack.com/services/…`, which also stops the console from being used to make the server request arbitrary URLs. After saving, it is shown masked (last 4 characters only), never written to logs, and can be replaced or removed. **Send test message** confirms it works.
- **Sender:** `whodatjet notify` posts the message. It is called by the `OnFailure=` alert unit, the watchdog, the updater, and the importer. Failed posts retry 3 times with backoff; if Slack stays unreachable, the alert is logged to the journal and still appears on the dashboard.
- **Watchdog:** `whodatjet-watchdog.timer` runs every 10 minutes and checks API health, data staleness, and disk headroom.
- **Events** (each can be toggled; the first six are on by default):

| Event | Trigger |
|---|---|
| Ingest failed | Import failed or download retries were exhausted (`OnFailure=` of the ingest unit) |
| FAA format changed | A required file or column is missing (schema-mismatch failure) |
| Data stale | No verified coverage for longer than the staleness threshold |
| Coverage gap | An import detects a new gap |
| API unhealthy | The watchdog's local `/healthz` check fails twice in a row, or the API or admin unit has failed |
| Disk space low | The data volume or a filesystem archive store has less than 15% free, or under 60 days of projected headroom |
| Archive store problem | The primary or replica store is unreachable or rejects writes, a replica has fallen behind by more than 1 day, or verification found a missing or corrupt archive |
| Update result | `whodatjet-update` succeeded, failed, or rolled back (on by default) |
| Security | 5 or more failed admin logins in 15 minutes, an admin account was added or disabled, or the default `admin` password is still unchanged 24 h after install (on by default) |
| Key activity | An API key was issued or revoked (off by default) |

- **De-duplication:** each condition has an `alert_state` row. While a condition persists, it is re-sent at most once per repeat interval (default 6 hours), and a single **resolved** message is sent when it clears.
- **Message content:** event, host, installed version, a short detail line, and a link to the admin dashboard. Messages never contain API keys, webhook URLs, or registrant data.

### Homepage monitoring

The service can be monitored from a [Homepage](https://gethomepage.dev) dashboard using two of Homepage's built-in mechanisms. No custom Homepage widget is needed.

1. **Up/down indicator, no key needed:** Homepage's `siteMonitor` points at `GET /healthz?strict=true`. That returns `200` when healthy and `503` when data is stale, the last import failed, or the database is unavailable, so Homepage's status dot reflects real data health, not just whether the process is up.
2. **Status widget, `monitor` key:** Homepage's `customapi` widget reads `GET /v1/status`, sending a `monitor`-scoped key in the `X-API-Key` header.

`GET /v1/status` returns flat JSON, with top-level fields only and numbers as numbers, so Homepage `mappings` can use each field directly:

```jsonc
{
  "status": "ok",                         // ok | degraded | down
  "version": "v1.2.0",
  "profile": "medium",
  "last_import_at": "2026-11-06T00:17:42-06:00",
  "last_import_result": "imported",       // imported | duplicate | failed
  "last_coverage_date": "2026-11-05",
  "data_age_days": 1,
  "snapshots": 2513,
  "open_gaps": 0,
  "active_alerts": 0,
  "requests_24h": 18234,
  "p95_ms_24h": 12,
  "errors_503_24h": 0,
  "disk_free_pct": 71.4,
  "archive_store": "s3",                 // filesystem | s3
  "archive_store_ok": true,
  "archive_replica_lag": 0,               // archives not yet copied to replica; null if none
  "disk_days_remaining": 1460
}
```

The response is computed from both databases (read-only) and the data volume's free space, and is cached for 30 seconds, so frequent dashboard polling costs essentially nothing. It never includes registry data, key details, or admin information.

**Example `services.yaml` entry.** It ships in `docs/homepage.md`, and the console's Monitoring page generates it with the real URLs. The key goes in a Homepage environment variable, not in the YAML:

```yaml
- Research:
    - WhoDatJet:
        icon: mdi-airplane
        href: https://whodatjet.<tailnet>.ts.net:8443        # admin console
        description: FAA aircraft registry history
        siteMonitor: https://whodatjet.<tailnet>.ts.net/healthz?strict=true
        widget:
          type: customapi
          url: https://whodatjet.<tailnet>.ts.net/v1/status
          refreshInterval: 60000
          headers:
            X-API-Key: "{{HOMEPAGE_VAR_WHODATJET_KEY}}"
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
```

- **Network reachability:** Homepage must be able to reach the API. In Tailscale mode, the Homepage host (or its container) must be on the tailnet. If the tailnet uses ACLs, allow it to reach port 443 on `whodatjet` only, not the admin port. In LAN mode, Homepage's address must be inside the API source CIDR.
- **Compatibility:** pin the `customapi` field names and `mappings` options shown here to the current Homepage documentation during implementation, and test them against a real Homepage instance in Phase 8.

## Daily ingestion

- `whodatjet-ingest.timer` uses `OnCalendar=*-*-* 00:15:00 America/Chicago` and `*-*-* 06:15:00 America/Chicago`, with `Persistent=true` and `RandomizedDelaySec=10m`. The morning run catches late FAA refreshes. On non-publication days the runs are cheap duplicates.
- Transient network failures retry with bounded exponential backoff. Exhausted retries record a `failed` run and exit nonzero. `OnFailure=whodatjet-alert@%n.service` runs `whodatjet notify`, which posts to the Slack webhook configured in the admin console (see [Slack notifications](#slack-notifications)).
- The downloader URL, expected file set, required columns, and HTTP `User-Agent` are configuration. The User-Agent is a descriptive service string. Phase 0 confirmed the FAA accepts it, and that its CDN answers `HEAD` with `503`, so the downloader uses `GET` only.
- Ingestion is idempotent: rerunning a download/import cannot duplicate snapshots, aircraft, records, or intervals.
- **Historical coverage:** there is no separate backfill. FAA annual databases are not used: Phase 0 found no annual archives on the FAA download page. History before the first daily import comes only from **`DEREG.txt`**, which the first daily import loads as `dereg_record` intervals (certificate issue date to cancel date) reaching back decades. Observed history starts with the first daily download.
- **Chronological order is required.** Archives that predate already-imported snapshots are never applied incrementally. Adding one later (e.g., a historical file found later) triggers `whodatjet rebuild`, which replays every archive in `source_date` order into a new database and swaps it in atomically.
- Retain all unique archives by default. Add an explicit retention command only after backup and storage requirements are agreed.

### Storage estimate

At about 60–70 MB per archive and about 250 publication days per year, unique archives grow by roughly **15–18 GB per year** (verify against live sizes in Phase 0). The database adds a small fraction of that, because only changed rows are stored. Recommended initial data volume: **50 GB** when archives stay on it. When archives live on a separate volume or in object storage, 20 GB for databases, staging, and the local archive cache is enough, and the archive location needs the growth above. The installer displays these estimates before confirming.

### Archive storage

Archives are irreplaceable: the FAA does not publish daily history, so a lost ZIP cannot be downloaded again. The installer therefore offers several storage options plus an optional second copy. All backends sit behind one `ArchiveStore` interface (`put` with verification, `get` to a local file, `exists`, `list`, `verify`), so the importer, `rebuild`, and the verification jobs never know which backend is in use.

| Option | Backend | What it is | When to choose it |
|---|---|---|---|
| **1a. Same data volume** (default) | `filesystem` | Archives in `/var/lib/whodatjet/archives` on the main data mount point | Simplest; one volume to size and back up |
| **1b. Separate Proxmox volume** | `filesystem` | A second mount point (`mp1: <storage>:<size>,mp=/var/lib/whodatjet/archives,backup=1`) on any Proxmox storage: another local disk, ZFS pool, LVM, Ceph RBD, or **NFS/CIFS storage backed by a NAS** (TrueNAS, Synology, etc.) | Keep large, slow-growing archives on cheap or NAS storage while the databases stay on fast local storage |
| **2. S3 (cloud)** | `s3` | Any S3-compatible service: AWS S3, Backblaze B2, Wasabi, Cloudflare R2 | Off-site durability without running anything yourself |
| **3. S3 (self-hosted)** | `s3` | A self-hosted S3-compatible server: **Garage** (lightweight and simple to run, but no bucket versioning or Object Lock, and its per-bucket key permissions are read/write/owner, so a write key can also delete. Use it with a replica.), SeaweedFS, Ceph RGW (if the Proxmox cluster already runs Ceph), or MinIO. Note that MinIO's community edition is now distributed as source only. | Object storage on your own hardware or in another building |

Option 1b keeps NAS access on the Proxmox host. Proxmox mounts the NFS/CIFS share as storage and gives the container an ordinary filesystem volume, so the unprivileged container needs no NFS privileges and has no UID-mapping problems.

**S3 configuration** (options 2 and 3): endpoint URL, region, bucket, key prefix (default `whodatjet/`), addressing style (`path` for Garage, MinIO, and SeaweedFS; `virtual` for AWS), an optional custom CA bundle for self-hosted TLS, and an access key and secret. The credentials are stored in `/etc/whodatjet/archive-store.env` (mode `0640`, root:service group), never in the databases or logs. Recommended bucket settings, which the installer checks and warns about if missing:

- Versioning, or Object Lock in governance/compliance mode, so archives cannot be overwritten or deleted.
- A credential limited to `PutObject`, `GetObject`, `ListBucket`, and `HeadObject` on the prefix, with **no delete permission**. `docs/` ships example AWS IAM and B2 policies. Garage cannot express a no-delete key; the closest is a dedicated key with read+write on the archive bucket only. Ceph RGW and MinIO support versioning and Object Lock if immutability on self-hosted storage is required.
- Server-side encryption where the service offers it.

**Optional replica (recommended):** a second store of any kind, e.g., archives on a local volume with an off-site copy in B2, or the reverse. After each successful import, the new archive is copied to the replica and recorded in `archive_copy`. A replica failure does not fail the import. It raises an **Archive store problem** alert, and `whodatjet archives sync` (also run by the watchdog) retries until the replica catches up.

**Local cache for remote stores:** with an `s3` primary store, the newest archives are also kept in a size-capped local cache (`ARCHIVE_CACHE_GB`, default 5). `rebuild` and re-imports stream older archives from the store one at a time into `staging/`, so a rebuild needs only enough local space for one archive. Rebuilding from S3 downloads the full archive set; on AWS that incurs egress charges, while B2 (within its free allowance), R2, and self-hosted stores make this cheap or free.

**Verification:** `whodatjet archives verify` checks that every archive recorded in `archive_copy` is present and has the recorded size. With `--deep` it also re-hashes the content against `archive_sha256`. A monthly timer runs a deep check on filesystem stores and a presence/size check on S3 stores; deep S3 checks are run manually because they download everything.

**Changing storage later:** `whodatjet archives migrate --to <store-config>` copies every archive to the new store and verifies each copy. It then switches the primary role and marks the old store `retired`, without deleting anything. Removing the old copies is left to the operator. `install.sh --reconfigure-storage <ctid>` on the Proxmox host adds or resizes the `mp1` volume when moving to option 1b.

## LXC deployment

Base target: an unprivileged Debian 13 LXC guest on Proxmox VE 9. A Proxmox storage-backed mount point (`mp0: <storage>:<size>,mp=/var/lib/whodatjet,backup=1`) holds the data, not a host bind mount, which avoids unprivileged UID-mapping problems. `backup=1` is required: without it, vzdump backups silently skip the mount point.

### Interactive installer

`deploy/proxmox/install.sh` runs on the Proxmox host. It checks that it is running as root on a Proxmox VE host (`pveversion`) and prompts for:

1. LXC configuration: container ID, hostname (default `whodatjet`), Debian 13 template and template storage, sizing profile (below), root-disk size, bridge, IP/DHCP, gateway, and DNS.
2. The Proxmox storage pool for the LXC root filesystem.
3. The Proxmox storage pool and size for the persistent data mount point (databases, staging, cache), showing the storage estimate above.
4. Archive storage: option 1a, 1b, 2, or 3 from [Archive storage](#archive-storage). For 1b it lists the node's Proxmox storages, including NFS/CIFS, and asks for the pool and size. For S3 it asks for the endpoint, region, bucket, prefix, addressing style, optional CA bundle, and credentials (secret read without echo). It then runs a connectivity test: write and read back a `.whodatjet-canary` object (no delete needed), and report versioning/Object Lock status. It then offers an optional replica, configured the same way.
5. Exposure mode: `tailscale` (default) or `lan`. For LAN it asks for the API port (default `8000`) and its allowed source CIDR, plus the admin port (default `8001`) and a separate admin source CIDR. It then confirms both Proxmox firewall rules.
6. Tailscale enrollment: a one-time (ideally ephemeral, pre-approved, tagged) auth key read without echo, or interactive enrollment in the guest with `tailscale up`.
7. Optional Slack webhook URL. It can be left blank and set later on the admin console's Notifications page.

### Sizing profiles

Before prompting for resources, the installer shows this table. The operator picks `low`, `medium` (default), `high`, or `custom`:

| Profile | vCPU | Memory | Swap | API workers | `--limit-concurrency` (per worker) | Concurrent in-flight requests | Sustained throughput | Suited to |
|---|---|---|---|---|---|---|---|---|
| `low` | 1 | 2 GB | 512 MB | 2 | 16 | ~10–15 | ~200–400 req/s | A few researchers querying interactively |
| `medium` | 2 | 4 GB | 1 GB | 4 | 24 | ~30–50 | ~600–1,000 req/s | A lab or class; occasional scripted batch lookups |
| `high` | 4 | 8 GB | 1 GB | 8 | 32 | ~80–120 | ~1,500–2,500 req/s | Many concurrent scripts or department-wide use |

How to read the table:

- **Concurrent in-flight requests** is how many lookups can be served at the same moment while keeping p95 latency under about 100 ms. Requests beyond the concurrency limit get a fast `503`, and clients should retry. Interactive users are idle most of the time, so the number of *users* supported is far higher than the number of in-flight requests: dozens on `low`, hundreds on `medium` or `high`.
- **Workers** is about 2 × vCPU. A lookup is mostly CPU work (Python and JSON), with a little waiting on page-cache misses.
- **Memory** covers about 100 MB per API worker, about 100 MB for the single-worker admin console, the importer's peak, which overlaps API traffic at night, and the remainder as page cache for indexes. Memory beyond that mostly speeds up `history` queries and first-hit latency.
- `rebuild` runs on every profile; on `low` it just takes longer.
- These numbers are **pre-implementation estimates**. Phase 8 load testing replaces them with measured values, and the README publishes those.

The installer reads the node's free CPU and memory (`pvesh get /nodes/<node>/status`) and warns if the chosen profile would overcommit the host. `custom` prompts for vCPU and memory, then suggests workers = 2 × vCPU and a concurrency limit. The chosen values are written to `pct` and to `/etc/whodatjet.env` (`UVICORN_WORKERS`, `UVICORN_LIMIT_CONCURRENCY`, `SQLITE_MMAP_SIZE`, `SQLITE_CACHE_SIZE`).

**Resizing later:** Run `install.sh --resize <ctid> --profile <name>` on the Proxmox host. It applies `pct set <id> --cores N --memory M --swap S`, updates the guest's environment file, and restarts the API. No reinstall is needed. Inside the guest, `whodatjet status` shows the active profile, worker count, and recent request-latency and `503` counts, so you can see when to move up a profile.

It creates the unprivileged LXC with `pct`. In `tailscale` mode it passes through the TUN device: `dev0: /dev/net/tun`, or on older hosts `lxc.cgroup2.devices.allow: c 10:200 rwm` plus `lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file`. It then attaches the data mount point, applies the network/firewall configuration, copies in the guest installer, starts the guest, and runs the guest setup. Secrets are passed through the guest's stdin, never on the command line, and are not written to Git, shell history, or generated configuration. On completion it prints the API endpoint, the admin console URL (and the TLS certificate fingerprint in LAN mode), the first API key (shown once), a reminder that the admin console login is `admin`/`admin` and must be changed at first login, the installed version, and the update command.

### Guest setup

The in-container setup:

1. Installs `uv`, a pinned Python 3.12, a dedicated non-login service account (`whodatjet`), the verified application release, and the updater under `/opt/whodatjet/releases/<version>`, with a `current` symlink.
2. Creates `/var/lib/whodatjet/{archives,staging,logs,backups,tls}`, owned by the service account.
3. Installs the API and admin services, the importer and watchdog services/timers, and the alert unit. Writes configuration to `/etc/whodatjet.env` (mode `0640`, root:service group).
4. Initializes `admin.db`, creates the default `admin` account (password `admin`, change forced at first login), saves the Slack webhook if one was given, and issues the first API key.
5. Configures exposure:
   - `tailscale`: installs Tailscale and enrolls the node. It binds the API to `127.0.0.1:8000` and the admin console to `127.0.0.1:8001`, then publishes them with `tailscale serve --bg http://127.0.0.1:8000` (HTTPS 443) and `tailscale serve --bg --https=8443 http://127.0.0.1:8001`. Both get tailnet-only HTTPS with valid certificates, and neither service depends on the `tailscale0` address existing at boot.
   - `lan`: binds the API to `0.0.0.0:<api port>` and the admin console to `0.0.0.0:<admin port>` (HTTPS, self-signed or operator certificate). Separate Proxmox firewall rules restrict each port to its own source CIDR.
6. Runs the first import, then prints backup guidance: back up `whodatjet.db` and `admin.db` (via SQLite's online backup, `whodatjet backup`, which covers both) together with the archives. Proxmox backups of the container are valid when every mount point has `backup=1`. Filesystem archive stores are included in those backups; S3 stores rely on bucket versioning or Object Lock, plus the optional replica.

### One-line application update

The installer places a stable wrapper at `/usr/local/bin/whodatjet-update`. The normal update command is:

```sh
sudo whodatjet-update
```

**Trust model (keyless signing):** releases are built and signed only by the GitHub Actions workflow `.github/workflows/release.yml` in `xDecisionSystems/WhoDatJet`, triggered by pushing a `v*` tag. The workflow publishes a `manifest.json` listing every asset's SHA-256 and signs it with Sigstore keyless signing (`cosign sign-blob`, producing `manifest.json.sigstore.json`). No private key exists for anyone to store, lose, or rotate. The signature proves the manifest was produced by that workflow in that repository from a tag.

Guest setup installs a pinned, hash-checked `cosign` binary. The wrapper verifies each release with:

```sh
cosign verify-blob manifest.json \
  --bundle manifest.json.sigstore.json \
  --certificate-identity-regexp '^https://github.com/xDecisionSystems/WhoDatJet/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

It then downloads `update.sh` for the target release, checks its hash against the verified manifest, and only then executes it. Protect the `v*` tags with a GitHub tag ruleset so that only the owner can create them.

The updater then:

1. Resolves an immutable release tag (latest signed release by default, or `--version <tag>`), never a moving branch.
2. Verifies every release asset against the signed manifest before installing it into a new `releases/<version>` directory.
3. Stops the importer timer and takes SQLite online backups of `whodatjet.db` and `admin.db` into `backups/`.
4. Runs the migrations for both databases transactionally, switches the `current` symlink, restarts the API and admin services, and checks `/healthz` and the admin console's health endpoint locally.
5. If any step fails, switches the symlink back, restores the pre-update database backups (safe, because ingestion was stopped throughout), restarts services, and reports the rollback.
6. Re-enables the timer, prints the installed version and status, and sends an **Update result** Slack notification.

**Later rollback** (`--rollback`) reverts **code only** to the previous release. Each migration declares the minimum code version compatible with the new schema, and `--rollback` refuses to cross an incompatible migration. Restoring an older database backup instead requires `--rollback --restore-db <backup>`. That command warns explicitly, then replays every archive imported since the backup, so no ingested history is lost.

Automatic application updates are out of scope. FAA data ingestion is automated, but code updates are started by an operator.

## Delivery phases

0. **Spike:** download the live FAA archive from the target network. Confirm the URL, User-Agent behavior, file set, headers, encoding, `Last-Modified` behavior, archive size, and serial/N-number edge cases. Build the test fixtures from real (trimmed) rows.
1. Bootstrap the Python project with `uv`, configuration, migration runner, and SQLite connection policy.
2. Implement normalization, the schema, the `ArchiveStore` interface with filesystem and S3 backends (S3 unit-tested against `moto`, plus a Garage container in CI), and the fixture-driven importer, including content-hash de-duplication, the run/snapshot split, single-transaction apply, and failed-import recovery.
3. Implement interval reconciliation and coverage tests: unchanged record, changed attributes, changed N-number, reassignment to a different aircraft, deregistration, re-registration of the same pair (new certificate date), low-confidence identities, DEREG-derived intervals, refusal of out-of-order archives, missed publication days (gap vs. weekend), failed imports that must not close intervals, and `rebuild` matching the incremental result.
4. Implement and test the API: as-of semantics, fallback order, confidence flags, field selection, validation, and API-key authentication (cache refresh, revocation within 30 s, expiry, per-key rate limits, usage flush).
5. Add the downloader with retries, timer and alert units, structured logs, operational CLI (`download`, `import`, `rebuild`, `backup`, `status`, `keys`, `admin`, `notify`), and import metrics.
6. Build the admin console: `admin.db` and its migrations, login/sessions/CSRF/lockout, dashboard, API-key issue/revoke, admin accounts, audit log, Slack webhook configuration with test message, watchdog timer, and alert de-duplication/resolution. Tests cover authentication and CSRF enforcement, forced password change blocking every other route while the default password is in use, key shown once and stored only as a hash, webhook URL validation, alert repeat suppression, the admin routes being absent from the API port, `monitor` keys getting `403` on every endpoint except `/v1/status`, and the `/v1/status` schema staying stable (a contract test, so Homepage mappings don't break across releases).
7. Add the interactive Proxmox installer, guest setup, the GitHub Actions release/Sigstore workflow and tag ruleset, the verified updater and rollback flow, and deployment/runbook documentation. The README begins with the one-line Proxmox-host bootstrap command, using a version-pinned release URL rather than a mutable branch:

   ```sh
   curl -fsSL https://github.com/xDecisionSystems/WhoDatJet/releases/download/vX.Y.Z/install.sh | sudo bash
   ```

   The README also gives a `wget` equivalent and states that the command must run on the Proxmox host, not inside a guest. It lists the release's `install.sh` SHA-256, and documents the safer alternative: download, verify with `cosign verify-blob` (or `gh attestation verify`), inspect, then run. The one-liner trusts GitHub TLS and the pinned URL; Sigstore verification protects every step after installation.
8. Test installation in a disposable Proxmox LXC on both exposure paths, including admin-console reachability on its own port only, a test Slack message, each archive storage option (a second Proxmox volume, an NFS-backed Proxmox storage, a cloud S3 bucket, and a self-hosted Garage instance, confirming the installer warns about Garage's missing versioning/Object Lock) with a replica, a forced replica outage and catch-up, `archives migrate` between backends, `rebuild` streaming from S3, a real Homepage instance showing the status widget and turning its `siteMonitor` red when ingestion is forced stale, and alerts fired by a forced ingest failure and by a stopped API. Include a container restart, a Proxmox backup and restore that keeps the data volume, a successful update, a forced-failure automatic rollback, and a code-only `--rollback`. Load-test each sizing profile with a realistic query mix (e.g., with `oha` or `locust`), including a run during an import. Record concurrent capacity, throughput, p95 latency, and the point where `503`s begin, then replace the estimated profile figures with the measured ones.
9. Run a live non-production import for at least one full week, including a weekend. Verify archive checksums, row counts, duplicate handling, and historical lookups, then deploy.

## Acceptance criteria

- Each unique FAA publication is retained exactly once and verified against its stored `archive_sha256`. Reruns and re-zipped identical publications create no new snapshot.
- Unchanged rows create no new `registration_record` versions or assignment intervals.
- N-number reassignment, deregistration, and later re-registration of the same pair are separate intervals, each with its bracketing snapshots.
- A failed import changes no history and is visible in `/v1/snapshots` and `/healthz?strict=true`.
- A tail-number date query returns the correct interval, with provenance, `basis`, and accurate confidence flags. Weekends never appear as gaps.
- `whodatjet rebuild` from archives produces the same history as incremental imports.
- API queries keep being served during an import, with no errors and no partially imported results. Each sizing profile meets its published concurrency and p95 latency figures in load testing.
- The installer offers `low`/`medium`/`high`/`custom` sizing with the capacity guidance above, warns about host overcommit, and `install.sh --resize` changes the profile without a reinstall.
- Lookups for dates before the first daily import are answered from `dereg_record` intervals (flagged `dereg_record`) or return `404` reporting that the date falls before observed coverage.
- The API, importer, and database survive container restarts and Proxmox backup and restore, because all state is on the backed-up persistent volume.
- The host installer creates an unprivileged LXC with working Tailscale (TUN passthrough) or LAN exposure, after collecting the rootfs and data storage choices.
- `sudo whodatjet-update` installs a Sigstore-verified, version-pinned release or rolls back automatically, without losing database or archive data. A manifest not signed by the repository's release workflow is rejected.
- Every `/v1/*` request without a valid, unexpired, unrevoked API key gets `401`. A revoked key stops working within 30 seconds. Keys are stored only as hashes and are displayed once, at creation.
- A fresh install's admin console accepts `admin`/`admin` and then allows nothing but a password change until a compliant new password is set. `whodatjet admin reset-password` restores that state.
- The admin console is reachable only on its own port (HTTPS in both exposure modes) and requires an admin login. It issues and revokes keys, shows usage, records every admin action in the audit log, and needs no root privileges.
- With a Slack webhook configured, a failed ingest, FAA format change, stale data, unhealthy API, low disk, and update result each produce one Slack message and one "resolved" message, with repeats suppressed in between. Messages contain no secrets or registrant data.
- Archives can be stored on the data volume, a separate Proxmox volume (including NAS-backed NFS/CIFS storage), cloud S3, or self-hosted S3-compatible storage, with an optional replica. Every archive is verified after it is written. A replica outage never fails an import and is caught up automatically. `rebuild` works from any backend, and `archives migrate` moves between backends without data loss.
- A Homepage dashboard shows WhoDatJet's health through `siteMonitor` (`/healthz?strict=true`, no key) and a `customapi` status widget (`/v1/status`, `monitor` key). A `monitor` key cannot read registry data.
- Automated tests cover normalization, importer idempotency, failed-import rollback, interval reconciliation, gap detection, DEREG-derived intervals, temporal API queries, API-key authentication, admin-console security, and notification de-duplication.

## Settled decisions

- Tailscale runs inside the LXC. Enrollment uses a one-time auth key or interactive `tailscale up`, and no key is committed.
- The API port defaults to `8000`. In Tailscale mode it is reached through `tailscale serve` (HTTPS on the tailnet).
- `fields` omitted, empty, or `*` returns all permitted fields, including registrant/address data. Otherwise only the requested allowed fields are returned.
- No annual-database backfill. History before daily collection comes only from DEREG, ingested as `dereg_record` intervals.
- Research database: records are served exactly as originally published, with no retroactive redaction.
- API access requires per-user API keys issued from an admin console on a separate port (`8001` locally; `:8443` via Tailscale). Slack incoming-webhook notifications are configured there. The console starts with `admin`/`admin`, and a password change is required at first login.
- Archive storage: the installer offers the data volume (default), a separate Proxmox volume (including NAS via Proxmox NFS/CIFS storage), cloud S3, or self-hosted S3-compatible storage, plus an optional replica. The databases always stay on block-style local storage.
- Self-hosted object storage: Garage is the preferred, tested self-hosted S3 backend. Its lack of versioning/Object Lock and its write-implies-delete keys are accepted. Mitigations: a dedicated read+write key scoped to the archive bucket, content-addressed keys that the application never overwrites or deletes, and a replica. The installer still shows the warning but does not block. Ceph RGW and MinIO remain supported through the same S3 backend.
- Monitoring: Homepage (gethomepage.dev) via `siteMonitor` on `/healthz?strict=true` and a `customapi` widget on `/v1/status` using a `monitor`-scoped key.
- Single writer: only the importer writes registry data, and no user-facing writes to it will ever be needed. Admin state (keys, accounts, settings, usage counters) lives in a separate small `admin.db`. SQLite in WAL mode is the permanent database choice, with read-only API connections serving many concurrent users.
- Releases are signed keylessly by GitHub Actions and Sigstore. There is no private key to manage.
- Repository: `github.com/xDecisionSystems/WhoDatJet`, public, so the installer and updater fetch releases without credentials.
- Naming: Python package and CLI `whodatjet`; units `whodatjet-api`, `whodatjet-ingest`, `whodatjet-alert@`; service account `whodatjet`; paths `/opt/whodatjet`, `/var/lib/whodatjet`, `/etc/whodatjet.env`; updater `whodatjet-update`; default LXC hostname `whodatjet`.

## Decisions needed before implementation

None outstanding. Phase 0 findings (archive sizes, FAA client behavior) may surface new ones.
