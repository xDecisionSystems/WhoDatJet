"""Static and dry-run checks for the shell installers (they cannot be executed for real in CI)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SCRIPTS = [DEPLOY / "proxmox" / "install.sh", DEPLOY / "install.sh", DEPLOY / "whodatjet-update"]
HOST = DEPLOY / "proxmox" / "install.sh"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shell_syntax(script):
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0


def dry_run(*args, env=None):
    return subprocess.run(["bash", str(HOST), "--dry-run", "--yes", *args], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", **(env or {})}, timeout=30)


FULL = ["--ctid", "123", "--hostname", "whodatjet", "--profile", "high", "--root-storage", "md1200-0",
        "--bridge", "vmbr2", "--vlan", "20", "--ip", "10.10.20.123/24", "--gateway", "10.10.20.1",
        "--dns", "10.10.20.1", "--s3-bucket", "b", "--s3-access-key-id", "AK"]


def test_dry_run_maps_every_option(tmp_path):
    secret = tmp_path / "s"
    secret.write_text("topsecret\n")
    r = dry_run(*FULL, "--s3-secret-key-file", str(secret))
    assert r.returncode == 0, r.stderr
    create = next(line for line in r.stdout.splitlines() if "pct create" in line)
    for fragment in ("123", "--cores 4", "--memory 8192", "--rootfs md1200-0:8", "tag=20", "ip=10.10.20.123/24",
                     "gw=10.10.20.1", "--nameserver 10.10.20.1", "mp=/var/lib/whodatjet", "--unprivileged 1"):
        assert fragment.replace(",", "\\,") in create or fragment in create, fragment
    assert "topsecret" not in r.stdout + r.stderr                                  # never printed


def test_static_ip_requires_gateway():
    r = dry_run("--ip", "10.1.1.5/24")
    assert r.returncode != 0 and "gateway" in r.stderr


def test_option_errors():
    assert dry_run("--ctid").returncode != 0
    assert dry_run("--bogus").returncode == 2
    assert dry_run(*FULL, "--s3-secret-key-file", "/nonexistent").returncode != 0
    assert dry_run(*FULL).returncode != 0                                          # bucket without a secret


def test_release_version_selects_guest_installer_from_release(tmp_path):
    secret = tmp_path / "s"
    secret.write_text("x")
    r = dry_run(*FULL, "--s3-secret-key-file", str(secret), "--version", "v1.2.3")
    assert "releases/download/v1.2.3/guest-install.sh" in r.stdout
    r = dry_run(*FULL, "--s3-secret-key-file", str(secret))
    assert "/deploy/install.sh" in r.stdout and "releases/download" not in r.stdout


def test_version_stamp_placeholder_is_replaced_by_sed():
    for script in (HOST, DEPLOY / "install.sh"):
        text = script.read_text()
        assert text.count("@VERSION@") == 1                                        # exactly one stamp point
        stamped = text.replace("@VERSION@", "v9.9.9")
        assert 'VERSION="${WHODATJET_VERSION:-v9.9.9}"' in stamped


def test_help_lists_every_option():
    out = subprocess.run(["bash", str(HOST), "--help"], capture_output=True, text=True).stdout
    for opt in ("--ctid", "--hostname", "--profile", "--root-storage", "--data-storage", "--bridge", "--vlan",
                "--ip", "--gateway", "--dns", "--s3-bucket", "--s3-endpoint", "--s3-secret-key-file",
                "--tailscale-authkey-file", "--slack-webhook-file", "--version", "--resize", "--dry-run"):
        assert opt in out, opt
