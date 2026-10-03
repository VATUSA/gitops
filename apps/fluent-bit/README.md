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

## IP truncation

The privacy policy (www.vatusa.net/info/privacy#data-retention) promises full
IP addresses for 90 days, then truncated IPs. The `log-ip-truncation` CronJob
(`templates/log-ip-truncation.yaml`, script in `files/truncate_log_ips.py`)
runs daily at 04:30 UTC and rewrites each object in day partitions older than
90 days, in place, with `client_ip` truncated: IPv4 to /24, IPv6 to /48, and
anything unparseable to `?`. Every other field is unchanged, so the DuckDB
queries below still work.

Rewritten objects carry `x-amz-meta-ip-truncated: v1` and are skipped on later
runs. Each run re-checks the 7 days before the cutoff (`LOOKBACK_DAYS`) to
cover missed runs. Truncation is idempotent, so re-running is always safe.

Credentials come from a second manually-created secret, a Spaces key scoped
to `vatusa-api-logs` with read/write:

```bash
kubectl create secret generic log-ip-truncation-do-spaces \
  --namespace logging \
  --from-literal=AWS_ACCESS_KEY_ID=<key> \
  --from-literal=AWS_SECRET_ACCESS_KEY=<secret>
```

To backfill, or to catch up after more than a week of failed runs, run a
one-off Job with `BACKFILL_FROM` set (`DRY_RUN=1` reports without writing):

```bash
kubectl create job -n logging --from=cronjob/log-ip-truncation log-ip-backfill \
  --dry-run=client -o yaml \
  | kubectl set env --local -f - BACKFILL_FROM=2026-06-23 -o yaml \
  | kubectl apply -f -
kubectl logs -n logging -f job/log-ip-backfill
```

### On AKS (Azure Blob)

`apps/fluent-bit-azure` runs the same script with `STORAGE_BACKEND=azure`
(its `files/truncate_log_ips.py` is a symlink to this chart's copy, so there is
one script). Differences:

- The marker is blob metadata `ip_truncated: v1`; Azure metadata names can't
  contain `-`. Listings include metadata, so finished blobs are skipped
  without being downloaded.
- Writes are conditional on the blob's ETag, so a blob that changed after it
  was read is never overwritten (the run fails instead).
- Credentials are the storage account key fluent-bit already uses
  (`fluent-bit-azure-blob`); no second secret.
- `LOOKBACK_DAYS` is 60 on dev, because dev AKS is stopped when idle and the
  CronJob only catches up on its latest missed run when the cluster starts.
- Leave blob **versioning and soft delete off** on the log container's
  account. Either would keep each pre-rewrite blob, full IPs included.

The backfill one-liner above works the same way (add `--context`).

Truncated logs are still personal data (the `uri` can contain CIDs), which is
why the policy says "truncated", not "anonymized".

## Querying with DuckDB

On AKS the logs are in Azure Blob; DuckDB's `azure` extension reads them with
your `az login`, no key needed. Fluent-bit's `azure_blob` output turns `$UUID`
into a folder, so the glob is one level deeper than on Spaces:

```sql
INSTALL azure;
LOAD azure;
SET azure_transport_option_type = 'curl';  -- otherwise "Problem with the SSL CA cert" on Arch
CREATE SECRET (TYPE azure, PROVIDER credential_chain, CHAIN 'cli', ACCOUNT_NAME 'vatusadevstorage');
SELECT count(*) FROM read_json_auto('az://vatusa-api-logs/year=*/month=*/day=*/*/*',
    hive_partitioning = true, format = 'newline_delimited', compression = 'gzip');
```

The rest of this section is the DOKS (Spaces) setup.

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
