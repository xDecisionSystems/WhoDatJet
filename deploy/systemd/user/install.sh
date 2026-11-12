#!/usr/bin/env bash
# Install the interim user-level collection timer.
# Usage: deploy/systemd/user/install.sh [ENV_FILE]   (default: <repo>/.env)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
env_file="$(realpath "${1:-$repo/.env}")"
[ -r "$env_file" ] || { echo "env file not readable: $env_file" >&2; exit 1; }
(cd "$repo" && uv sync --frozen -q)
dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$dest"
sed -e "s#@REPO_DIR@#$repo#g" -e "s#@ENV_FILE@#$env_file#g" "$here/whodatjet-ingest.service" > "$dest/whodatjet-ingest.service"
cp "$here/whodatjet-ingest.timer" "$dest/whodatjet-ingest.timer"
systemctl --user daemon-reload
systemctl --user enable --now whodatjet-ingest.timer
systemctl --user list-timers whodatjet-ingest.timer --no-pager
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo
  echo "NOTE: lingering is off, so the timer runs only while $USER is logged in."
  echo "      Enable it once with:  sudo loginctl enable-linger $USER"
fi
