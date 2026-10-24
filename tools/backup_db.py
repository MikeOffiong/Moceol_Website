#!/usr/bin/env python3
"""
Moceol -- daily backup of the subscribers/events database to S3-compatible
cloud storage (Cloudflare R2 or Backblaze B2). Run by a systemd timer
(moceol-backup.timer) - safe to run by hand too.

Backs up SUBSCRIBE_DB_PATH (subscribers, intake submissions, and the
append-only event log) using sqlite3's own online backup API for a
consistent snapshot even while the live service is writing to it, gzips
it, and uploads to the configured bucket under a dated key. Also deletes
remote backups older than BACKUP_RETENTION_DAYS as a safety net - set a
bucket lifecycle rule too, this script isn't a substitute for one.

Needs BACKUP_S3_ENDPOINT_URL, BACKUP_S3_BUCKET, BACKUP_S3_ACCESS_KEY,
BACKUP_S3_SECRET_KEY set (same env file as the rest of the service).
Does nothing destructive if they're missing - just refuses to run.
"""

import gzip
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config

DB_PATH = os.environ.get("SUBSCRIBE_DB_PATH", "/root/moceol_subscribers.db")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/root/backups")
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))

S3_ENDPOINT_URL = os.environ.get("BACKUP_S3_ENDPOINT_URL", "")
S3_BUCKET = os.environ.get("BACKUP_S3_BUCKET", "")
S3_ACCESS_KEY = os.environ.get("BACKUP_S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("BACKUP_S3_SECRET_KEY", "")
S3_REGION = os.environ.get("BACKUP_S3_REGION", "auto")
S3_PREFIX = os.environ.get("BACKUP_S3_PREFIX", "moceol-subscribers")


def make_snapshot():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join(BACKUP_DIR, f"moceol_subscribers_{timestamp}.db")

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(snapshot_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    gz_path = snapshot_path + ".gz"
    with open(snapshot_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(snapshot_path)
    return gz_path, timestamp


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=Config(signature_version="s3v4"),
    )


def upload(client, gz_path, timestamp):
    key = f"{S3_PREFIX}/moceol_subscribers_{timestamp}.db.gz"
    client.upload_file(gz_path, S3_BUCKET, key)
    return key


def prune_old_backups(client):
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_RETENTION_DAYS)
    paginator = client.get_paginator("list_objects_v2")
    removed = 0
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{S3_PREFIX}/"):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                client.delete_object(Bucket=S3_BUCKET, Key=obj["Key"])
                removed += 1
    return removed


def prune_local_snapshots(keep=2):
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db.gz"))
    for f in (files[:-keep] if len(files) > keep else []):
        os.remove(os.path.join(BACKUP_DIR, f))


def main():
    if not all([S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY]):
        print("Backup storage not configured (BACKUP_S3_* env vars missing). Aborting.")
        sys.exit(1)

    gz_path, timestamp = make_snapshot()
    size_kb = os.path.getsize(gz_path) / 1024
    print(f"Snapshot created: {gz_path} ({size_kb:.1f} KB)")

    client = s3_client()
    key = upload(client, gz_path, timestamp)
    print(f"Uploaded to s3://{S3_BUCKET}/{key}")

    removed = prune_old_backups(client)
    if removed:
        print(f"Pruned {removed} remote backup(s) older than {BACKUP_RETENTION_DAYS} days")

    prune_local_snapshots()
    print("Done.")


if __name__ == "__main__":
    main()
