#!/usr/bin/env python3
"""
Exports every "New drop alerts" signup to CSV.

Run on the droplet: /root/venv/bin/python3 /root/export_subscribers.py [output.csv]
Defaults to subscribers_export.csv in the current directory.
"""

import csv
import os
import sqlite3
import sys

DB_PATH = os.environ.get("SUBSCRIBE_DB_PATH", "/root/moceol_subscribers.db")


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "subscribers_export.csv"
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT email, created_at, source FROM subscribers ORDER BY created_at"
    ).fetchall()
    conn.close()

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "created_at", "source"])
        writer.writerows(rows)

    print(f"Exported {len(rows)} subscriber(s) to {out_path}")


if __name__ == "__main__":
    main()
