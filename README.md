# WhoDatJet

Historical FAA aircraft-registration lookups. WhoDatJet downloads the FAA Releasable Aircraft Registration Database every day, keeps each unique archive, imports only what changed, and answers *which aircraft held this N-number on this date* through a small API-key-protected HTTP API, with provenance and a confidence level. A separate admin console issues API keys and manages alerts.

## Install on Proxmox

Run on the **Proxmox host** (not inside a guest), as root. It creates an unprivileged Debian 13 LXC, goes inside, downloads and verifies WhoDatJet, installs it and enables its services.

```sh
curl -fsSL https://github.com/xDecisionSystems/WhoDatJet/releases/download/vX.Y.Z/install.sh | sudo bash
# or:  wget -qO- https://github.com/xDecisionSystems/WhoDatJet/releases/download/vX.Y.Z/install.sh | sudo bash
```

Replace `vX.Y.Z` with a release from the [releases page](https://github.com/xDecisionSystems/WhoDatJet/releases) (a version-pinned URL, not a branch, so what you run never changes under you). Until a first release exists, `https://raw.githubusercontent.com/xDecisionSystems/WhoDatJet/main/deploy/proxmox/install.sh` installs from git without signature checks.

It asks for the container settings. Every question has an option, so it can run unattended; for example:

```sh
bash install.sh --yes --ctid 123 --hostname whodatjet --profile medium \
  --root-storage local-lvm --bridge vmbr0 --ip 192.168.1.50/24 --gateway 192.168.1.1 \
  --s3-bucket my-archives --s3-endpoint https://s3.example.com --s3-access-key-id AKID \
  --s3-secret-key-file /root/s3-secret --tailscale-authkey-file /root/ts-key
```

`bash install.sh --help` lists everything. Secrets are never option values: use the `*-file` options. They travel into the container in a 0600 file that is deleted after use.

**Safer alternative: verify before you run.** Download, check the signature and the checksum, read the script, then run it:

```sh
V=vX.Y.Z; R=https://github.com/xDecisionSystems/WhoDatJet/releases/download/$V
curl -fsSLO $R/install.sh -O $R/manifest.json -O $R/manifest.json.sigstore.json
cosign verify-blob --bundle manifest.json.sigstore.json \
  --certificate-identity https://github.com/xDecisionSystems/WhoDatJet/.github/workflows/release.yml@refs/tags/$V \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com manifest.json
grep -A1 '"install.sh"' manifest.json        # compare with: sha256sum install.sh
less install.sh && sudo bash install.sh
```

The one-liner trusts GitHub's TLS and the pinned URL. The Sigstore signature protects everything after that: the guest installer and `whodatjet-update` refuse any release whose signed manifest does not match.

## What you get

| | |
|---|---|
| API | port 8000: `GET /v1/tail-numbers/{n}?date=`, `/history`, `/v1/aircraft/{id}`, `/v1/snapshots`, `/v1/status`, `/healthz`, interactive docs at `/docs` |
| Admin console | port 8001, served under `/admin`: API keys, notifications (Slack), monitoring helper, admins, audit log. First sign-in `admin` / `admin`, with a forced password change |
| Timers | ingest (00:15 and 06:15 Central), backup (08:00), watchdog (every 10 minutes) |
| Archives | S3-compatible bucket (`<year>/<source_date>_<sha256>.zip`, never overwritten) or local disk |
| Access | joins your tailnet; publish the two ports with a reverse proxy (see [docs/deployment.md](docs/deployment.md)) |

## Operate

```sh
sudo whodatjet-update              # verified update; backs up first; rolls back by itself if unhealthy
sudo whodatjet-update --check      # installed vs latest
sudo whodatjet-update --rollback   # back to the previous release (code only)
whodatjet status                   # coverage, last run, keys, storage   (needs /etc/whodatjet*.env loaded)
whodatjet keys create <name>       # issue an API key (shown once); also on the admin console
```

Resize later from the Proxmox host: `bash install.sh --resize <ctid> --profile low|medium|high`.

More: [deployment and operations](docs/deployment.md), [Homepage monitoring](docs/homepage.md), [measured performance](docs/performance.md).

## Develop

```sh
uv sync && uv run pytest -q && uv run ruff check src tests && uv run mypy src
```

Releases are built and signed by `.github/workflows/release.yml` when a `v*` tag is pushed (the tag must equal the version in `pyproject.toml`).
