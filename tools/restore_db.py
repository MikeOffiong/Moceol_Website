#!/usr/bin/env python3
"""
Moceol -- restores the subscribers DB from a dated backup in cloud storage.
Never touches the live DB - always writes to --out, which defaults to a
clearly-named local file. If you actually mean to replace production,
stop moceol-subscribe.service first, then copy the restored file over
/root/moceol_subscribers.db yourself.

Usage:
  List backups:            python3 restore_db.py --list
  Restore most recent:     python3 restore_db.py --out restored.db
  Restore a specific one:  python3 restore_db.py --key moceol-subscribers/moceol_subscribers_20261024_030000.db.gz --out restored.db
"""

import argparse
import gzip
import os
import shutil
import sqlite3
import sys

import boto3
from botocore.config import Config

S3_ENDPOINT_URL = os.environ.get("BACKUP_S3_ENDPOINT_URL", "")
S3_BUCKET = os.environ.get("BACKUP_S3_BUCKET", "")
S3_ACCESS_KEY = os.environ.get("BACKUP_S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("BACKUP_S3_SECRET_KEY", "")
S3_REGION = os.environ.get("BACKUP_S3_REGION", "auto")
S3_PREFIX = os.environ.get("BACKUP_S3_PREFIX", "moceol-subscribers")


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=Config(signature_version="s3v4"),
    )


def list_backups(client):
    paginator = client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{S3_PREFIX}/"):
        for obj in page.get("Contents", []):
            keys.append((obj["Key"], obj["LastModified"]))
    keys.sort(key=lambda x: x[1])
    return keys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true", help="List available backups and exit.")
    p.add_argument("--key", default="", help="Specific backup key to restore (default: most recent).")
    p.add_argument("--out", default="restored_moceol_subscribers.db", help="Where to write the restored DB.")
    args = p.parse_args()

    if not all([S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY]):
        print("Backup storage not configured (BACKUP_S3_* env vars missing).")
        sys.exit(1)

    client = s3_client()

    if args.list:
        backups = list_backups(client)
        if not backups:
            print("No backups found.")
            return
        for key, modified in backups:
            print(f"{modified}  {key}")
        return

    key = args.key
    if not key:
        backups = list_backups(client)
        if not backups:
            print("No backups found.")
            sys.exit(1)
        key = backups[-1][0]
        print(f"No --key given, using most recent: {key}")

    gz_path = "/tmp/_restore_download.db.gz"
    client.download_file(S3_BUCKET, key, gz_path)

    with gzip.open(gz_path, "rb") as f_in, open(args.out, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(gz_path)

    print(f"Restored to {args.out}")

    conn = sqlite3.connect(args.out)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print(f"Tables present: {tables}")
    for t in ("subscribers", "intake_submissions", "events", "sent_log"):
        if t in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {count} row(s)")
    conn.close()


if __name__ == "__main__":
    main()
