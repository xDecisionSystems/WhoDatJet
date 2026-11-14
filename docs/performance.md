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

## Other profiles (disposable container, same real data, same host, load from the VLAN without Tailscale)

| Profile | Peak throughput | p95 at that load | Sheds load (`503`) from about |
|---|---|---|---|
| `low` (1 vCPU, 2 workers) | ~520 req/s at 12 clients | 31 ms | 16-24 in flight (21% `503` at 24) |
| `medium` (2 vCPU, 4 workers) | ~1,000 req/s at 24 clients | 60 ms | well beyond 24 |
| `high` (4 vCPU, 8 workers) | ~1,430 req/s at 20 clients | 27 ms | beyond 150 clients (7 `503` in 6,700 requests) |

The `high` figure is limited by the single test machine (6 cores, Python clients), so it is a lower bound. Without the Tailscale hop the same `medium` container does about 1,000 req/s instead of 750.

## During an import
A full `rebuild` (a complete import of the archive, the heaviest write the service does) running at the same time as 24 concurrent clients on `medium`: throughput fell about 13% (1,020 to 887 req/s in a 40 s run), p95 unchanged, `503` rate negligible (6 in 160,000 over a 240 s run). The import itself took about 3.4 minutes at full load. The API keeps serving from its own consistent snapshot of the database throughout.

## Failure drills (live container, 2026-11-14)
| Drill | Result |
|---|---|
| Forced ingest failure (bad FAA URL) | Run recorded as failed, `ingest_failed` alert sent, `/healthz?strict=true` 503; the next normal ingest cleared the alert (one "resolved" message) and the health check |
| API stopped | One failed watchdog check raised nothing; the second raised "API unhealthy" (site returned 502); restart plus one watchdog run resolved it |
| S3 unreachable during ingest | Download kept in `staging/`, `ingest_failed` and "archive store problem" alerts, API unaffected; after recovery the staged file was reconciled (already stored, so dropped) and both alerts resolved |

## Bugs found by this testing
- Multi-worker keep-alive stall of 44 ms per request (below).
- `rebuild` failed with `database is locked` whenever the API had the database open (its swap renamed the file and changed the journal mode). It now copies the new database into the live file with SQLite's online backup, atomically, with no restart needed.
- The watchdog checked the admin console at `/healthz` after it moved under `/admin`, raising a false alert.
- The updater and the guest installer both failed to find `cosign` under `pct exec` and mis-reported it as a bad signature.

## Bug found by this test
With 2 or more workers every keep-alive request after the first took about 44 ms (uvicorn's multi-worker listening socket lacked `TCP_NODELAY`, so Nagle's algorithm met the client's delayed ACK). Before the fix one client managed 19 req/s and the service topped out near 140 req/s; after it, 160 and 750. Fixed in `whodatjet.server`, with a regression test (`tests/test_server.py`).
