"""Process launcher for the API.

Why not just `uvicorn.run(..., workers=N)`: with more than one worker uvicorn creates the shared listening
socket itself, and the connections accepted from it do not get TCP_NODELAY. The server then sends each
response as two small writes (headers, body), and Nagle's algorithm plus the client's delayed ACK stall every
keep-alive request after the first by about 40 ms (measured: 44 ms per request with 2+ workers, 3 ms with
one). On Linux accepted connections inherit TCP_NODELAY from the listening socket, so we set it there.
"""

from __future__ import annotations

import socket

import uvicorn
from uvicorn.supervisors import Multiprocess


def run_api(host: str, port: int, workers: int, limit_concurrency: int | None,
            app: str = "whodatjet.api:create_app_from_env") -> None:
    config = uvicorn.Config(app, factory=True, host=host, port=port, workers=workers,
                            limit_concurrency=limit_concurrency, proxy_headers=False, server_header=False,
                            log_level="info")
    if workers > 1:
        sock = config.bind_socket()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        Multiprocess(config, sockets=[sock]).run()
    else:
        uvicorn.Server(config).run()
