#!/usr/bin/env python3
"""
Moceol -- "New drop alerts" email signup endpoint.
===================================================
Runs continuously on the droplet (systemd: moceol-subscribe.service),
bound to 127.0.0.1 only. Caddy terminates HTTPS at api.moceol.com and
reverse-proxies to this process - it is never exposed directly.

POST /subscribe  {email, first_name, last_name, source, website}
  - first_name is required, last_name is optional - used only to greet the
    fan by name in their welcome email and personalize the artist's alert.
  - "website" is an untitled honeypot field, hidden off-screen in the real
    form via CSS: real visitors never see or fill it, so anything non-empty
    there is almost certainly a bot. Accepted with a normal-looking 200 but
    never stored, so bots can't tell they were caught.
  - Email is validated and lowercased server-side (never trust the client).
    A duplicate (case-insensitive) returns the same success response
    without adding a row or revealing that it was a duplicate.
  - CORS is restricted to ALLOWED_ORIGINS; every other Origin gets no CORS
    headers at all. That's a browser-side convention, not real protection
    against a non-browser client, hence the rate limit and size cap below.
  - On a genuinely new row (not a duplicate, not the honeypot), fires two
    async emails via Resend - a warm first-person welcome to the fan, and
    a "you've got a new fan" alert to CLIENT_NOTIFY_EMAIL - both scheduled
    with asyncio.create_task so neither can slow down or fail the fan's
    own response. A Resend failure is logged, never raised back to the
    fan, and never costs the signup: the DB write already happened.

GET /unsubscribe?token=...
  - One click, no confirmation step (as asked) - removes that subscriber
    and shows a plain HTML confirmation page. Keyed off a random per-
    subscriber token, never the raw email, so the link can't be guessed
    or used to enumerate addresses. Registered with allow_head=False so
    email-security scanners that HEAD a link before a human opens it
    can't trigger a silent unsubscribe.

Never logs a full email address - not even in this private server's own
application log. The only place an email address is stored is the DB.
"""

import asyncio
import html
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from email_brand import (
    ARTIST_ACCENT,
    ARTIST_BG,
    ARTIST_CARD_BG,
    ARTIST_NAME,
    ARTIST_SITE_DISPLAY,
    ARTIST_SITE_URL,
    BRAND_MUTED,
    PLATFORM_ACCENT,
    PLATFORM_BG,
    PLATFORM_CARD_BG,
    RESEND_FROM,
    UNSUB_FOOTNOTE,
    build_artist_footer,
    build_fan_footer,
    render_email_html,
)

DB_PATH = os.environ.get("SUBSCRIBE_DB_PATH", "/root/moceol_subscribers.db")
ALLOWED_ORIGINS = set(
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS", "https://moceol.com,https://www.moceol.com"
    ).split(",") if o.strip()
)
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "5"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "600"))
LISTEN_HOST = os.environ.get("SUBSCRIBE_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SUBSCRIBE_LISTEN_PORT", "8080"))
UNSUBSCRIBE_BASE_URL = os.environ.get("UNSUBSCRIBE_BASE_URL", "https://api.moceol.com/unsubscribe")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LEN = 254
MAX_SOURCE_LEN = 64
MAX_BODY_BYTES = 8 * 1024  # this form never needs more than a few hundred bytes

MAX_NAME_LEN = 100

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
CLIENT_NOTIFY_EMAIL = os.environ.get("CLIENT_NOTIFY_EMAIL", "moceol.ent@gmail.com")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("subscribe")

# ---------------------------------------------------------------------------
# Per-IP sliding-window rate limit. In-memory is fine at this traffic scale -
# a restart just resets everyone's window, which is an acceptable trade-off
# for not needing a separate store.
# ---------------------------------------------------------------------------
_hits = {}


def _rate_limited(ip):
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    hits = [t for t in _hits.get(ip, []) if t > window_start]
    hits.append(now)
    _hits[ip] = hits
    if len(_hits) > 5000:
        for key in list(_hits.keys()):
            _hits[key] = [t for t in _hits[key] if t > window_start]
            if not _hits[key]:
                del _hits[key]
    return len(hits) > RATE_LIMIT_MAX


def init_db():
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
    # Migration for a DB created before these columns existed.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(subscribers)")]
    if "unsubscribe_token" not in cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN unsubscribe_token TEXT")
    if "first_name" not in cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN first_name TEXT")
    if "last_name" not in cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN last_name TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_subscribers_unsub_token "
        "ON subscribers(unsubscribe_token)"
    )
    # send_announcement.py dedupes against this so a release is never
    # re-sent to someone it already reached.
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


# ---------------------------------------------------------------------------
# Resend (transactional email)
# ---------------------------------------------------------------------------
# Fire-and-forget tasks need a strong reference somewhere or the event loop
# is free to garbage-collect them mid-flight - this set is that reference.
_background_tasks = set()


def fire_and_forget(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def send_email(to, subject, html_body, text_body=None):
    if not RESEND_API_KEY:
        log.error("RESEND_API_KEY not set - cannot send email (subject=%r)", subject)
        return False
    payload = {
        "from": RESEND_FROM,
        "to": [to] if isinstance(to, str) else to,
        "subject": subject,
        "html": html_body,
    }
    if text_body:
        payload["text"] = text_body
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 300:
                    body_text = await resp.text()
                    log.error("Resend send failed (status=%s, subject=%r): %s", resp.status, subject, body_text[:300])
                    return False
                return True
    except Exception as e:
        log.error("Resend send raised (subject=%r): %s", subject, e)
        return False


async def notify_client_of_new_fan(fan_email, first_name, last_name, created_at_iso, source):
    """Fired (not awaited) right after a genuinely new subscriber row is
    committed. A failure here is logged and never touches the fan-facing
    response - the signup itself already succeeded."""
    full_name = f"{first_name} {last_name}".strip() if last_name else first_name
    subject = f"New fan alert: {first_name} just joined your list"

    signup_dt = datetime.fromisoformat(created_at_iso)
    signup_display = signup_dt.strftime("%B %d, %Y at %I:%M %p UTC")

    text_body = (
        f"You've got a new fan!\n\n"
        f"{full_name} just signed up for your drop alerts.\n\n"
        f"Email: {fan_email}\n"
        f"Signed up: {signup_display}\n\n"
        "They've already been sent a warm welcome email.\n\n"
        f"Powered by {ARTIST_NAME}"
    )
    body_html = (
        '<p style="margin:0 0 16px; font-size:19px; font-weight:bold;">You&rsquo;ve got a new fan! \U0001F389</p>'
        f'<p style="margin:0 0 20px;"><strong>{html.escape(full_name)}</strong> just signed up for your drop alerts.</p>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%; margin:0 0 20px;">'
        f'<tr><td style="padding:3px 0; width:90px; color:{BRAND_MUTED};">Email</td>'
        f'<td style="padding:3px 0;">{html.escape(fan_email)}</td></tr>'
        f'<tr><td style="padding:3px 0; color:{BRAND_MUTED};">Signed up</td>'
        f'<td style="padding:3px 0;">{html.escape(signup_display)}</td></tr>'
        "</table>"
        f'<p style="margin:0; font-size:13px; color:{BRAND_MUTED};">They&rsquo;ve already been sent a warm welcome email.</p>'
    )
    html_body = render_email_html(
        body_html, build_artist_footer(), PLATFORM_BG, PLATFORM_CARD_BG, PLATFORM_ACCENT
    )

    ok = await send_email(CLIENT_NOTIFY_EMAIL, subject, html_body, text_body)
    if not ok:
        log.error("New-fan notification email failed to send (source=%s)", source)


async def send_fan_welcome_email(fan_email, first_name, unsubscribe_token):
    """Fired (not awaited) right alongside the client notification. Warm,
    first-person, from the artist directly - thanks the fan by name and
    says what they'll get, without overpromising just releases (this
    list will also carry things like client updates down the line)."""
    subject = f"Hello, {first_name}"
    unsubscribe_url = f"{UNSUBSCRIBE_BASE_URL}?token={unsubscribe_token}"

    text_body = (
        f"Hey {first_name},\n\n"
        "Thank you so much for joining - it really means a lot to have you here.\n\n"
        "You'll get new music and updates from me, straight to your inbox, whenever "
        "there's something worth sharing.\n\n"
        f"Talk soon,\n{ARTIST_NAME}\n\n"
        f"There's more to explore - music, updates, and more - at {ARTIST_SITE_DISPLAY} "
        f"({ARTIST_SITE_URL}).\n\n"
        f"{UNSUB_FOOTNOTE}\n"
        f"Unsubscribe: {unsubscribe_url}"
    )
    body_html = (
        f"<p style=\"margin:0 0 16px;\">Hey {html.escape(first_name)},</p>"
        '<p style="margin:0 0 16px;">Thank you so much for joining - it really means a lot to have you here.</p>'
        '<p style="margin:0 0 24px;">You&rsquo;ll get new music and updates from me, straight to your inbox, '
        "whenever there&rsquo;s something worth sharing.</p>"
        f'<p style="margin:0;">Talk soon,<br/>{html.escape(ARTIST_NAME)}</p>'
    )
    html_body = render_email_html(
        body_html, build_fan_footer(unsubscribe_url), ARTIST_BG, ARTIST_CARD_BG, ARTIST_ACCENT
    )

    ok = await send_email(fan_email, subject, html_body, text_body)
    if not ok:
        log.error("Fan welcome email failed to send")


UNSUBSCRIBE_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Unsubscribed | Moceol</title>
<style>
  body { background:#003f42; color:#fff; font-family:Tahoma,Geneva,sans-serif;
    display:flex; align-items:center; justify-content:center; min-height:100vh;
    margin:0; padding:24px; text-align:center; }
  .card { max-width:420px; }
  h1 { color:#f2aa4c; font-family:Helvetica,Arial,sans-serif; font-size:26px; margin-bottom:14px; }
  p { color:rgb(190,188,188); line-height:1.5; }
</style></head>
<body><div class="card"><h1>You've been unsubscribed.</h1>
<p>You won't get any more drop alerts from Moceol. Sorry to see you go.</p>
</div></body></html>"""


def _cors_headers(request):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Vary": "Origin",
        }
    return {}


def _client_ip(request):
    # Only Caddy (on localhost) can reach this service - LISTEN_HOST is
    # 127.0.0.1 and nothing else is routed to it - so a forwarded header
    # here is trustworthy rather than attacker-spoofable.
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


async def handle_options(request):
    return web.Response(status=204, headers=_cors_headers(request))


async def handle_subscribe(request):
    headers = _cors_headers(request)
    ip = _client_ip(request)

    if _rate_limited(ip):
        return web.json_response({"ok": False, "error": "rate_limited"}, status=429, headers=headers)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400, headers=headers)

    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "invalid_body"}, status=400, headers=headers)

    if (body.get("website") or "").strip():
        log.info("Honeypot tripped (ip=%s) - discarded, no row written", ip)
        return web.json_response({"ok": True}, headers=headers)

    email = (body.get("email") or "").strip().lower()
    source = (body.get("source") or "unknown").strip()[:MAX_SOURCE_LEN]
    first_name = (body.get("first_name") or "").strip()[:MAX_NAME_LEN]
    last_name = (body.get("last_name") or "").strip()[:MAX_NAME_LEN]

    if not email or len(email) > MAX_EMAIL_LEN or not EMAIL_RE.match(email):
        return web.json_response({"ok": False, "error": "invalid_email"}, status=400, headers=headers)
    if not first_name:
        return web.json_response({"ok": False, "error": "missing_first_name"}, status=400, headers=headers)

    created_at = datetime.now(timezone.utc).isoformat()
    token = secrets.token_urlsafe(24)
    conn = sqlite3.connect(DB_PATH)
    is_new_row = False
    try:
        conn.execute(
            "INSERT INTO subscribers (email, created_at, source, unsubscribe_token, first_name, last_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (email, created_at, source, token, first_name, last_name),
        )
        conn.commit()
        is_new_row = True
        log.info("New signup accepted (source=%s)", source)
    except sqlite3.IntegrityError:
        log.info("Duplicate signup ignored (source=%s)", source)
    finally:
        conn.close()

    if is_new_row:
        fire_and_forget(notify_client_of_new_fan(email, first_name, last_name, created_at, source))
        fire_and_forget(send_fan_welcome_email(email, first_name, token))

    return web.json_response({"ok": True}, headers=headers)


async def handle_unsubscribe(request):
    token = request.query.get("token", "")
    if token:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute("DELETE FROM subscribers WHERE unsubscribe_token = ?", (token,))
            conn.commit()
            if cur.rowcount:
                log.info("Unsubscribe processed")
        finally:
            conn.close()
    return web.Response(text=UNSUBSCRIBE_PAGE, content_type="text/html")


async def handle_health(request):
    return web.json_response({"ok": True})


def main():
    init_db()
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_post("/subscribe", handle_subscribe)
    app.router.add_options("/subscribe", handle_options)
    # allow_head=False: a security scanner's HEAD request to preview this
    # link must not be able to trigger the unsubscribe itself.
    app.router.add_get("/unsubscribe", handle_unsubscribe, allow_head=False)
    app.router.add_get("/health", handle_health)
    log.info("Subscribe service starting on %s:%s", LISTEN_HOST, LISTEN_PORT)
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()
