#!/usr/bin/env python3
"""
Moceol -- manual "new release" announcement sender.
====================================================
Run by hand on the droplet. Never automatic, never triggered by the web
service - nothing schedules this.

    /root/venv/bin/python3 /root/send_announcement.py \\
        --release-key clarity-of-mind \\
        --title "Clarity of Mind" \\
        --link "https://moceol.com/listen.html?r=clarity-of-mind" \\
        --message "New one just dropped - give it a listen!"

--release-key is a short unique id for THIS release (the listen.html slug
is a natural choice). It's how dedup works: every successful send is
recorded in sent_log keyed on (email, release_key), so re-running the same
command later (after a partial failure, or just to catch new subscribers)
never re-emails anyone it already reached for that release.

--dry-run prints who it would email without sending anything or touching
sent_log - use it first, every time.

Without --yes, it prints the recipient count and asks for a typed "yes"
before sending anything live. Pass --yes only once you're sure.

Email is warm, first-person, greets each fan by first name ("Hey, James"),
and always includes a one-click unsubscribe link built from that
subscriber's own token - never their raw email.
"""

import argparse
import asyncio
import html
import os
import sqlite3
import sys

import aiohttp

DB_PATH = os.environ.get("SUBSCRIBE_DB_PATH", "/root/moceol_subscribers.db")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "Moceol <hello@hello.moceol.com>")
UNSUBSCRIBE_BASE_URL = os.environ.get("UNSUBSCRIBE_BASE_URL", "https://api.moceol.com/unsubscribe")
SEND_DELAY_SECONDS = float(os.environ.get("ANNOUNCE_SEND_DELAY_SECONDS", "0.5"))


def init_db():
    """Mirrors subscribe_service.py's schema so this script works even if
    it's ever run before the web service has started once on a fresh DB."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            source TEXT,
            unsubscribe_token TEXT,
            first_name TEXT,
            last_name TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_log (
            email TEXT NOT NULL,
            release_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (email, release_key)
        )
        """
    )
    conn.commit()
    conn.close()


def parse_args():
    p = argparse.ArgumentParser(
        description="Send a drop announcement to every subscriber not yet sent this release.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--release-key", required=True, help="Unique id for this release, e.g. the listen.html slug.")
    p.add_argument("--title", required=True, help="Release title.")
    p.add_argument("--link", required=True, help="listen.html link for this release.")
    p.add_argument("--message", required=True, help="Short custom message to include.")
    p.add_argument("--dry-run", action="store_true", help="Preview recipients only - sends nothing, records nothing.")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    return p.parse_args()


def get_pending_subscribers(release_key):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT s.email, s.first_name, s.unsubscribe_token
        FROM subscribers s
        WHERE s.unsubscribe_token IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM sent_log l WHERE l.email = s.email AND l.release_key = ?
          )
        ORDER BY s.created_at
        """,
        (release_key,),
    ).fetchall()
    conn.close()
    return rows


def record_sent(email, release_key):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO sent_log (email, release_key, sent_at) VALUES (?, ?, datetime('now'))",
        (email, release_key),
    )
    conn.commit()
    conn.close()


def build_email(first_name, title, link, message, unsubscribe_url):
    greeting = f"Hey, {first_name}" if first_name else "Hey"
    subject = f"New release: {title}"
    text_body = (
        f"{greeting},\n\n"
        f"{message}\n\n"
        f"{title} - listen now: {link}\n\n"
        f"Talk soon,\nMoceol\n\n"
        f"Unsubscribe: {unsubscribe_url}"
    )
    html_body = (
        f"<p>{html.escape(greeting)},</p>"
        f"<p>{html.escape(message)}</p>"
        f'<p><a href="{html.escape(link)}">{html.escape(title)} - listen now</a></p>'
        f"<p>Talk soon,<br/>Moceol</p>"
        f'<p style="margin-top:32px;font-size:12px;color:#999;">'
        f'<a href="{html.escape(unsubscribe_url)}" style="color:#999;">Unsubscribe</a></p>'
    )
    return subject, html_body, text_body


async def send_one(session, to_email, subject, html_body, text_body):
    try:
        async with session.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": RESEND_FROM, "to": [to_email], "subject": subject, "html": html_body, "text": text_body},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status >= 300:
                body_text = await resp.text()
                print(f"  FAILED {to_email}: {resp.status} {body_text[:200]}")
                return False
            return True
    except Exception as e:
        print(f"  FAILED {to_email}: {e}")
        return False


async def main_async(args):
    init_db()

    if not RESEND_API_KEY and not args.dry_run:
        print("RESEND_API_KEY is not set in the environment. Aborting.")
        sys.exit(1)

    pending = get_pending_subscribers(args.release_key)
    print(f"{len(pending)} subscriber(s) pending for release '{args.release_key}'.")
    if not pending:
        print("Nothing to send.")
        return

    for email, first_name, _ in pending[:10]:
        label = f"{first_name} <{email}>" if first_name else email
        print(f"  - {label}")
    if len(pending) > 10:
        print(f"  ...and {len(pending) - 10} more")

    if args.dry_run:
        print("\nDRY RUN - no emails sent, sent_log not updated.")
        return

    if not args.yes:
        confirm = input(f"\nSend to all {len(pending)} subscriber(s) above? Type 'yes' to continue: ")
        if confirm.strip().lower() != "yes":
            print("Aborted - nothing sent.")
            return

    sent, failed = 0, 0
    async with aiohttp.ClientSession() as session:
        for email, first_name, token in pending:
            unsubscribe_url = f"{UNSUBSCRIBE_BASE_URL}?token={token}"
            subject, html_body, text_body = build_email(first_name, args.title, args.link, args.message, unsubscribe_url)
            ok = await send_one(session, email, subject, html_body, text_body)
            if ok:
                record_sent(email, args.release_key)
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(SEND_DELAY_SECONDS)

    print(f"\nDone. Sent: {sent}, Failed: {failed}.")


def main():
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
