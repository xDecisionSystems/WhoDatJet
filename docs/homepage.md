# Monitoring with Homepage

Two built-in Homepage mechanisms, no custom widget:

1. **Up/down dot** (no key): `siteMonitor` on `GET /healthz?strict=true`. It returns 503 when data is stale, the last import failed or the database is unavailable, so the dot reflects real data health, not only whether the process is running.
2. **Status widget** (`monitor` key): `customapi` on `GET /v1/status` with a `monitor`-scoped key in `X-API-Key`. A monitor key can call only `/v1/status` and gets 403 everywhere else, so a dashboard credential can never read registry data.

The console's **Monitoring** page issues the key and generates this entry with your real URLs. Put the key in a Homepage environment variable, not in the YAML:

```yaml
- Research:
    - WhoDatJet:
        icon: mdi-airplane
        href: https://aircraft.example.org/admin/
        description: FAA aircraft registry history
        siteMonitor: https://aircraft.example.org/healthz?strict=true
        widget:
          type: customapi
          url: https://aircraft.example.org/v1/status
          refreshInterval: 60000
          headers:
            X-API-Key: "{{HOMEPAGE_VAR_WHODATJET_KEY}}"
          mappings:
            - field: status
              label: Status
            - field: last_coverage_date
              label: Data as of
              format: date
            - field: active_alerts
              label: Alerts
              format: number
            - field: disk_free_pct
              label: Disk free
              format: percent
```

`/v1/status` returns flat JSON (top-level fields, numbers as numbers): `status` (ok, degraded, down), `version`, `profile`, `last_import_at`, `last_import_result`, `last_coverage_date`, `data_age_days`, `snapshots`, `open_gaps`, `active_alerts`, `requests_24h`, `p95_ms_24h`, `errors_503_24h`, `disk_free_pct`, `archive_store`, `archive_store_ok`, `archive_replica_lag`, `disk_days_remaining`. It is cached for 30 seconds and never contains registry data, key details or admin information. Homepage must be able to reach the API URL (on a tailnet-only name, the Homepage host must be on the tailnet).
