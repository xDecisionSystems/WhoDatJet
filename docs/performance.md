# Measured performance (Phase 8)

Measured 2026-11-14 on the live container (LXC 123 on proxopotamus): **medium profile**, 2 vCPU (Xeon E5-2620 @ 2.0 GHz), 4 GB RAM, 4 API workers, `--limit-concurrency` 24 per worker, the real FAA registry loaded (594,671 assignments, 536,426 N-numbers, 703,515 registration records, 2 snapshots). Load came from a separate machine over the tailnet (so `tailscaled` took 18-27% of the container's CPU on top of the API), straight to port 8000, not through the reverse proxy. Requests were a realistic mix: 60% current lookup, 30% lookup with `?date=`, 10% `/history`, on randomly chosen N-numbers; about a third were not-found (404) lookups.

| Concurrent clients | Throughput | p95 latency | `503` |
|---|---|---|---|
| 1 | 160 req/s | 7 ms | 0 |
| 24 | 755 req/s | 63 ms | 0 |
| 48 | 730 req/s | 116 ms | 0.2% |
| 100 | 585 req/s | 544 ms | 5% |
| 200 | 560 req/s | 1,123 ms | 5% |

Reading it:
- The container's two cores are the limit: about **750 requests/second** at best, with p95 under 100 ms up to roughly **30 in-flight requests**. That matches the plan's `medium` estimate (600-1,000 req/s, 30-50 in flight).
- Past the limit the service **sheds load with fast `503`s** instead of queueing without bound; throughput drops only 20-25% and `/healthz` keeps answering. Clients should retry `503`.
- A lookup costs 0.14 ms in SQLite (indexed); the rest is Python, JSON and the network stack, so more vCPUs raise throughput almost linearly.
- Still to measure: the `low` and `high` profiles, a run during an import, and the path through NPMplus.

## Bug found by this test
With 2 or more workers every keep-alive request after the first took about 44 ms (uvicorn's multi-worker listening socket lacked `TCP_NODELAY`, so Nagle's algorithm met the client's delayed ACK). Before the fix one client managed 19 req/s and the service topped out near 140 req/s; after it, 160 and 750. Fixed in `whodatjet.server`, with a regression test (`tests/test_server.py`).
