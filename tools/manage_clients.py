#!/usr/bin/env python3
"""Moceol -- tiny CLI to manage the live-sites count shown on services.html.

Run by hand on the droplet. Not automated - marking a client site live or
paused is still a manual decision Moceol makes once a site is actually
published. Reads/writes the same `clients` table subscribe_service.py's
GET /live-count counts from (rows with live=1).

Usage:
  python3 manage_clients.py --add <slug> "<Artist Name>"
  python3 manage_clients.py --live <slug>
  python3 manage_clients.py --pause <slug>
  python3 manage_clients.py --list
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.environ.get("SUBSCRIBE_DB_PATH", "/root/moceol_subscribers.db")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--add", nargs=2, metavar=("SLUG", "ARTIST_NAME"))
    p.add_argument("--live", metavar="SLUG")
    p.add_argument("--pause", metavar="SLUG")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()

    if not any([args.add, args.live, args.pause, args.list]):
        p.print_help()
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    try:
        if args.add:
            slug, artist_name = args.add
            try:
                conn.execute(
                    "INSERT INTO clients (slug, artist_name, live, created_at) VALUES (?, ?, 0, ?)",
                    (slug, artist_name, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
                print(f"Added {artist_name} ({slug}), not live yet.")
            except sqlite3.IntegrityError:
                print(f"A client with slug '{slug}' already exists.")
                sys.exit(1)
        elif args.live:
            cur = conn.execute("UPDATE clients SET live = 1 WHERE slug = ?", (args.live,))
            conn.commit()
            print(f"Marked {args.live} live." if cur.rowcount else f"No client with slug '{args.live}'.")
        elif args.pause:
            cur = conn.execute("UPDATE clients SET live = 0 WHERE slug = ?", (args.pause,))
            conn.commit()
            print(f"Marked {args.pause} paused." if cur.rowcount else f"No client with slug '{args.pause}'.")
        elif args.list:
            rows = conn.execute(
                "SELECT slug, artist_name, live FROM clients ORDER BY created_at"
            ).fetchall()
            if not rows:
                print("No clients recorded yet.")
            live_count = 0
            for slug, artist_name, live in rows:
                print(f"{'LIVE  ' if live else 'paused'}  {slug}  ({artist_name})")
                live_count += live
            print(f"\n{live_count} live.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
