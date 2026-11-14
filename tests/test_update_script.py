"""deploy/whodatjet-update: verification, switching, automatic rollback (with stub cosign/uv/systemctl)."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "whodatjet-update"
REPO = "xDecisionSystems/WhoDatJet"

STUB_WHODATJET = """#!/usr/bin/env bash
echo "whodatjet $*" >> "$WDJ_TEST_LOG"
if [ "$1" = migrate ] && [ -n "${WDJ_STUB_MIGRATE_FAIL:-}" ]; then exit 1; fi
exit 0
"""
STUB_UV = """#!/usr/bin/env bash
echo "uv $*" >> "$WDJ_TEST_LOG"
echo "uv-cwd $PWD" >> "$WDJ_TEST_LOG"
mkdir -p .venv/bin
cp "$WDJ_STUB_WHODATJET" .venv/bin/whodatjet && chmod +x .venv/bin/whodatjet
ln -sf "$WDJ_STUB_PYTHON" .venv/bin/python
"""
STUB_COSIGN = """#!/usr/bin/env bash
echo "cosign $*" >> "$WDJ_TEST_LOG"
[ -z "${WDJ_STUB_COSIGN_FAIL:-}" ]
"""
STUB_RUNUSER = """#!/usr/bin/env bash
# runuser -u USER -- CMD...   (no user switching in tests; just run CMD)
echo "runuser $*" >> "$WDJ_TEST_LOG"
[ "$1" = -u ] && shift 2
[ "$1" = -- ] && shift
exec "$@"
"""
STUB_SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "$WDJ_TEST_LOG"
"""


class Health:
    """Local HTTP endpoint: /healthz (200 or 503) and /releases/latest (redirects to the latest tag)."""

    def __init__(self):
        self.ok = True
        self.latest = "v0.2.0"
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.startswith("/healthz"):
                    ok = outer.ok() if callable(outer.ok) else outer.ok
                    self.send_response(200 if ok else 503)
                    self.end_headers()
                elif self.path == "/releases/latest":
                    self.send_response(302)
                    self.send_header("Location", f"/releases/tag/{outer.latest}")
                    self.end_headers()
                elif self.path.startswith("/releases/tag/"):
                    self.send_response(200)
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def health():
    h = Health()
    yield h
    h.server.shutdown()


@pytest.fixture
def box(tmp_path, health):
    """A fake installation (v0.1.0 current) plus a release server directory."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("whodatjet-stub", STUB_WHODATJET), ("uv", STUB_UV), ("cosign", STUB_COSIGN),
                       ("systemctl", STUB_SYSTEMCTL), ("runuser", STUB_RUNUSER)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    app = tmp_path / "opt"
    old = app / "releases" / "v0.1.0"
    (old / ".venv" / "bin").mkdir(parents=True)
    shutil_copy(bindir / "whodatjet-stub", old / ".venv" / "bin" / "whodatjet")
    (old / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (app / "current").symlink_to(old)
    (tmp_path / "units").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "svc.env").write_text("X=1\n")
    env = {
        **os.environ,
        "WDJ_NO_ROOT_CHECK": "1", "WDJ_APP": str(app), "WDJ_DATA": str(tmp_path / "data"),
        "WDJ_ENV": str(tmp_path / "svc.env"), "WDJ_STORE_ENV": str(tmp_path / "none.env"),
        "WDJ_UNIT_DIR": str(tmp_path / "units"), "WDJ_RELEASE_BASE": f"file://{tmp_path}/rel",
        "WDJ_COSIGN": str(bindir / "cosign"), "WDJ_UV": str(bindir / "uv"),
        "WDJ_SYSTEMCTL": str(bindir / "systemctl"), "WDJ_RUNUSER": str(bindir / "runuser"),
        "WDJ_PYTHON": sys.executable,
        "WDJ_HEALTH_API": f"http://127.0.0.1:{health.port}/healthz", "WDJ_HEALTH_WAIT": "2",
        "WDJ_LATEST_URL": f"http://127.0.0.1:{health.port}/releases/latest",
        "WDJ_SELF": str(tmp_path / "installed-update"), "WDJ_TEST_LOG": str(tmp_path / "calls.log"),
        "WDJ_STUB_WHODATJET": str(bindir / "whodatjet-stub"), "WDJ_STUB_PYTHON": sys.executable,
    }
    return tmp_path, app, env


def shutil_copy(src: Path, dst: Path) -> None:
    dst.write_bytes(src.read_bytes())
    dst.chmod(0o755)


def make_release(root: Path, tag: str, *, tamper: bool = False, manifest_version: str | None = None) -> None:
    rel = root / "rel" / tag
    rel.mkdir(parents=True)
    src = root / f"src-{tag}" / f"whodatjet-{tag}"
    (src / "deploy" / "systemd").mkdir(parents=True)
    (src / "deploy" / "systemd" / "whodatjet-api.service").write_text(f"[Unit]\nDescription=api {tag}\n")
    (src / "deploy" / "whodatjet-update").write_text("#!/bin/sh\necho new updater\n")
    (src / "pyproject.toml").write_text("[project]\nname='x'\n")
    tarball = rel / f"whodatjet-{tag}.tar.gz"
    with tarfile.open(tarball, "w:gz") as t:
        t.add(src, arcname=f"whodatjet-{tag}")
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    manifest = {"schema": 1, "version": manifest_version or tag, "repository": REPO,
                "assets": {f"whodatjet-{tag}.tar.gz": {"sha256": digest, "size": tarball.stat().st_size}}}
    (rel / "manifest.json").write_text(json.dumps(manifest))
    (rel / "manifest.json.sigstore.json").write_text("{}")
    if tamper:
        tarball.write_bytes(tarball.read_bytes() + b"x")


def run(env, *args, **extra):
    return subprocess.run(["bash", str(SCRIPT), *args], env={**env, **extra}, capture_output=True, text=True,
                          timeout=60)


def calls(root: Path) -> list[str]:
    p = root / "calls.log"
    return p.read_text().splitlines() if p.exists() else []


def current(app: Path) -> str:
    return os.path.basename(os.path.realpath(app / "current"))


def test_successful_update(box):
    root, app, env = box
    make_release(root, "v0.2.0")
    r = run(env, "--version", "v0.2.0")
    assert r.returncode == 0, r.stderr + r.stdout
    assert current(app) == "v0.2.0"
    assert os.path.basename(os.path.realpath(app / "previous")) == "v0.1.0"
    log = calls(root)
    verify = next(line for line in log if line.startswith("cosign verify-blob"))
    assert f"--certificate-identity https://github.com/{REPO}/.github/workflows/release.yml@refs/tags/v0.2.0" in verify
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in verify
    # commands reach the app intact through the service-account wrapper (regression: first word was dropped)
    # the virtualenv must be built in its final directory (venv scripts embed absolute paths)
    assert f"uv-cwd {app}/releases/v0.2.0" in log, log
    assert (app / "releases" / "v0.2.0" / ".installed").exists()
    assert any(line == "whodatjet backup --keep 14" for line in log), log
    assert any(line == "whodatjet migrate" for line in log), log
    assert not any("exec:" in line for line in log)
    assert log.index(next(line for line in log if "whodatjet backup" in line)) < \
        log.index(next(line for line in log if "whodatjet migrate" in line))
    assert any(line.startswith("systemctl restart whodatjet-api whodatjet-admin") for line in log)
    assert (root / "units" / "whodatjet-api.service").read_text().endswith("api v0.2.0\n")
    assert (root / "installed-update").exists()                                    # the updater updates itself
    assert any("update_result" in line and "updated from v0.1.0 to v0.2.0" in line for line in log)


def test_invalid_signature_installs_nothing(box):
    root, app, env = box
    make_release(root, "v0.2.0")
    r = run(env, "--version", "v0.2.0", WDJ_STUB_COSIGN_FAIL="1")
    assert r.returncode != 0 and "NOT valid" in r.stderr
    assert current(app) == "v0.1.0" and not (app / "releases" / "v0.2.0").exists()
    assert not any(line.startswith("systemctl restart") for line in calls(root))


def test_tampered_tarball_rejected(box):
    root, app, env = box
    make_release(root, "v0.2.0", tamper=True)
    r = run(env, "--version", "v0.2.0")
    assert r.returncode != 0 and "does not match the signed manifest" in r.stderr
    assert current(app) == "v0.1.0" and not (app / "releases" / "v0.2.0").exists()


def test_manifest_for_another_version_rejected(box):
    root, app, env = box
    make_release(root, "v0.2.0", manifest_version="v0.0.9")      # replayed older signed manifest
    r = run(env, "--version", "v0.2.0")
    assert r.returncode != 0 and "not v0.2.0" in r.stderr
    assert current(app) == "v0.1.0"


def test_unhealthy_after_update_rolls_back_automatically(box, health):
    root, app, env = box
    make_release(root, "v0.2.0")
    health.ok = lambda: current(app) == "v0.1.0"                                 # only the old code is healthy
    r = run(env, "--version", "v0.2.0")
    assert r.returncode == 1 and "health check failed" in r.stderr
    assert current(app) == "v0.1.0"                                              # back on the old code
    restarts = [line for line in calls(root) if line.startswith("systemctl restart")]
    assert len(restarts) == 2                                                    # restarted for new, then for old
    assert any("update_result" in line and "rolled back" in line for line in calls(root))


def test_migration_failure_rolls_back(box):
    root, app, env = box
    make_release(root, "v0.2.0")
    r = run(env, "--version", "v0.2.0", WDJ_STUB_MIGRATE_FAIL="1")
    assert r.returncode == 1 and "migration failed" in r.stderr
    assert current(app) == "v0.1.0"


def test_manual_rollback(box):
    root, app, env = box
    make_release(root, "v0.2.0")
    assert run(env, "--version", "v0.2.0").returncode == 0
    before = len(calls(root))
    r = run(env, "--rollback")
    assert r.returncode == 0, r.stderr
    assert current(app) == "v0.1.0"
    assert os.path.basename(os.path.realpath(app / "previous")) == "v0.2.0"      # and rollback can be undone
    assert not any(line.startswith("whodatjet migrate") for line in calls(root)[before:])  # code only


def test_rollback_without_previous_fails(box):
    root, app, env = box
    r = run(env, "--rollback")
    assert r.returncode != 0 and "no previous release" in r.stderr


def test_check_and_up_to_date(box, health):
    root, app, env = box
    r = run(env, "--check")
    assert r.returncode == 0 and "installed: v0.1.0" in r.stdout and "target:    v0.2.0" in r.stdout
    health.latest = "v0.1.0"
    r = run(env)
    assert r.returncode == 0 and "already up to date" in r.stdout
    assert not any("cosign" in line for line in calls(root))                     # nothing downloaded or verified


def test_rejects_odd_version_strings(box):
    root, app, env = box
    for bad in ("../x", "latest", "v1", "v1.2.3;rm"):
        assert run(env, "--version", bad).returncode != 0, bad


def test_old_releases_pruned(box):
    root, app, env = box
    for v in ("v0.2.0", "v0.3.0", "v0.4.0", "v0.5.0"):
        make_release(root, v)
        assert run(env, "--version", v, WDJ_KEEP_RELEASES="2").returncode == 0, v
    kept = sorted(p.name for p in (app / "releases").iterdir())
    assert "v0.5.0" in kept and "v0.4.0" in kept and "v0.1.0" not in kept


def test_missing_cosign_is_reported_as_such_not_as_bad_signature(box):
    root, app, env = box
    make_release(root, "v0.2.0")
    r = run(env, "--version", "v0.2.0", WDJ_COSIGN="/nonexistent/cosign")
    assert r.returncode != 0 and "cosign not found" in r.stderr and "NOT valid" not in r.stderr
    assert current(app) == "v0.1.0" and not (app / "releases" / "v0.2.0").exists()


def test_path_includes_usr_local_bin(box):
    root, app, env = box
    env = {**env, "PATH": "/usr/bin:/bin"}
    env["WDJ_COSIGN"] = "cosign"                                   # resolved through the script's own PATH fix
    r = subprocess.run(["bash", "-c", f'export PATH=/usr/bin:/bin; bash {SCRIPT} --check'], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_incomplete_release_directory_is_rebuilt(box):
    root, app, env = box
    make_release(root, "v0.2.0")
    stale = app / "releases" / "v0.2.0"
    stale.mkdir(parents=True)
    (stale / "junk").write_text("half-installed")                     # left by an interrupted earlier attempt
    r = run(env, "--version", "v0.2.0")
    assert r.returncode == 0, r.stderr
    assert not (stale / "junk").exists() and (stale / ".installed").exists()
