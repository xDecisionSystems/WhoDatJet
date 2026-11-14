#!/usr/bin/env bash
# WhoDatJet guest installer. Run as root inside a Debian 13 (trixie) LXC or VM:
#
#   curl -fsSL https://raw.githubusercontent.com/xDecisionSystems/WhoDatJet/main/deploy/install.sh | bash
#
# Re-running upgrades the code to WHODATJET_REF and keeps /etc/whodatjet*, the data and the API keys.
#
# Under the Proxmox installer, settings arrive in WHODATJET_ANSWERS_FILE instead.
# Optional environment (anything unset is asked for on the terminal):
#   WHODATJET_REF        git branch or tag to install            (default: main)
#   WHODATJET_PROFILE    low | medium | high                      (default: medium)
#   WHODATJET_S3_ENDPOINT_URL, WHODATJET_S3_REGION, WHODATJET_S3_BUCKET, WHODATJET_S3_PREFIX,
#   WHODATJET_S3_ADDRESSING_STYLE, WHODATJET_S3_ACCESS_KEY_ID, WHODATJET_S3_SECRET_ACCESS_KEY
#                        archive store; leave the bucket empty to keep archives on local disk
#   WHODATJET_SLACK_WEBHOOK_URL   optional alert webhook
#   WHODATJET_TAILSCALE_AUTHKEY   optional; otherwise run `tailscale up` yourself afterwards
#   WHODATJET_API_BIND (default 0.0.0.0), WHODATJET_ADMIN_BIND (default 127.0.0.1)   listen addresses
#   WHODATJET_ADMIN_PREFIX (default /admin; empty to serve the console at the root of its own host name)
#   WHODATJET_FIRST_INGEST=no     skip the first download/import
set -euo pipefail
# uv and cosign live in /usr/local/bin, which minimal environments (pct exec, cron) may not have on PATH.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

# Settings handed over by the Proxmox installer (a 0600 file of VAR=value lines); read once, then deleted.
if [ -n "${WHODATJET_ANSWERS_FILE:-}" ] && [ -r "$WHODATJET_ANSWERS_FILE" ]; then
  set -a; . "$WHODATJET_ANSWERS_FILE"; set +a
  rm -f "$WHODATJET_ANSWERS_FILE"
  WHODATJET_NONINTERACTIVE=1
fi

# A released installer carries its own version (stamped at build time); WHODATJET_VERSION overrides it.
# With a version the code comes from the signed GitHub release; without one it is cloned from git (WHODATJET_REF).
VERSION="${WHODATJET_VERSION:-@VERSION@}"
case "$VERSION" in @*) VERSION="" ;; esac
REPO="${WHODATJET_REPO:-xDecisionSystems/WhoDatJet}"
COSIGN_VERSION=v3.1.4
cosign_sha() { case "$1" in
  amd64) echo 424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa ;;
  arm64) echo ade83b7aac62e131cacfe96d958948b2150cc9efec46a570a2649b8e27dedad2 ;;
esac; }

REPO_URL="${WHODATJET_REPO_URL:-https://github.com/xDecisionSystems/WhoDatJet.git}"
REF="${WHODATJET_REF:-main}"
PROFILE="${WHODATJET_PROFILE:-medium}"
APP=/opt/whodatjet
DATA=/var/lib/whodatjet
ENV_FILE=/etc/whodatjet.env
STORE_ENV=/etc/whodatjet/archive-store.env

die() { echo "error: $*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "run as root"
command -v apt-get >/dev/null || die "this installer supports Debian/Ubuntu"

case "$PROFILE" in
  low)    WORKERS=2; LIMIT=16 ;;
  medium) WORKERS=4; LIMIT=24 ;;
  high)   WORKERS=8; LIMIT=32 ;;
  *) die "WHODATJET_PROFILE must be low, medium or high" ;;
esac

# ask VAR "prompt" [default] [secret]; uses the terminal even when the script itself is piped in.
ask() {
  local var="$1" prompt="$2" def="${3:-}" secret="${4:-}" val=""
  if [ -n "${!var:-}" ]; then return; fi
  if [ -z "${WHODATJET_NONINTERACTIVE:-}" ] && { : </dev/tty; } 2>/dev/null; then
    if [ -n "$secret" ]; then read -r -s -p "$prompt: " val </dev/tty; echo >/dev/tty
    else read -r -p "$prompt${def:+ [$def]}: " val </dev/tty; fi
  fi
  printf -v "$var" '%s' "${val:-$def}"
}

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates git sqlite3 python3 >/dev/null

if ! command -v uv >/dev/null && [ ! -x /usr/local/bin/uv ]; then
  say "Installing uv"
  curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
UV="$(command -v uv || echo /usr/local/bin/uv)"

say "Service account and directories"
id whodatjet >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin whodatjet
install -d -o whodatjet -g whodatjet -m 0750 "$DATA" "$DATA"/{archives,staging,logs,backups}
install -d -m 0755 "$APP" "$APP/releases" /etc/whodatjet

say "Tailscale"
if ! command -v tailscale >/dev/null; then
  echo "Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
fi
systemctl enable --now tailscaled >/dev/null 2>&1 || true
if [ -n "${WHODATJET_TAILSCALE_AUTHKEY:-}" ] && ! tailscale status >/dev/null 2>&1; then
  tailscale up --authkey "$WHODATJET_TAILSCALE_AUTHKEY" --hostname "${WHODATJET_TS_HOSTNAME:-$(hostname)}" \
    --accept-dns=false
fi
if tailscale status >/dev/null 2>&1; then
  echo "Joined the tailnet as $(tailscale status --self --json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || hostname)"
else
  echo "Tailscale is installed but not connected. Run: tailscale up --hostname ${WHODATJET_TS_HOSTNAME:-$(hostname)} --accept-dns=false"
fi
# The web pages are published through NPMplus (HTTPS on the lab's wildcard certificate); `tailscale serve` is not used.

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# cosign (pinned version, hash-checked) verifies release signatures here and in `whodatjet-update`.
arch="$(dpkg --print-architecture)"
want="$(cosign_sha "$arch")"; [ -n "$want" ] || die "unsupported architecture: $arch"
if [ ! -x /usr/local/bin/cosign ] || [ "$(sha256sum /usr/local/bin/cosign | cut -d' ' -f1)" != "$want" ]; then
  say "Installing cosign $COSIGN_VERSION"
  curl -fsSL -o "$tmp/cosign" "https://github.com/sigstore/cosign/releases/download/$COSIGN_VERSION/cosign-linux-$arch"
  [ "$(sha256sum "$tmp/cosign" | cut -d' ' -f1)" = "$want" ] || die "the cosign download does not match its pinned checksum"
  install -m 0755 "$tmp/cosign" /usr/local/bin/cosign
fi

if [ -n "$VERSION" ]; then
  say "Fetching WhoDatJet $VERSION (signed release)"
  base="https://github.com/$REPO/releases/download/$VERSION"
  curl -fsSL -o "$tmp/manifest.json" "$base/manifest.json"
  curl -fsSL -o "$tmp/manifest.json.sigstore.json" "$base/manifest.json.sigstore.json"
  /usr/local/bin/cosign verify-blob --bundle "$tmp/manifest.json.sigstore.json" \
    --certificate-identity "https://github.com/$REPO/.github/workflows/release.yml@refs/tags/$VERSION" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com "$tmp/manifest.json" >/dev/null 2>"$tmp/cosign.err" \
    || { cat "$tmp/cosign.err" >&2; die "could not verify the release signature (see the message above); nothing was installed"; }
  tarball="whodatjet-$VERSION.tar.gz"
  want_sha="$(python3 - "$tmp/manifest.json" "$tarball" "$VERSION" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
if m.get("version") != sys.argv[3]:
    sys.exit("manifest is for a different version")
print(m["assets"][sys.argv[2]]["sha256"])
PY
)" || die "the signed manifest does not describe $VERSION"
  curl -fsSL -o "$tmp/$tarball" "$base/$tarball"
  [ "$(sha256sum "$tmp/$tarball" | cut -d' ' -f1)" = "$want_sha" ] || die "$tarball does not match the signed manifest"
  version="$VERSION"
  rel="$APP/releases/$version"
  if [ ! -f "$rel/.installed" ]; then
    rm -rf "$rel"
    mkdir -p "$tmp/x" && tar -xzf "$tmp/$tarball" -C "$tmp/x"
    mv "$tmp/x/whodatjet-$VERSION" "$rel"
    (cd "$rel" && UV_PYTHON_INSTALL_DIR="$APP/python" "$UV" sync --frozen --no-dev --python 3.12 >/dev/null)
    touch "$rel/.installed"
  fi
else
  say "Fetching WhoDatJet ($REF, from git, not a signed release)"
  git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$tmp/src"
  version="$(git -C "$tmp/src" rev-parse --short HEAD)"
  rel="$APP/releases/$version"
  if [ ! -f "$rel/.installed" ]; then
    rm -rf "$rel"
    mv "$tmp/src" "$rel"
    rm -rf "$rel/.git"
    (cd "$rel" && UV_PYTHON_INSTALL_DIR="$APP/python" "$UV" sync --frozen --no-dev --python 3.12 >/dev/null)
    touch "$rel/.installed"
  fi
fi
install -m 0755 "$rel/deploy/whodatjet-update" /usr/local/bin/whodatjet-update
# The service account must be able to run the interpreter and the code.
chmod -R a+rX "$rel" "$APP/python" 2>/dev/null || true
ln -sfn "$rel" "$APP/current"

if [ ! -f "$ENV_FILE" ]; then
  say "Writing $ENV_FILE ($PROFILE profile)"
  sed -e "s/^WHODATJET_PROFILE=.*/WHODATJET_PROFILE=$PROFILE/" \
      -e "s/^UVICORN_WORKERS=.*/UVICORN_WORKERS=$WORKERS/" \
      -e "s/^UVICORN_LIMIT_CONCURRENCY=.*/UVICORN_LIMIT_CONCURRENCY=$LIMIT/" \
      "$rel/deploy/systemd/whodatjet.env.example" > "$ENV_FILE"
  ask WHODATJET_SLACK_WEBHOOK_URL "Slack webhook URL for alerts (blank to skip)" "" secret
  [ -z "${WHODATJET_SLACK_WEBHOOK_URL:-}" ] || echo "WHODATJET_SLACK_WEBHOOK_URL=$WHODATJET_SLACK_WEBHOOK_URL" >> "$ENV_FILE"
  # Reverse-proxy friendly: the API listens on the container's network address; the admin console stays on
  # loopback unless WHODATJET_ADMIN_BIND is set (e.g. 0.0.0.0 when NPMplus publishes it).
  # The console is served under /admin by default so one NPMplus host can publish API (/) and console (/admin).
  sed -i -e "s#^WHODATJET_ADMIN_PREFIX=.*#WHODATJET_ADMIN_PREFIX=${WHODATJET_ADMIN_PREFIX-/admin}#" \
         -e "s/^WHODATJET_API_HOST=.*/WHODATJET_API_HOST=${WHODATJET_API_BIND:-0.0.0.0}/" \
         -e "s/^WHODATJET_ADMIN_HOST=.*/WHODATJET_ADMIN_HOST=${WHODATJET_ADMIN_BIND:-127.0.0.1}/" "$ENV_FILE"
  chown root:whodatjet "$ENV_FILE"; chmod 0640 "$ENV_FILE"
else
  say "$ENV_FILE exists; keeping it"
fi

if [ ! -f "$STORE_ENV" ]; then
  say "Archive storage (S3-compatible; leave the bucket empty for local disk)"
  ask WHODATJET_S3_BUCKET "S3 bucket"
  if [ -n "${WHODATJET_S3_BUCKET:-}" ]; then
    ask WHODATJET_S3_ENDPOINT_URL "S3 endpoint URL (blank for AWS)"
    ask WHODATJET_S3_REGION "S3 region" "garage"
    ask WHODATJET_S3_PREFIX "Key prefix inside the bucket (usually blank)"
    ask WHODATJET_S3_ADDRESSING_STYLE "Addressing style (path|virtual)" "path"
    ask WHODATJET_S3_ACCESS_KEY_ID "S3 access key id"
    ask WHODATJET_S3_SECRET_ACCESS_KEY "S3 secret key" "" secret
    [ -n "${WHODATJET_S3_ACCESS_KEY_ID:-}" ] && [ -n "${WHODATJET_S3_SECRET_ACCESS_KEY:-}" ] \
      || die "S3 access key id and secret are required"
    umask 0137
    {
      for v in WHODATJET_S3_ENDPOINT_URL WHODATJET_S3_REGION WHODATJET_S3_BUCKET WHODATJET_S3_PREFIX \
               WHODATJET_S3_ADDRESSING_STYLE WHODATJET_S3_ACCESS_KEY_ID WHODATJET_S3_SECRET_ACCESS_KEY; do
        printf "%s='%s'\n" "$v" "${!v:-}"
      done
    } > "$STORE_ENV"
    chown root:whodatjet "$STORE_ENV"; chmod 0640 "$STORE_ENV"
    # Connectivity check: write and read back a canary object (no delete permission needed).
    # Quick connectivity check using the application's own configuration.
    (set -a; . "$ENV_FILE"; . "$STORE_ENV"; set +a
     "$APP/current/.venv/bin/python" - <<'PY'
import os, boto3
from botocore.config import Config
c = boto3.client("s3", endpoint_url=os.environ.get("WHODATJET_S3_ENDPOINT_URL") or None,
                 region_name=os.environ.get("WHODATJET_S3_REGION") or None,
                 aws_access_key_id=os.environ["WHODATJET_S3_ACCESS_KEY_ID"],
                 aws_secret_access_key=os.environ["WHODATJET_S3_SECRET_ACCESS_KEY"],
                 config=Config(s3={"addressing_style": os.environ.get("WHODATJET_S3_ADDRESSING_STYLE") or "path"}))
key = os.environ.get("WHODATJET_S3_PREFIX", "") + ".whodatjet-canary"
c.put_object(Bucket=os.environ["WHODATJET_S3_BUCKET"], Key=key, Body=b"whodatjet canary")
assert c.get_object(Bucket=os.environ["WHODATJET_S3_BUCKET"], Key=key)["Body"].read() == b"whodatjet canary"
PY
    ) || die "could not write/read the canary object in the archive store (see $STORE_ENV)"
  else
    sed -i 's/^WHODATJET_ARCHIVE_BACKEND=.*/WHODATJET_ARCHIVE_BACKEND=filesystem/' "$ENV_FILE"
  fi
else
  say "$STORE_ENV exists; keeping it"
fi

say "Installing systemd units"
for u in whodatjet-api.service whodatjet-admin.service whodatjet-ingest.service whodatjet-ingest.timer \
         whodatjet-backup.service whodatjet-backup.timer whodatjet-watchdog.service whodatjet-watchdog.timer \
         'whodatjet-alert@.service'; do
  install -m 0644 "$rel/deploy/systemd/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload

say "Initialising databases"
run() {  # run a command as the service account with the service environment loaded
  runuser -u whodatjet -- bash -c 'set -a; . /etc/whodatjet.env
    [ -r /etc/whodatjet/archive-store.env ] && . /etc/whodatjet/archive-store.env; set +a; exec "$@"' _ "$@"
}
run "$APP/current/.venv/bin/whodatjet" migrate
run "$APP/current/.venv/bin/whodatjet" admin init

systemctl enable --now whodatjet-api.service whodatjet-admin.service whodatjet-ingest.timer \
  whodatjet-backup.timer whodatjet-watchdog.timer

if [ "${WHODATJET_FIRST_INGEST:-yes}" != "no" ]; then
  say "First download and import (can take several minutes)"
  systemctl start whodatjet-ingest.service || echo "first ingest failed: see journalctl -u whodatjet-ingest" >&2
fi

if [ ! -e "$DATA/.first-key-issued" ]; then
  say "First API key (shown once; store it now)"
  run "$APP/current/.venv/bin/whodatjet" keys create first-key --owner installer && touch "$DATA/.first-key-issued"
fi

say "Done"
systemctl --no-pager --lines=0 status whodatjet-api.service | head -4 || true
echo "Check:    $APP/current/.venv/bin/whodatjet status   (as root, with: set -a; . /etc/whodatjet.env; . $STORE_ENV; set +a)"
echo "Pages:    the API listens on port 8000 and the admin console on 8001 (loopback unless WHODATJET_ADMIN_BIND is set);"
echo "          publish them with NPMplus proxy hosts (forward to http://<this-ip>:8000 and :8001). Admin sign-in: admin / admin, change it at first login."
echo "Logs:     journalctl -u whodatjet-api -u whodatjet-ingest"
echo "Update:   sudo whodatjet-update   (verifies the signed release, backs up, rolls back by itself if unhealthy; --check, --rollback)"
