"""The multi-worker API must not add ~40 ms to every keep-alive request (missing TCP_NODELAY)."""

from __future__ import annotations

import os
import socket
import statistics
import subprocess
import sys
import time

import httpx


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_keepalive_latency_with_multiple_workers(tmp_path):
    port = free_port()
    env = {**os.environ, "WHODATJET_DATA_DIR": str(tmp_path), "WHODATJET_API_PORT": str(port),
           "UVICORN_WORKERS": "2"}
    subprocess.run([sys.executable, "-m", "whodatjet.cli", "migrate"], env=env, check=True, capture_output=True)
    proc = subprocess.Popen([sys.executable, "-m", "whodatjet.cli", "serve"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as c:
            for _ in range(60):
                try:
                    c.get("/healthz")
                    break
                except httpx.HTTPError:
                    time.sleep(0.25)
            times = []
            for _ in range(10):
                t = time.perf_counter()
                assert c.get("/healthz").status_code == 200
                times.append((time.perf_counter() - t) * 1000)
        assert statistics.median(times) < 20, f"keep-alive requests take {statistics.median(times):.1f} ms"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
