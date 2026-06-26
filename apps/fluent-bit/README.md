# fluent-bit — API access log pipeline

Ships structured JSON access logs from ingress-nginx to a DO Spaces bucket for
offline analysis with DuckDB. This was implemented as "Phase 0" of the VATUSA
legacy API migration: build an endpoint usage inventory before migrating anything.

## Architecture

```
ingress-nginx (all cluster traffic)
    → structured JSON log line per request
    → /var/log/containers/*_ingress-nginx_*.log (on each node)
    → fluent-bit DaemonSet (tails log files, kubernetes filter, S3 output)
    → DO Spaces bucket: vatusa-api-logs (sfo3)
    → DuckDB for analysis
```

fluent-bit runs as a DaemonSet so it covers all nodes. It only picks up
ingress-nginx container logs — everything else is ignored.

**Note:** ingress-nginx is the single ingress controller for the whole cluster,
so the bucket contains traffic for all hostnames (api.vatusa.net, www.vatusa.net,
forums.vatusa.net, per-ARTCC API subdomains, etc.). Filter by `host` in DuckDB
queries as needed.

## Log format

Configured in `apps/ingress-nginx/values.yaml` via `log-format-upstream`. Each
access log line is a JSON object with these fields:

| Field          | nginx variable           | Notes                                    |
|----------------|--------------------------|------------------------------------------|
| `time`         | `$time_iso8601`          |                                          |
| `host`         | `$host`                  |                                          |
| `method`       | `$request_method`        |                                          |
| `uri`          | `$uri`                   | **No query string** — prevents API keys from appearing in logs |
| `status`       | `$status`                |                                          |
| `request_time` | `$request_time`          | Seconds (float)                          |
| `bytes_sent`   | `$bytes_sent`            |                                          |
| `user_agent`   | `$http_user_agent`       |                                          |
| `client_ip`    | `$http_x_forwarded_for`  |                                          |
| `upstream`     | `$proxy_upstream_name`   | Identifies which backend served the request |

`$uri` (not `$request_uri`) is intentional: `$uri` strips the query string,
keeping `?apikey=...` and similar credentials out of the logs.

## S3 layout

Files land under Hive-style partition directories:

```
vatusa-api-logs/
  year=2026/
    month=06/
      day=23/
        210347-ingress.nginx.var.log.containers....
        211351-ingress.nginx.var.log.containers....
```

Files are gzip-compressed newline-delimited JSON. They do **not** get a `.gz`
file extension (fluent-bit S3 plugin behaviour) — use `compression = 'gzip'`
explicitly in DuckDB.

Upload triggers: whichever comes first — 10 MB accumulated or 10 minutes elapsed.
Low-traffic periods won't flush until the timeout fires.

## Credentials

The DO Spaces access key is stored in a **manually-created** Kubernetes secret
that is NOT tracked in Git:

```
namespace: logging
secret name: fluent-bit-do-spaces
keys: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
```

If the `logging` namespace or the ArgoCD Application is ever deleted, this secret
is lost and must be recreated before fluent-bit will start:

```bash
kubectl create secret generic fluent-bit-do-spaces \
  --namespace logging \
  --from-literal=AWS_ACCESS_KEY_ID=<key> \
  --from-literal=AWS_SECRET_ACCESS_KEY=<secret>
```

Future improvement: wire this through External Secrets Operator + OpenBao instead.

## Querying with DuckDB

```sql
INSTALL httpfs;
LOAD httpfs;

CREATE OR REPLACE SECRET do_spaces (
    TYPE S3,
    KEY_ID     'your-access-key-id',
    SECRET     'your-secret-access-key',
    REGION     'sfo3',
    ENDPOINT   'sfo3.digitaloceanspaces.com',
    URL_STYLE  'path'
);
```

**Important:** Use `URL_STYLE = 'path'` (not `vhost`). The glob must NOT have a
leading `/` before `year=`. The correct pattern is:

```sql
's3://vatusa-api-logs/year=*/month=*/day=*/**'
```

with `compression = 'gzip'` always specified explicitly.

### Endpoint inventory query

The primary use case — hit counts and latency per route, with numeric IDs
normalised to `:id`:

```sql
SELECT
    host,
    regexp_replace(uri, '/[0-9]+', '/:id') AS route_pattern,
    count(*)                                AS hits,
    round(avg(CAST(request_time AS DOUBLE)) * 1000, 1) AS avg_ms,
    count_if(CAST(status AS INT) >= 500)    AS errors
FROM read_json_auto(
    's3://vatusa-api-logs/year=*/month=*/day=*/**',
    compression     = 'gzip',
    format          = 'newline_delimited',
    hive_partitioning = true
)
WHERE host = 'api.vatusa.net'
GROUP BY 1, 2
ORDER BY hits DESC;
```

Scope to a date range cheaply (partition pruning):

```sql
WHERE year = '2026' AND month = '06' AND host = 'api.vatusa.net'
```

## Known gotchas

- **No `.gz` extension on files** — always pass `compression = 'gzip'` to DuckDB,
  never rely on file extension detection.
- **DuckDB glob must not start with `/`** — keys in DO Spaces are stored without
  a leading slash; a leading slash in the glob causes SigV4 signature mismatches
  and 403 errors even with valid credentials.
- **fluent-bit liveness probe needs HTTP server** — the chart's default liveness
  probe checks port 2020. If you override `config.service`, include
  `HTTP_Server On / HTTP_Listen 0.0.0.0 / HTTP_Port 2020` or the pod will
  CrashLoopBackOff.
- **varlog mount is not automatic in chart v0.47.9** — must be declared explicitly
  in `daemonSetVolumes` / `daemonSetVolumeMounts`, and `securityContext` must run
  as root (uid 0) to read root-owned host log files.
