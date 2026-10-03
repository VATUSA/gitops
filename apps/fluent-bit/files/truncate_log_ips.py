"""Truncate client IPs in ingress access logs once they pass the retention window.

The privacy policy promises full IP addresses are kept for RETENTION_DAYS days.
After that, each object in the day partition is rewritten in place with every
address in `client_ip` truncated: IPv4 to its /24, IPv6 to its /48. Anything
that is not an IP address (other than an empty value or "-") is replaced
with "?" so an unexpected format can't leak an address.

Rewritten objects are tagged in their metadata (x-amz-meta-ip-truncated on S3,
ip_truncated on Azure Blob, whose metadata names can't contain "-"), so
re-running over a day that is already done just skips it. Truncation is also
idempotent, so a partial run is safe to repeat.

Environment:
  STORAGE_BACKEND  "s3" (default; DO Spaces, R2) or "azure" (Azure Blob)
  For s3:
    S3_BUCKET, S3_ENDPOINT, S3_REGION   bucket location
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  For azure:
    AZURE_STORAGE_ACCOUNT, AZURE_CONTAINER   container location
    AZURE_STORAGE_KEY                        account key
    AZURE_BLOB_ENDPOINT   optional; defaults to https://<account>.blob.core.windows.net
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


class S3Store:
    MARKER_KEY = "ip-truncated"

    def __init__(self):
        import boto3

        self.bucket = os.environ["S3_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT"],
            region_name=os.environ["S3_REGION"],
        )
        self.url = f"s3://{self.bucket}"

    def list(self, prefix: str):
        """Yields (key, done); done is None when unknown until the object is read."""
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for entry in page.get("Contents", []):
                yield entry["Key"], None

    def get(self, key: str) -> tuple[bytes, bool, dict]:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        done = obj.get("Metadata", {}).get(self.MARKER_KEY) == MARKER_VALUE
        return obj["Body"].read(), done, obj

    def put(self, key: str, body: bytes, original: dict) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=original.get("ContentType", "binary/octet-stream"),
            ContentEncoding="gzip",
            Metadata={self.MARKER_KEY: MARKER_VALUE},
        )


class AzureBlobStore:
    MARKER_KEY = "ip_truncated"

    def __init__(self):
        from azure.storage.blob import ContainerClient

        account = os.environ["AZURE_STORAGE_ACCOUNT"]
        container = os.environ["AZURE_CONTAINER"]
        self.client = ContainerClient(
            os.environ.get("AZURE_BLOB_ENDPOINT", f"https://{account}.blob.core.windows.net"),
            container,
            credential={"account_name": account, "account_key": os.environ["AZURE_STORAGE_KEY"]},
        )
        self.url = f"az://{account}/{container}"

    def list(self, prefix: str):
        # Listing returns metadata, so finished blobs are skipped without a download.
        for blob in self.client.list_blobs(name_starts_with=prefix, include=["metadata"]):
            yield blob.name, (blob.metadata or {}).get(self.MARKER_KEY) == MARKER_VALUE

    def get(self, key: str) -> tuple[bytes, bool, dict]:
        downloader = self.client.download_blob(key)
        done = (downloader.properties.metadata or {}).get(self.MARKER_KEY) == MARKER_VALUE
        return downloader.readall(), done, downloader.properties

    def put(self, key: str, body: bytes, original) -> None:
        from azure.core import MatchConditions
        from azure.storage.blob import ContentSettings

        # Keep fluent-bit's content settings (it sets no Content-Encoding on .gz
        # blobs, which DuckDB relies on to read them as gzip).
        settings = original.content_settings
        self.client.upload_blob(
            key,
            body,
            overwrite=True,
            content_settings=ContentSettings(
                content_type=settings.content_type,
                content_encoding=settings.content_encoding,
            ),
            metadata={self.MARKER_KEY: MARKER_VALUE},
            # Fail rather than overwrite if fluent-bit or anything else changed
            # the blob since it was read.
            etag=original.etag,
            match_condition=MatchConditions.IfNotModified,
        )


def process_day(store, day: datetime.date, dry_run: bool) -> dict:
    prefix = day.strftime("year=%Y/month=%m/day=%d/")
    stats = {"objects": 0, "skipped": 0, "rewritten": 0, "records": 0, "dropped": 0}
    for key, done in store.list(prefix):
        stats["objects"] += 1
        if done:
            stats["skipped"] += 1
            continue
        raw, done, original = store.get(key)
        if done:
            stats["skipped"] += 1
            continue
        body = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        new_body, records, dropped = rewrite(body)
        stats["records"] += records
        stats["dropped"] += dropped
        if dry_run:
            continue
        store.put(key, gzip.compress(new_body), original)
        stats["rewritten"] += 1
    return stats


def main() -> int:
    backend = os.environ.get("STORAGE_BACKEND", "s3")
    store = {"s3": S3Store, "azure": AzureBlobStore}[backend]()
    dry_run = os.environ.get("DRY_RUN") == "1"
    days = days_to_process(datetime.datetime.now(datetime.timezone.utc).date())
    if not days:
        print("nothing to do", flush=True)
        return 0
    print(
        f"truncating IPs in {store.url} for {days[0]} .. {days[-1]}"
        f" ({len(days)} days){' [dry run]' if dry_run else ''}",
        flush=True,
    )
    totals = {}
    for day in days:
        stats = process_day(store, day, dry_run)
        for name, count in stats.items():
            totals[name] = totals.get(name, 0) + count
        print(f"{day}: {stats}", flush=True)
    print(f"done: {totals}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
