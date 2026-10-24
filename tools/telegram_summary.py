#!/usr/bin/env python3
"""Moceol -- daily/weekly Telegram analytics summary.

Run once a day by a systemd timer (moceol-summary.timer, not yet enabled -
see tools/moceol-summary.service/.timer). Reads the append-only `events`
table and `intake_submissions`, sends one clean message to the owner's
Telegram covering the previous UTC calendar day: visitors, device split,
top cities, top traffic sources, form starts, submissions, and a funnel
line. On Sundays, also sends a weekly roundup comparing the last 7 days
against the 7 days before that.

This is separate from the instant per-submission Telegram ping in
subscribe_service.py (notify_client_of_new_inquiry/send_telegram_notification)
- that stays exactly as it is. This script only adds a once-a-day digest.

Needs SUBSCRIBE_DB_PATH, TELEGRAM_NOTIFY_BOT_TOKEN, TELEGRAM_NOTIFY_CHAT_ID
(same env file as subscribe_service.py). Read-only against the DB - never
writes, never deletes.
"""

import html
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import requests

DB_PATH = os.environ.get("SUBSCRIBE_DB_PATH", "/root/moceol_subscribers.db")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_NOTIFY_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID", "")

TOP_N = 5


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_NOTIFY_BOT_TOKEN/CHAT_ID not set - cannot send summary.", file=sys.stderr)
        sys.exit(1)
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    if resp.status_code >= 300:
        print(f"Telegram send failed ({resp.status_code}): {resp.text[:300]}", file=sys.stderr)
        sys.exit(1)


def day_range(date):
    """UTC calendar-day [start, end) as ISO strings, for a date object."""
    start = datetime(date.year, date.month, date.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def period_metrics(conn, start_iso, end_iso):
    def scalar(sql, params):
        return conn.execute(sql, params).fetchone()[0]

    def top_rows(column, extra_where=""):
        rows = conn.execute(
            f"SELECT {column}, COUNT(DISTINCT client_id) AS n FROM events "
            f"WHERE event_type = 'page_visit' AND created_at >= ? AND created_at < ? "
            f"AND {column} IS NOT NULL AND {column} != '' {extra_where} "
            f"GROUP BY {column} ORDER BY n DESC LIMIT {TOP_N}",
            (start_iso, end_iso),
        ).fetchall()
        return rows

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
    real_submissions = scalar(
        "SELECT COUNT(*) FROM intake_submissions WHERE created_at >= ? AND created_at < ?",
        (start_iso, end_iso),
    )

    devices = top_rows("device_type")
    cities = top_rows("city")
    sources = top_rows("traffic_source")

    return {
        "visitors": visitors,
        "started": started,
        "submitted": submitted,
        "real_submissions": real_submissions,
        "devices": devices,
        "cities": cities,
        "sources": sources,
    }


def fmt_rows(rows, empty_label):
    if not rows:
        return empty_label
    return "\n".join(f"  {html.escape(str(label) or 'unknown')}: {n}" for label, n in rows)


def format_daily_message(metrics, for_date):
    m = metrics
    lines = [
        f"<b>\U0001F4CA Daily summary - {for_date.isoformat()}</b>",
        "",
        f"Visitors: <b>{m['visitors']}</b>",
        "",
        "<b>Devices</b>",
        fmt_rows(m["devices"], "  no visits"),
        "",
        "<b>Top cities</b>",
        fmt_rows(m["cities"], "  no location data"),
        "",
        "<b>Top traffic sources</b>",
        fmt_rows(m["sources"], "  no source data"),
        "",
        f"Form starts: <b>{m['started']}</b>",
        f"Submissions: <b>{m['real_submissions']}</b>",
        "",
        f"<b>Funnel</b>: {m['visitors']} visited → {m['started']} started → {m['submitted']} submitted",
    ]
    return "\n".join(lines)


def pct_change(new, old):
    if old == 0:
        return "n/a" if new == 0 else "+∞"
    change = (new - old) / old * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.0f}%"


def format_weekly_message(this_week, last_week, week_start, week_end):
    lines = [
        f"<b>\U0001F4C8 Weekly roundup - {week_start.isoformat()} to {week_end.isoformat()}</b>",
        "",
        f"Visitors: <b>{this_week['visitors']}</b> ({pct_change(this_week['visitors'], last_week['visitors'])} vs prior week)",
        f"Form starts: <b>{this_week['started']}</b> ({pct_change(this_week['started'], last_week['started'])})",
        f"Submissions: <b>{this_week['real_submissions']}</b> ({pct_change(this_week['real_submissions'], last_week['real_submissions'])})",
        "",
        f"<b>Funnel</b>: {this_week['visitors']} visited → {this_week['started']} started → {this_week['submitted']} submitted",
    ]
    return "\n".join(lines)


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        today_utc = datetime.now(timezone.utc).date()
        yesterday = today_utc - timedelta(days=1)

        start_iso, end_iso = day_range(yesterday)
        daily_metrics = period_metrics(conn, start_iso, end_iso)
        send_telegram(format_daily_message(daily_metrics, yesterday))
        print(f"Sent daily summary for {yesterday.isoformat()}.")

        if today_utc.weekday() == 6:  # Sunday
            # "This week" = the 7 full days ending yesterday (today's data is
            # still incoming). day_range(X)[0] is the start of day X, which
            # doubles as the exclusive end of day X-1 - so it's used both as
            # a start and, one week over, as an end boundary below.
            this_week_start = today_utc - timedelta(days=7)
            last_week_start = this_week_start - timedelta(days=7)

            this_start_iso = day_range(this_week_start)[0]
            this_end_iso = day_range(today_utc)[0]
            last_start_iso = day_range(last_week_start)[0]
            last_end_iso = this_start_iso

            this_week_metrics = period_metrics(conn, this_start_iso, this_end_iso)
            last_week_metrics = period_metrics(conn, last_start_iso, last_end_iso)

            send_telegram(format_weekly_message(this_week_metrics, last_week_metrics, this_week_start, today_utc - timedelta(days=1)))
            print("Sent weekly roundup.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
