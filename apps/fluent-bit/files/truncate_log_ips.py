"""Truncate client IPs in ingress access logs once they pass the retention window.

The privacy policy promises full IP addresses are kept for RETENTION_DAYS days.
After that, each object in the day partition is rewritten in place with every
address in `client_ip` truncated: IPv4 to its /24, IPv6 to its /48. Anything
that is not an IP address (other than an empty value or "-") is replaced
with "?" so an unexpected format can't leak an address.

Rewritten objects are tagged with x-amz-meta-ip-truncated, so re-running over
a day that is already done just skips it. Truncation is also idempotent, so
a partial run is safe to repeat.

Environment:
  S3_BUCKET, S3_ENDPOINT, S3_REGION   bucket location
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  RETENTION_DAYS   days of full IPs to keep (default 90)
  LOOKBACK_DAYS    days before the cutoff to re-check, covering missed runs (default 7)
  BACKFILL_FROM    YYYY-MM-DD; process every day from here to the cutoff instead
  DRY_RUN          "1" to report what would change without writing
"""

import datetime
import gzip
import ipaddress
import json
import os
import sys

MARKER_KEY = "ip-truncated"
MARKER_VALUE = "v1"


def truncate_address(value: str) -> str:
    value = value.strip()
    if value in ("", "-"):
        return value
    host = value
    # Strip a port: "1.2.3.4:5678" or "[2001:db8::1]:5678".
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "?"
    prefix = 24 if address.version == 4 else 48
    network = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
    return str(network.network_address)


def truncate_field(value: str) -> str:
    return ", ".join(truncate_address(part) for part in value.split(","))


def rewrite(body: bytes) -> tuple[bytes, int, int]:
    """Returns the rewritten NDJSON, the number of records, and unparseable lines dropped."""
    out = []
    records = 0
    dropped = 0
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            # nginx can log raw non-UTF-8 bytes from a client; surrogateescape
            # carries them through the rewrite unchanged.
            record = json.loads(line.decode("utf-8", "surrogateescape"))
        except json.JSONDecodeError:
            # Can't find the IP in a line we can't parse, so don't keep it.
            dropped += 1
            continue
        if isinstance(record.get("client_ip"), str):
            record["client_ip"] = truncate_field(record["client_ip"])
        out.append(
            json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8", "surrogateescape"
            )
        )
        records += 1
    return b"\n".join(out) + b"\n" if out else b"", records, dropped


def days_to_process(today: datetime.date) -> list[datetime.date]:
    retention = int(os.environ.get("RETENTION_DAYS", "90"))
    lookback = int(os.environ.get("LOOKBACK_DAYS", "7"))
    # A day is kept in full for `retention` days after it ends.
    last = today - datetime.timedelta(days=retention + 1)
    backfill = os.environ.get("BACKFILL_FROM")
    if backfill:
        first = datetime.date.fromisoformat(backfill)
    else:
        first = last - datetime.timedelta(days=lookback - 1)
    days = []
    day = first
    while day <= last:
        days.append(day)
        day += datetime.timedelta(days=1)
    return days


def process_day(s3, bucket: str, day: datetime.date, dry_run: bool) -> dict:
    prefix = day.strftime("year=%Y/month=%m/day=%d/")
    stats = {"objects": 0, "skipped": 0, "rewritten": 0, "records": 0, "dropped": 0}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in page.get("Contents", []):
            key = entry["Key"]
            stats["objects"] += 1
            obj = s3.get_object(Bucket=bucket, Key=key)
            if obj.get("Metadata", {}).get(MARKER_KEY) == MARKER_VALUE:
                stats["skipped"] += 1
                continue
            raw = obj["Body"].read()
            body = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
            new_body, records, dropped = rewrite(body)
            stats["records"] += records
            stats["dropped"] += dropped
            if dry_run:
                continue
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=gzip.compress(new_body),
                ContentType=obj.get("ContentType", "binary/octet-stream"),
                ContentEncoding="gzip",
                Metadata={MARKER_KEY: MARKER_VALUE},
            )
            stats["rewritten"] += 1
    return stats


def main() -> int:
    import boto3

    bucket = os.environ["S3_BUCKET"]
    dry_run = os.environ.get("DRY_RUN") == "1"
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        region_name=os.environ["S3_REGION"],
    )
    days = days_to_process(datetime.datetime.now(datetime.timezone.utc).date())
    if not days:
        print("nothing to do", flush=True)
        return 0
    print(
        f"truncating IPs in s3://{bucket} for {days[0]} .. {days[-1]}"
        f" ({len(days)} days){' [dry run]' if dry_run else ''}",
        flush=True,
    )
    totals = {}
    for day in days:
        stats = process_day(s3, bucket, day, dry_run)
        for name, count in stats.items():
            totals[name] = totals.get(name, 0) + count
        print(f"{day}: {stats}", flush=True)
    print(f"done: {totals}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
