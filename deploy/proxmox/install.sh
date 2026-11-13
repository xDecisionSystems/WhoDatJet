#!/usr/bin/env bash
# WhoDatJet Proxmox installer. Run as root ON THE PROXMOX HOST (not inside a guest):
#
#   curl -fsSL https://raw.githubusercontent.com/xDecisionSystems/WhoDatJet/main/deploy/proxmox/install.sh | bash
#
# It asks for the container settings, creates an unprivileged Debian 13 LXC, starts it, then goes inside and
# runs the guest installer (deploy/install.sh), which downloads WhoDatJet, installs it and enables its services.
#
# Everything can be given as an option, so no prompts are needed (add --yes so unanswered questions take their
# defaults instead of asking). Example, fully non-interactive:
#
#   bash install.sh --yes --ctid 123 --hostname whodatjet --profile medium \
#       --root-storage md1200-0 --data-storage md1200-0 --bridge vmbr2 --vlan 20 \
#       --ip 10.10.20.123/24 --gateway 10.10.20.1 --dns 10.10.20.1 \
#       --s3-bucket faa-aircraft-database --s3-endpoint https://s3.example.com \
#       --s3-access-key-id AKID --s3-secret-key-file /root/s3-secret \
#       --tailscale-authkey-file /root/ts-key
#
# Run with --help for the full option list. Secrets are NOT accepted as option values (they would show up in the
# process list): use the *-file options or the environment variables named in --help.
# They travel into the container in a 0600 file which the guest installer reads and deletes.
set -euo pipefail

RAW="${WHODATJET_RAW_URL:-https://raw.githubusercontent.com/xDecisionSystems/WhoDatJet}"
REF="${WHODATJET_REF:-main}"
YES=0; DRY=0; RESIZE=""; PROFILE_ARG=""

die() { echo "error: $*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then printf '[dry-run]'; printf ' %q' "$@"; echo; else "$@"; fi; }

usage() {
  cat <<'USAGE'
Usage: install.sh [options]            (run as root on the Proxmox host)

Modes:
  --yes, --non-interactive   never prompt; unanswered questions take their defaults
  --dry-run                  print what would be done, change nothing
  --resize CTID --profile P  change CPU/RAM/API workers of an existing container, then exit

Container (env var in brackets):
  --ctid N               container ID                                  [WDJ_CTID]       default: next free
  --hostname NAME        hostname (also the tailnet name)              [WDJ_HOSTNAME]   default: whodatjet
  --profile P            low | medium | high                           [WDJ_PROFILE]    default: medium
  --root-storage S       storage for the root filesystem               [WDJ_ROOT_STORAGE] default: local-lvm
  --root-size GB         root disk size                                [WDJ_ROOT_GB]    default: 8
  --data-storage S       storage for the data volume                   [WDJ_DATA_STORAGE] default: root storage
  --data-size GB         data volume size                              [WDJ_DATA_GB]    default: 20
  --template-storage S   storage holding container templates           [WDJ_TEMPLATE_STORAGE] default: local
  --bridge B             network bridge                                [WDJ_BRIDGE]     default: vmbr0
  --vlan TAG             VLAN tag                                      [WDJ_VLAN]       default: none
  --ip CIDR|dhcp         IPv4 address                                  [WDJ_IP]         default: dhcp
  --gateway IP           gateway (required with a static --ip)         [WDJ_GW]
  --dns IP               DNS server                                    [WDJ_DNS]        default: inherit

Archive storage (leave --s3-bucket empty to keep archives on local disk):
  --s3-bucket NAME                                                     [WHODATJET_S3_BUCKET]
  --s3-endpoint URL      blank for AWS                                 [WHODATJET_S3_ENDPOINT_URL]
  --s3-region R          default: garage                               [WHODATJET_S3_REGION]
  --s3-prefix P          key prefix inside the bucket (usually blank)  [WHODATJET_S3_PREFIX]
  --s3-addressing S      path | virtual (default: path)                [WHODATJET_S3_ADDRESSING_STYLE]
  --s3-access-key-id ID                                                [WHODATJET_S3_ACCESS_KEY_ID]
  --s3-secret-key-file F file holding the secret key                   [WHODATJET_S3_SECRET_ACCESS_KEY]

Access and alerts:
  --tailscale-authkey-file F   Tailscale auth key (else enroll later)  [WHODATJET_TAILSCALE_AUTHKEY]
  --slack-webhook-file F       Slack incoming-webhook URL (optional)   [WHODATJET_SLACK_WEBHOOK_URL]
  --ref REF              git branch/tag of WhoDatJet to install        [WHODATJET_REF]  default: main
USAGE
}

need() { [ $# -ge 2 ] && [ -n "$2" ] || die "option $1 needs a value"; }
secret_from_file() {  # VAR option file
  [ -r "$3" ] || die "$2: cannot read $3"
  printf -v "$1" '%s' "$(tr -d '\r\n' < "$3")"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y|--non-interactive) YES=1 ;;
    --dry-run) DRY=1 ;;
    --resize) need "$@"; RESIZE="$2"; shift ;;
    --profile) need "$@"; PROFILE_ARG="$2"; WDJ_PROFILE="$2"; shift ;;
    --ctid) need "$@"; WDJ_CTID="$2"; shift ;;
    --hostname) need "$@"; WDJ_HOSTNAME="$2"; shift ;;
    --root-storage) need "$@"; WDJ_ROOT_STORAGE="$2"; shift ;;
    --root-size) need "$@"; WDJ_ROOT_GB="$2"; shift ;;
    --data-storage) need "$@"; WDJ_DATA_STORAGE="$2"; shift ;;
    --data-size) need "$@"; WDJ_DATA_GB="$2"; shift ;;
    --template-storage) need "$@"; WDJ_TEMPLATE_STORAGE="$2"; shift ;;
    --bridge) need "$@"; WDJ_BRIDGE="$2"; shift ;;
    --vlan) need "$@"; WDJ_VLAN="$2"; shift ;;
    --ip) need "$@"; WDJ_IP="$2"; shift ;;
    --gateway) need "$@"; WDJ_GW="$2"; shift ;;
    --dns) need "$@"; WDJ_DNS="$2"; shift ;;
    --s3-bucket) need "$@"; WHODATJET_S3_BUCKET="$2"; shift ;;
    --s3-endpoint) need "$@"; WHODATJET_S3_ENDPOINT_URL="$2"; shift ;;
    --s3-region) need "$@"; WHODATJET_S3_REGION="$2"; shift ;;
    --s3-prefix) need "$@"; WHODATJET_S3_PREFIX="$2"; shift ;;
    --s3-addressing) need "$@"; WHODATJET_S3_ADDRESSING_STYLE="$2"; shift ;;
    --s3-access-key-id) need "$@"; WHODATJET_S3_ACCESS_KEY_ID="$2"; shift ;;
    --s3-secret-key-file) need "$@"; secret_from_file WHODATJET_S3_SECRET_ACCESS_KEY "$1" "$2"; shift ;;
    --tailscale-authkey-file) need "$@"; secret_from_file WHODATJET_TAILSCALE_AUTHKEY "$1" "$2"; shift ;;
    --slack-webhook-file) need "$@"; secret_from_file WHODATJET_SLACK_WEBHOOK_URL "$1" "$2"; shift ;;
    --ref) need "$@"; REF="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done


# ask VAR "prompt" default [secret]  -- preset variable wins, then --yes default, then the terminal.
ask() {
  local var="$1" prompt="$2" def="${3:-}" secret="${4:-}" val=""
  if [ -n "${!var:-}" ]; then return; fi
  if [ "$YES" = 0 ] && [ -r /dev/tty ]; then
    if [ -n "$secret" ]; then read -r -s -p "$prompt: " val </dev/tty; echo >/dev/tty
    else read -r -p "$prompt${def:+ [$def]}: " val </dev/tty; fi
  fi
  printf -v "$var" '%s' "${val:-$def}"
}

[ "$DRY" = 1 ] || [ "$(id -u)" -eq 0 ] || die "run as root"
if [ "$DRY" = 0 ]; then
  command -v pveversion >/dev/null && command -v pct >/dev/null \
    || die "this must run on a Proxmox VE host (pveversion/pct not found); do not run it inside a guest"
fi

profile_values() {  # sets CORES MEM SWAP WORKERS LIMIT
  case "$1" in
    low)    CORES=1; MEM=2048; SWAP=512;  WORKERS=2; LIMIT=16 ;;
    medium) CORES=2; MEM=4096; SWAP=1024; WORKERS=4; LIMIT=24 ;;
    high)   CORES=4; MEM=8192; SWAP=1024; WORKERS=8; LIMIT=32 ;;
    *) die "profile must be low, medium or high" ;;
  esac
}

warn_overcommit() {  # CORES MEM
  command -v pvesh >/dev/null || return 0
  pvesh get "/nodes/$(hostname)/status" --output-format json 2>/dev/null | python3 -c '
import json,sys
cores, mem = int(sys.argv[1]), int(sys.argv[2])
try:
    s = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
free_mb = (s["memory"]["total"] - s["memory"]["used"]) // 2**20
if mem > free_mb:
    print(f"WARNING: {mem} MB requested but only {free_mb} MB of host memory is free")
cpus = s.get("cpuinfo", {}).get("cpus", cores)
if cores > cpus:
    print(f"WARNING: {cores} cores requested but the host has {cpus}")
' "$1" "$2" || true
}

# ----------------------------------------------------------------------------------------------- resize
if [ -n "$RESIZE" ]; then
  [ -n "$PROFILE_ARG" ] || die "--resize needs --profile low|medium|high"
  profile_values "$PROFILE_ARG"
  [ "$DRY" = 1 ] || pct status "$RESIZE" >/dev/null 2>&1 || die "no container $RESIZE"
  say "Resizing CT $RESIZE to the $PROFILE_ARG profile ($CORES vCPU, $MEM MB, $WORKERS workers)"
  warn_overcommit "$CORES" "$MEM"
  run pct set "$RESIZE" --cores "$CORES" --memory "$MEM" --swap "$SWAP"
  run pct exec "$RESIZE" -- sed -i \
    -e "s/^WHODATJET_PROFILE=.*/WHODATJET_PROFILE=$PROFILE_ARG/" \
    -e "s/^UVICORN_WORKERS=.*/UVICORN_WORKERS=$WORKERS/" \
    -e "s/^UVICORN_LIMIT_CONCURRENCY=.*/UVICORN_LIMIT_CONCURRENCY=$LIMIT/" /etc/whodatjet.env
  run pct exec "$RESIZE" -- systemctl restart whodatjet-api
  say "Done"
  exit 0
fi

# ----------------------------------------------------------------------------------------------- questions
say "WhoDatJet container settings (press Enter to accept the [default])"
NEXT="$( [ "$DRY" = 1 ] && echo 123 || pvesh get /cluster/nextid 2>/dev/null || echo 200 )"
ask WDJ_CTID "Container ID" "$NEXT"
ask WDJ_HOSTNAME "Hostname" "whodatjet"
ask WDJ_PROFILE "Sizing profile (low = 1 vCPU/2 GB, medium = 2/4 GB, high = 4/8 GB)" "medium"
ask WDJ_ROOT_STORAGE "Storage for the root filesystem" "local-lvm"
ask WDJ_ROOT_GB "Root disk size (GB)" "8"
ask WDJ_DATA_STORAGE "Storage for the data volume (databases, staging, backups)" "$WDJ_ROOT_STORAGE"
ask WDJ_DATA_GB "Data volume size (GB)" "20"
ask WDJ_TEMPLATE_STORAGE "Storage that holds container templates" "local"
ask WDJ_BRIDGE "Network bridge" "vmbr0"
ask WDJ_VLAN "VLAN tag (blank for none)" ""
ask WDJ_IP "IPv4 address as CIDR, or 'dhcp'" "dhcp"
if [ "$WDJ_IP" != "dhcp" ]; then ask WDJ_GW "Gateway" ""; [ -n "${WDJ_GW:-}" ] || die "a gateway is required for a static address"; fi
ask WDJ_DNS "DNS server (blank to inherit from the host)" ""
profile_values "$WDJ_PROFILE"
[[ "$WDJ_CTID" =~ ^[0-9]+$ ]] || die "container ID must be a number"
[[ "$WDJ_HOSTNAME" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || die "invalid hostname"
if [ "$DRY" = 0 ]; then
  pct status "$WDJ_CTID" >/dev/null 2>&1 && die "container $WDJ_CTID already exists"
  pvesm status 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$WDJ_ROOT_STORAGE" || die "no storage named $WDJ_ROOT_STORAGE"
  pvesm status 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$WDJ_DATA_STORAGE" || die "no storage named $WDJ_DATA_STORAGE"
  if [ "$WDJ_IP" != "dhcp" ] && ping -c1 -W1 "${WDJ_IP%/*}" >/dev/null 2>&1; then
    die "${WDJ_IP%/*} already answers ping; choose another address"
  fi
fi
warn_overcommit "$CORES" "$MEM"

say "Archive storage (S3-compatible; leave the bucket empty to keep archives on local disk)"
ask WHODATJET_S3_BUCKET "S3 bucket" ""
if [ -n "${WHODATJET_S3_BUCKET:-}" ]; then
  ask WHODATJET_S3_ENDPOINT_URL "S3 endpoint URL (blank for AWS)" ""
  ask WHODATJET_S3_REGION "S3 region" "garage"
  ask WHODATJET_S3_PREFIX "Key prefix inside the bucket (usually blank)" ""
  ask WHODATJET_S3_ADDRESSING_STYLE "Addressing style (path|virtual)" "path"
  ask WHODATJET_S3_ACCESS_KEY_ID "S3 access key id" ""
  ask WHODATJET_S3_SECRET_ACCESS_KEY "S3 secret key (input hidden)" "" secret
  [ -n "${WHODATJET_S3_ACCESS_KEY_ID:-}" ] && [ -n "${WHODATJET_S3_SECRET_ACCESS_KEY:-}" ] \
    || die "the S3 access key id and secret are required"
fi
say "Access: WhoDatJet is published on your tailnet (API https://<host>/, admin https://<host>:8443/)"
ask WHODATJET_TAILSCALE_AUTHKEY "Tailscale auth key (blank to enroll by hand later; input hidden)" "" secret
ask WHODATJET_SLACK_WEBHOOK_URL "Slack webhook URL for alerts (blank to set it later in the console)" "" secret

# ----------------------------------------------------------------------------------------------- create
say "Container template"
TPL=""
if [ "$DRY" = 0 ]; then
  TPL="$(pveam list "$WDJ_TEMPLATE_STORAGE" 2>/dev/null | awk '{print $1}' | grep 'debian-13-standard' | sort | tail -1 || true)"
  if [ -z "$TPL" ]; then
    echo "Downloading the Debian 13 template..."
    pveam update >/dev/null
    NAME="$(pveam available --section system | awk '{print $2}' | grep '^debian-13-standard' | sort | tail -1)"
    [ -n "$NAME" ] || die "no debian-13-standard template is available"
    pveam download "$WDJ_TEMPLATE_STORAGE" "$NAME" >/dev/null
    TPL="$WDJ_TEMPLATE_STORAGE:vztmpl/$NAME"
  fi
else
  TPL="$WDJ_TEMPLATE_STORAGE:vztmpl/debian-13-standard_<latest>_amd64.tar.zst"
fi
echo "using $TPL"

NET="name=eth0,bridge=$WDJ_BRIDGE,firewall=1"
[ -z "${WDJ_VLAN:-}" ] || NET="$NET,tag=$WDJ_VLAN"
if [ "$WDJ_IP" = "dhcp" ]; then NET="$NET,ip=dhcp"; else NET="$NET,ip=$WDJ_IP,gw=$WDJ_GW"; fi
ARGS=(--hostname "$WDJ_HOSTNAME" --cores "$CORES" --memory "$MEM" --swap "$SWAP"
      --rootfs "$WDJ_ROOT_STORAGE:$WDJ_ROOT_GB" --unprivileged 1 --features nesting=1 --ostype debian --onboot 1
      --tags whodatjet --net0 "$NET" --mp0 "$WDJ_DATA_STORAGE:$WDJ_DATA_GB,mp=/var/lib/whodatjet,backup=1")
[ -z "${WDJ_DNS:-}" ] || ARGS+=(--nameserver "$WDJ_DNS")

say "Creating container $WDJ_CTID ($WDJ_HOSTNAME)"
run pct create "$WDJ_CTID" "$TPL" "${ARGS[@]}"
CONF="/etc/pve/lxc/$WDJ_CTID.conf"
if [ "$DRY" = 1 ]; then
  echo "[dry-run] append TUN device lines to $CONF"
else
  printf 'lxc.cgroup2.devices.allow: c 10:200 rwm\nlxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file\n' >> "$CONF"
fi
run pct start "$WDJ_CTID"

say "Waiting for the container's network"
if [ "$DRY" = 0 ]; then
  ok=0
  for _ in $(seq 1 45); do
    if pct exec "$WDJ_CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  [ "$ok" = 1 ] || die "the container cannot resolve names after 90 s; check the bridge/VLAN, gateway and DNS. \
Container $WDJ_CTID was left in place: fix it, or remove it with: pct stop $WDJ_CTID; pct destroy $WDJ_CTID --purge"
fi

# ----------------------------------------------------------------------------------------------- install inside
say "Passing settings into the container (0600 file, deleted after use)"
ANS="$(mktemp -d)/answers"
umask 077
{
  echo "WHODATJET_REF=$(printf %q "$REF")"
  echo "WHODATJET_PROFILE=$(printf %q "$WDJ_PROFILE")"
  echo "WHODATJET_TS_HOSTNAME=$(printf %q "$WDJ_HOSTNAME")"
  for v in WHODATJET_S3_BUCKET WHODATJET_S3_ENDPOINT_URL WHODATJET_S3_REGION WHODATJET_S3_PREFIX \
           WHODATJET_S3_ADDRESSING_STYLE WHODATJET_S3_ACCESS_KEY_ID WHODATJET_S3_SECRET_ACCESS_KEY \
           WHODATJET_TAILSCALE_AUTHKEY WHODATJET_SLACK_WEBHOOK_URL; do
    [ -z "${!v:-}" ] || echo "$v=$(printf %q "${!v}")"
  done
} > "$ANS"
cleanup() { shred -u "$ANS" 2>/dev/null || rm -f "$ANS"; rmdir "$(dirname "$ANS")" 2>/dev/null || true; }
trap cleanup EXIT
run pct push "$WDJ_CTID" "$ANS" /root/.whodatjet-answers --perms 0600

say "Downloading and installing WhoDatJet inside the container (this can take several minutes)"
GUEST_URL="$RAW/$REF/deploy/install.sh"
run pct exec "$WDJ_CTID" -- bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null
  curl -fsSL "$1" -o /root/whodatjet-install.sh
  WHODATJET_ANSWERS_FILE=/root/.whodatjet-answers bash /root/whodatjet-install.sh
  rm -f /root/whodatjet-install.sh' _ "$GUEST_URL" \
  || die "installation inside the container failed. Inspect with: pct enter $WDJ_CTID  (re-run: bash /root/whodatjet-install.sh \
after fetching $GUEST_URL). To start over: pct stop $WDJ_CTID; pct destroy $WDJ_CTID --purge"

[ "$DRY" = 0 ] || { say "Dry run finished; nothing was changed"; exit 0; }
say "WhoDatJet is installed in container $WDJ_CTID"
cat <<MSG
  Shell:     pct enter $WDJ_CTID
  Services:  whodatjet-api, whodatjet-admin, whodatjet-ingest.timer, whodatjet-backup.timer, whodatjet-watchdog.timer
  Admin:     https://$WDJ_HOSTNAME.<your-tailnet>.ts.net:8443  (admin / admin; you must change the password at first login)
  Resize:    bash install.sh --resize $WDJ_CTID --profile low|medium|high
  Update:    pct exec $WDJ_CTID -- bash -c 'curl -fsSL $GUEST_URL | bash'
MSG
