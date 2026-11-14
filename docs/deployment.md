# Deployment and operations

## Layout in the container
- Code: `/opt/whodatjet/releases/<version>`; `current` and `previous` symlinks; Python 3.12 managed by `uv` in `/opt/whodatjet/python`.
- Settings: `/etc/whodatjet.env`; archive-store credentials: `/etc/whodatjet/archive-store.env` (both mode 0640, root:whodatjet).
- Data: `/var/lib/whodatjet` (`whodatjet.db`, `admin.db`, `staging/`, `backups/`, `logs/`). Mount it as its own volume with `backup=1` so Proxmox backups include it.
- Services: `whodatjet-api`, `whodatjet-admin`; timers `whodatjet-ingest`, `whodatjet-backup`, `whodatjet-watchdog`; `whodatjet-alert@` runs when a unit fails.

## Publishing the web pages
The API (port 8000) and the console (port 8001) listen on all addresses by default (`--api-bind`, `--admin-bind` change that). Put a reverse proxy in front for HTTPS. With NPMplus or nginx, one host name can serve both:

- `/` -> `http://<container>:8000` (the API)
- `/admin` -> `http://<container>:8001` (the console; it is built to live under `/admin`, so forward the path unchanged)

Set `WHODATJET_FORWARDED_ALLOW_IPS` in `/etc/whodatjet.env` to the proxy's address so the console trusts its `X-Forwarded-*` headers (https redirects, real client addresses in the audit log), and `WHODATJET_ADMIN_URL` / `WHODATJET_API_URL` so alert messages and the Monitoring page link correctly. To serve the console at the root of its own host name instead, set `WHODATJET_ADMIN_PREFIX=` (empty).

## Alerts
Sign in to the console, **Notifications**: paste a Slack incoming webhook (must be `https://hooks.slack.com/services/...`), choose events and the repeat interval, press **Send test message**. A condition alerts once, repeats at most every repeat interval while it lasts, and sends one "resolved" message when it clears.

## Updates and rollback
`sudo whodatjet-update` downloads the release manifest and its Sigstore signature, verifies them with the pinned `cosign` (certificate identity = this repository's release workflow at that exact tag), checks the tarball against the signed manifest, backs up the databases, switches `current`, runs migrations, restarts the services and waits for `/healthz`. If anything after the switch fails it returns to the previous release by itself and posts the result to Slack. `--rollback` switches back by hand. Migrations only add to the schema, so older code keeps working with a newer database.

## Storage outages
If the archive store is unreachable during ingest, the downloaded archive stays in `staging/pending-*.zip` and is uploaded by the next run, with its original source date. The API keeps answering from the local database. Until the upload succeeds the staged file is the only copy, so do not wipe `staging/`.

## Recovery
- Forgotten admin password: `whodatjet admin reset-password <user>` inside the container (password becomes `admin`, change forced).
- Damaged `whodatjet.db`: `whodatjet rebuild` replays every archive in order into a fresh database (the old one is kept as a backup).
- Backups: `whodatjet backup --keep 14` (daily timer) writes consistent copies of both databases to `/var/lib/whodatjet/backups`.
