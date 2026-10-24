#!/usr/bin/env python3
"""Moceol -- weekly behavior summary export, for pasting into an AI agent.

Run by hand on the droplet. Aggregate behavior data only - visits, device
split, cities, traffic sources, the funnel, and submission counts by tier.
No emails, phone numbers, or names of any kind (artist/stage names
included) ever appear in this export, by design, regardless of what's
asked for later - this script simply never reads those columns.

Usage:
  python3 export_summary.py                 Writes a .txt file under
                                              EXPORT_DIR (default /root/exports)
  python3 export_summary.py --telegram       Also sends the same text as a
                                              Telegram message
  python3 export_summary.py --days 14        Change the window (default 7)
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import requests

DB_PATH = os.environ.get("SUBSCRIBE_DB_PATH", "/root/moceol_subscribers.db")
EXPORT_DIR = os.environ.get("EXPORT_DIR", "/root/exports")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_NOTIFY_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID", "")

TOP_N = 8


def build_summary(conn, days):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    def scalar(sql, params):
        return conn.execute(sql, params).fetchone()[0]

    def top(column):
        return conn.execute(
            f"SELECT {column}, COUNT(DISTINCT client_id) AS n FROM events "
            f"WHERE event_type = 'page_visit' AND created_at >= ? AND created_at < ? "
            f"AND {column} IS NOT NULL AND {column} != '' GROUP BY {column} ORDER BY n DESC LIMIT {TOP_N}",
            (start_iso, end_iso),
        ).fetchall()

    visitors = scalar(
        "SELECT COUNT(DISTINCT client_id) FROM events "
        "WHERE event_type = 'page_visit' AND created_at >= ? AND created_at < ?",
        (start_iso, end_iso),
    )
    started = scalar(
        "SELECT COUNT(DISTINCT client_id) FROM events "
        "WHERE event_type = 'form_started' AND created_at >= ? AND created_at < ?",
        (start_iso, end_iso),
    )
    submitted = scalar(
        "SELECT COUNT(DISTINCT client_id) FROM events "
        "WHERE event_type = 'form_submitted' AND created_at >= ? AND created_at < ?",
        (start_iso, end_iso),
    )
    tier_rows = conn.execute(
        "SELECT tier, COUNT(*) FROM intake_submissions WHERE created_at >= ? AND created_at < ? GROUP BY tier",
        (start_iso, end_iso),
    ).fetchall()

    lines = [
        f"Moceol behavior summary - last {days} days ({start.date().isoformat()} to {end.date().isoformat()})",
        "No names, emails or phone numbers are included in this export.",
        "",
        f"Visitors: {visitors}",
        f"Form starts: {started}",
        f"Submissions: {submitted}",
        f"Funnel: {visitors} visited -> {started} started -> {submitted} submitted",
        "",
        "Submissions by tier:",
    ]
    if tier_rows:
        for tier, n in tier_rows:
            lines.append(f"  {tier or 'unspecified'}: {n}")
    else:
        lines.append("  none")

    lines.append("")
    lines.append("Devices:")
    devices = top("device_type")
    lines += [f"  {d}: {n}" for d, n in devices] if devices else ["  no data"]

    lines.append("")
    lines.append("Top cities:")
    cities = top("city")
    lines += [f"  {c}: {n}" for c, n in cities] if cities else ["  no data"]

    lines.append("")
    lines.append("Top traffic sources:")
    sources = top("traffic_source")
    lines += [f"  {s}: {n}" for s, n in sources] if sources else ["  no data"]

    return "\n".join(lines)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_NOTIFY_BOT_TOKEN/CHAT_ID not set - cannot send.", file=sys.stderr)
        return False
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=15,
    )
    if resp.status_code >= 300:
        print(f"Telegram send failed ({resp.status_code}): {resp.text[:300]}", file=sys.stderr)
        return False
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--telegram", action="store_true", help="Also send as a Telegram message.")
    p.add_argument("--days", type=int, default=7, help="Window size in days (default 7).")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        summary = build_summary(conn, args.days)
    finally:
        conn.close()

    os.makedirs(EXPORT_DIR, exist_ok=True)
    filename = f"weekly_summary_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
    out_path = os.path.join(EXPORT_DIR, filename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(summary)
    print(f"Written to {out_path}")
    print()
    print(summary)

    if args.telegram:
        # Telegram messages cap at 4096 chars - this summary is always far
        # under that, but split just in case a very large --days value grows it.
        if send_telegram(summary):
            print("\nSent to Telegram.")


if __name__ == "__main__":
    main()
