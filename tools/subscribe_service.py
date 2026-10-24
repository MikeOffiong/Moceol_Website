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

POST /intake  (full Stage 1 field set - see handle_intake)
  - The services-page build form. Same honeypot, validation, and rate
    limit as /subscribe. Every submission fires an async alert email to
    CLIENT_NOTIFY_EMAIL plus a Telegram ping - no welcome email back to
    the sender, since this isn't a mailing-list signup, just a one-time
    build request.

POST /intake-photos  multipart: client_id, kind (portrait|gallery), file
  - Full Website clients' portrait + gallery uploads, correlated to their
    /intake row by client_id. JPEG/PNG only (checked by magic bytes, not
    just the browser-supplied Content-Type), capped per file, capped at
    10 gallery photos per client. Stored under INTAKE_UPLOADS_DIR, one
    subdirectory per client_id - never touches the DB directly.

GET /live-count
  - Public read of how many client sites are currently marked live (see
    tools/manage_clients.py, run by hand - marking a site live or paused
    is still a manual decision). Drives the "built for N artists" line on
    services.html once it reaches 2.

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
URL_RE = re.compile(r"^https?://[^\s]+\.[^\s]+$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.I)
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
WHATSAPP_RE = re.compile(r"^[+0-9()\-\s]{7,20}$")

MAX_EMAIL_LEN = 254
MAX_SOURCE_LEN = 64

MAX_NAME_LEN = 100
MAX_LINKS_LEN = 500
MAX_MESSAGE_LEN = 2000
MAX_URL_LEN = 300
MAX_WHATSAPP_LEN = 20
MAX_DOMAIN_LEN = 253
MAX_BIO_LEN = 2000
MAX_SUPPORT_TEXT_LEN = 300

# Photo uploads (/intake-photos) are the only requests allowed to be large -
# this is the Application-wide ceiling, with headroom over PHOTO_MAX_BYTES
# for multipart boundary overhead. The small JSON endpoints never get close.
PHOTO_MAX_BYTES = 6 * 1024 * 1024
APP_MAX_BODY_BYTES = PHOTO_MAX_BYTES + 2 * 1024 * 1024
MAX_GALLERY_PHOTOS = 10
PHOTO_KINDS = {"portrait", "gallery"}
UPLOADS_DIR = os.environ.get("INTAKE_UPLOADS_DIR", "/root/moceol_uploads")
UPLOAD_RATE_LIMIT_MAX = int(os.environ.get("UPLOAD_RATE_LIMIT_MAX", "30"))

PLATFORM_URL_COLUMNS = [
    "apple_music_url", "youtube_url", "amazon_music_url", "facebook_url",
    "x_url", "tiktok_url", "instagram_url", "audiomack_url", "boomplay_url",
]

INTAKE_TIER_LABELS = {
    "landing": "Landing Page",
    "full": "Full Website",
}

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
CLIENT_NOTIFY_EMAIL = os.environ.get("CLIENT_NOTIFY_EMAIL", "moceol.ent@gmail.com")
TELEGRAM_NOTIFY_BOT_TOKEN = os.environ.get("TELEGRAM_NOTIFY_BOT_TOKEN", "")
TELEGRAM_NOTIFY_CHAT_ID = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID", "")

EVENT_TYPES = {"page_visit", "video_played", "form_started", "form_submitted"}
MAX_EVENT_EXTRA_LEN = 500
EVENT_RATE_LIMIT_MAX = int(os.environ.get("EVENT_RATE_LIMIT_MAX", "40"))

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


# Separate, more generous limiter for /event: one real visitor can easily
# fire 3-4 of these (page_visit, video_played, form_started, form_submitted)
# in a single session, which the stricter form-submission limit above would
# wrongly choke off.
_event_hits = {}


def _event_rate_limited(ip):
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    hits = [t for t in _event_hits.get(ip, []) if t > window_start]
    hits.append(now)
    _event_hits[ip] = hits
    if len(_event_hits) > 5000:
        for key in list(_event_hits.keys()):
            _event_hits[key] = [t for t in _event_hits[key] if t > window_start]
            if not _event_hits[key]:
                del _event_hits[key]
    return len(hits) > EVENT_RATE_LIMIT_MAX


# A third limiter for /intake-photos: generous enough for one portrait plus
# up to 10 gallery photos (with a retry or two) in a single sitting.
_upload_hits = {}


def _upload_rate_limited(ip):
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    hits = [t for t in _upload_hits.get(ip, []) if t > window_start]
    hits.append(now)
    _upload_hits[ip] = hits
    if len(_upload_hits) > 5000:
        for key in list(_upload_hits.keys()):
            _upload_hits[key] = [t for t in _upload_hits[key] if t > window_start]
            if not _upload_hits[key]:
                del _upload_hits[key]
    return len(hits) > UPLOAD_RATE_LIMIT_MAX


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
    # Services-page build-form submissions. `name` and `links`/`message` are
    # kept for older rows' sake but no longer written to - artist_name is now
    # the canonical name field (also copied into `name` so the original NOT
    # NULL constraint stays satisfied without a destructive migration).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS intake_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            tier TEXT,
            links TEXT,
            message TEXT,
            created_at TEXT NOT NULL,
            client_id TEXT
        )
        """
    )
    intake_cols = [row[1] for row in conn.execute("PRAGMA table_info(intake_submissions)")]
    new_intake_columns = {
        "client_id": "TEXT",
        "whatsapp": "TEXT",
        "bio": "TEXT",
        "spotify_url": "TEXT",
        "brand_color": "TEXT",
        "domain_primary": "TEXT",
        "domain_backup": "TEXT",
        "fan_alert_email": "TEXT",
        "apple_music_url": "TEXT",
        "youtube_url": "TEXT",
        "amazon_music_url": "TEXT",
        "facebook_url": "TEXT",
        "x_url": "TEXT",
        "tiktok_url": "TEXT",
        "instagram_url": "TEXT",
        "audiomack_url": "TEXT",
        "boomplay_url": "TEXT",
        "gumroad_mode": "TEXT",
        "gumroad_handle": "TEXT",
        "support_enabled": "INTEGER",
        "support_link": "TEXT",
        "support_text": "TEXT",
    }
    for col, col_type in new_intake_columns.items():
        if col not in intake_cols:
            conn.execute(f"ALTER TABLE intake_submissions ADD COLUMN {col} {col_type}")

    # Client sites Moceol has marked live - see tools/manage_clients.py.
    # Drives the services.html "built for N artists" line (GET /live-count).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            artist_name TEXT NOT NULL,
            live INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )

    # Append-only funnel event log - page_visit, video_played, form_started,
    # form_submitted. Nothing in this codebase ever UPDATEs or DELETEs a row
    # here; every step is a fresh timestamped fact, kept as-is permanently
    # (subject to the backup/retention policy, not row-level deletion).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            page TEXT,
            created_at TEXT NOT NULL,
            country TEXT,
            city TEXT,
            device_type TEXT,
            traffic_source TEXT,
            extra TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_client_id ON events(client_id)")
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


async def send_telegram_notification(text):
    """Fire-and-forget DM to the owner, reusing the same bot/token the
    release bot already uses to message OWNER_USER_ID - no new bot to
    create or authorize."""
    if not TELEGRAM_NOTIFY_BOT_TOKEN or not TELEGRAM_NOTIFY_CHAT_ID:
        log.error("TELEGRAM_NOTIFY_BOT_TOKEN/CHAT_ID not set - cannot send Telegram notification")
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{TELEGRAM_NOTIFY_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_NOTIFY_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    body_text = await resp.text()
                    log.error("Telegram notify failed (status=%s): %s", resp.status, body_text[:200])
                    return False
                return True
    except Exception as e:
        log.error("Telegram notify raised: %s", e)
        return False


# ---------------------------------------------------------------------------
# GeoIP + device/traffic parsing for the event log. Cached by IP so a single
# visitor's several events (page_visit, video_played, ...) only trigger one
# lookup, and so we don't lean on a free-tier external API harder than
# necessary.
# ---------------------------------------------------------------------------
_geo_cache = {}


async def _lookup_geo(ip):
    if not ip or ip in ("unknown", "127.0.0.1"):
        return ("", "")
    if ip in _geo_cache:
        return _geo_cache[ip]
    country, city = "", ""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,city"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "success":
                        country = data.get("country") or ""
                        city = data.get("city") or ""
    except Exception as e:
        log.error("GeoIP lookup failed: %s", e)
    if len(_geo_cache) > 5000:
        _geo_cache.clear()
    _geo_cache[ip] = (country, city)
    return (country, city)


def _device_type(user_agent):
    ua = (user_agent or "").lower()
    if "ipad" in ua or "tablet" in ua or ("android" in ua and "mobile" not in ua):
        return "tablet"
    if "mobi" in ua or "iphone" in ua or "android" in ua:
        return "mobile"
    return "desktop"


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


async def notify_client_of_new_inquiry(fields, created_at_iso):
    """Fired (not awaited) right after an intake submission is saved. Uses
    the platform palette, same as the new-fan alert - this is an internal
    notification to the operator, not something a fan ever sees."""
    artist_name = fields["artist_name"]
    tier_label = INTAKE_TIER_LABELS.get(fields["tier"], fields["tier"])
    subject = f"New build request: {artist_name}"

    submitted_dt = datetime.fromisoformat(created_at_iso)
    submitted_display = submitted_dt.strftime("%B %d, %Y at %I:%M %p UTC")

    row_specs = [
        ("Tier", tier_label),
        ("Contact email", fields["contact_email"]),
        ("WhatsApp", fields["whatsapp"]),
        ("Spotify", fields["spotify_url"]),
        ("Brand color", fields["brand_color"]),
        ("Domain (preferred)", fields["domain_primary"]),
        ("Domain (backup)", fields["domain_backup"]),
        ("New-fan alert email", fields["fan_alert_email"]),
    ]
    for col in PLATFORM_URL_COLUMNS:
        if fields.get(col):
            row_specs.append((col.replace("_url", "").replace("_", " ").title(), fields[col]))
    if fields["tier"] == "full":
        row_specs.append(("Bio", fields["bio"]))
        gumroad_display = fields["gumroad_handle"] if fields["gumroad_mode"] == "own" else "Set one up with their contact email"
        row_specs.append(("Gumroad", gumroad_display))
        if fields["support_enabled"]:
            row_specs.append(("Support link", fields["support_link"]))
            row_specs.append(("Support line", fields["support_text"]))
    row_specs.append(("Submitted", submitted_display))

    text_lines = [f"New services-page build request from {artist_name}.", ""]
    for label, value in row_specs:
        text_lines.append(f"{label}: {value}")
    text_lines.extend(["", f"Powered by {ARTIST_NAME}"])
    text_body = "\n".join(text_lines)

    rows_html = "".join(
        f'<tr><td style="padding:3px 0; width:150px; color:{BRAND_MUTED}; vertical-align:top;">{html.escape(label)}</td>'
        f'<td style="padding:3px 0;">{html.escape(str(value))}</td></tr>'
        for label, value in row_specs
    )

    body_html = (
        '<p style="margin:0 0 16px; font-size:19px; font-weight:bold;">New build request \U0001F4EC</p>'
        f'<p style="margin:0 0 20px;"><strong>{html.escape(artist_name)}</strong> just submitted the services-page form.</p>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%; margin:0 0 20px;">'
        f"{rows_html}"
        "</table>"
    )
    html_body = render_email_html(
        body_html, build_artist_footer(), PLATFORM_BG, PLATFORM_CARD_BG, PLATFORM_ACCENT
    )

    ok = await send_email(CLIENT_NOTIFY_EMAIL, subject, html_body, text_body)
    if not ok:
        log.error("Inquiry notification email failed to send")


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


async def handle_intake(request):
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
        log.info("Intake honeypot tripped (ip=%s) - discarded, no row written", ip)
        return web.json_response({"ok": True}, headers=headers)

    def field(key, max_len=MAX_NAME_LEN):
        return (body.get(key) or "").strip()[:max_len]

    artist_name = field("artist_name")
    contact_email = field("contact_email", MAX_EMAIL_LEN).lower()
    whatsapp = field("whatsapp", MAX_WHATSAPP_LEN)
    spotify_url = field("spotify_url", MAX_URL_LEN)
    brand_color = field("brand_color", 10)
    domain_primary = field("domain_primary", MAX_DOMAIN_LEN).lower()
    domain_backup = field("domain_backup", MAX_DOMAIN_LEN).lower()
    tier = field("tier", 10)
    fan_alert_email = field("fan_alert_email", MAX_EMAIL_LEN).lower()
    client_id = field("client_id", 64)

    platform_urls = {col: field(col, MAX_URL_LEN) for col in PLATFORM_URL_COLUMNS}

    bio = field("bio", MAX_BIO_LEN)
    gumroad_mode = field("gumroad_mode", 10)
    gumroad_handle = field("gumroad_handle", MAX_URL_LEN)
    support_enabled = bool(body.get("support_enabled"))
    support_link = field("support_link", MAX_URL_LEN)
    support_text = field("support_text", MAX_SUPPORT_TEXT_LEN)

    if not artist_name:
        return web.json_response({"ok": False, "error": "missing_artist_name"}, status=400, headers=headers)
    if not contact_email or len(contact_email) > MAX_EMAIL_LEN or not EMAIL_RE.match(contact_email):
        return web.json_response({"ok": False, "error": "invalid_contact_email"}, status=400, headers=headers)
    if not WHATSAPP_RE.match(whatsapp):
        return web.json_response({"ok": False, "error": "invalid_whatsapp"}, status=400, headers=headers)
    if not URL_RE.match(spotify_url):
        return web.json_response({"ok": False, "error": "invalid_spotify_url"}, status=400, headers=headers)
    if not COLOR_RE.match(brand_color):
        return web.json_response({"ok": False, "error": "invalid_brand_color"}, status=400, headers=headers)
    if not DOMAIN_RE.match(domain_primary) or not DOMAIN_RE.match(domain_backup):
        return web.json_response({"ok": False, "error": "invalid_domain"}, status=400, headers=headers)
    if tier not in INTAKE_TIER_LABELS:
        return web.json_response({"ok": False, "error": "invalid_tier"}, status=400, headers=headers)
    if not fan_alert_email or not EMAIL_RE.match(fan_alert_email):
        return web.json_response({"ok": False, "error": "invalid_fan_alert_email"}, status=400, headers=headers)
    for col, value in platform_urls.items():
        if value and not URL_RE.match(value):
            return web.json_response({"ok": False, "error": f"invalid_{col}"}, status=400, headers=headers)

    if tier == "full":
        if not bio:
            return web.json_response({"ok": False, "error": "missing_bio"}, status=400, headers=headers)
        if gumroad_mode not in ("own", "setup"):
            return web.json_response({"ok": False, "error": "invalid_gumroad_mode"}, status=400, headers=headers)
        if gumroad_mode == "own" and not gumroad_handle:
            return web.json_response({"ok": False, "error": "missing_gumroad_handle"}, status=400, headers=headers)
        if support_enabled:
            if not support_link or not URL_RE.match(support_link):
                return web.json_response({"ok": False, "error": "invalid_support_link"}, status=400, headers=headers)
            if not support_text:
                return web.json_response({"ok": False, "error": "missing_support_text"}, status=400, headers=headers)
        else:
            support_link = ""
            support_text = ""
    else:
        bio = ""
        gumroad_mode = ""
        gumroad_handle = ""
        support_enabled = False
        support_link = ""
        support_text = ""

    created_at = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO intake_submissions "
            "(name, email, artist_name, tier, created_at, client_id, whatsapp, bio, spotify_url, "
            "brand_color, domain_primary, domain_backup, fan_alert_email, "
            "apple_music_url, youtube_url, amazon_music_url, facebook_url, x_url, tiktok_url, "
            "instagram_url, audiomack_url, boomplay_url, gumroad_mode, gumroad_handle, "
            "support_enabled, support_link, support_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artist_name, contact_email, artist_name, tier, created_at, client_id, whatsapp, bio, spotify_url,
                brand_color, domain_primary, domain_backup, fan_alert_email,
                platform_urls["apple_music_url"], platform_urls["youtube_url"], platform_urls["amazon_music_url"],
                platform_urls["facebook_url"], platform_urls["x_url"], platform_urls["tiktok_url"],
                platform_urls["instagram_url"], platform_urls["audiomack_url"], platform_urls["boomplay_url"],
                gumroad_mode, gumroad_handle, int(support_enabled), support_link, support_text,
            ),
        )
        conn.commit()
        log.info("New intake submission accepted (tier=%s)", tier)
    finally:
        conn.close()

    fields = {
        "artist_name": artist_name,
        "contact_email": contact_email,
        "whatsapp": whatsapp,
        "spotify_url": spotify_url,
        "brand_color": brand_color,
        "domain_primary": domain_primary,
        "domain_backup": domain_backup,
        "tier": tier,
        "fan_alert_email": fan_alert_email,
        "bio": bio,
        "gumroad_mode": gumroad_mode,
        "gumroad_handle": gumroad_handle,
        "support_enabled": support_enabled,
        "support_link": support_link,
        "support_text": support_text,
        **platform_urls,
    }
    fire_and_forget(notify_client_of_new_inquiry(fields, created_at))
    tier_label = INTAKE_TIER_LABELS.get(tier, tier)
    fire_and_forget(send_telegram_notification(
        f"\U0001F4EC New services inquiry\n<b>{html.escape(artist_name)}</b> ({html.escape(tier_label)})"
    ))

    return web.json_response({"ok": True}, headers=headers)


def _sniff_image_ext(head_bytes):
    if head_bytes[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


async def handle_intake_photos(request):
    headers = _cors_headers(request)
    ip = _client_ip(request)

    if _upload_rate_limited(ip):
        return web.json_response({"ok": False, "error": "rate_limited"}, status=429, headers=headers)

    try:
        reader = await request.multipart()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_multipart"}, status=400, headers=headers)

    client_id = ""
    kind = ""
    file_field = None

    field = await reader.next()
    while field is not None:
        if field.name == "client_id":
            client_id = ((await field.text()) or "").strip()[:64]
        elif field.name == "kind":
            kind = ((await field.text()) or "").strip()
        elif field.name == "file":
            file_field = field
            break
        field = await reader.next()

    if not client_id or kind not in PHOTO_KINDS or file_field is None:
        return web.json_response({"ok": False, "error": "invalid_fields"}, status=400, headers=headers)

    safe_client_id = re.sub(r"[^a-zA-Z0-9_-]", "_", client_id)
    client_dir = os.path.join(UPLOADS_DIR, safe_client_id)
    os.makedirs(client_dir, exist_ok=True)

    if kind == "gallery":
        existing = [f for f in os.listdir(client_dir) if f.startswith("gallery-")]
        if len(existing) >= MAX_GALLERY_PHOTOS:
            return web.json_response({"ok": False, "error": "gallery_full"}, status=400, headers=headers)

    chunks = []
    total = 0
    chunk = await file_field.read_chunk(size=65536)
    while chunk:
        total += len(chunk)
        if total > PHOTO_MAX_BYTES:
            return web.json_response({"ok": False, "error": "file_too_large"}, status=413, headers=headers)
        chunks.append(chunk)
        chunk = await file_field.read_chunk(size=65536)

    data = b"".join(chunks)
    if not data:
        return web.json_response({"ok": False, "error": "empty_file"}, status=400, headers=headers)

    ext = _sniff_image_ext(data[:16])
    if not ext:
        return web.json_response({"ok": False, "error": "invalid_image"}, status=400, headers=headers)

    if kind == "portrait":
        for old_ext in ("jpg", "png"):
            old_path = os.path.join(client_dir, "portrait." + old_ext)
            if os.path.exists(old_path):
                os.remove(old_path)
        filename = "portrait." + ext
    else:
        existing_numbers = []
        for f in os.listdir(client_dir):
            match = re.match(r"gallery-(\d+)\.", f)
            if match:
                existing_numbers.append(int(match.group(1)))
        next_number = (max(existing_numbers) + 1) if existing_numbers else 1
        filename = f"gallery-{next_number}.{ext}"

    with open(os.path.join(client_dir, filename), "wb") as out_file:
        out_file.write(data)

    gallery_count = len([f for f in os.listdir(client_dir) if f.startswith("gallery-")])
    log.info("Photo upload accepted (kind=%s, bytes=%d)", kind, total)

    return web.json_response(
        {"ok": True, "filename": filename, "gallery_count": gallery_count}, headers=headers
    )


async def handle_live_count(request):
    headers = _cors_headers(request)
    conn = sqlite3.connect(DB_PATH)
    try:
        count = conn.execute("SELECT COUNT(*) FROM clients WHERE live = 1").fetchone()[0]
    finally:
        conn.close()
    return web.json_response({"count": count}, headers=headers)


async def handle_event(request):
    headers = _cors_headers(request)
    ip = _client_ip(request)

    if _event_rate_limited(ip):
        return web.json_response({"ok": False, "error": "rate_limited"}, status=429, headers=headers)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400, headers=headers)

    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "invalid_body"}, status=400, headers=headers)

    client_id = (body.get("client_id") or "").strip()[:64]
    event_type = (body.get("event_type") or "").strip()
    page = (body.get("page") or "").strip()[:100]
    traffic_source = (body.get("traffic_source") or "").strip()[:200]
    extra = (body.get("extra") or "").strip()[:MAX_EVENT_EXTRA_LEN]

    if not client_id or event_type not in EVENT_TYPES:
        return web.json_response({"ok": False, "error": "invalid_event"}, status=400, headers=headers)

    country, city = await _lookup_geo(ip)
    device_type = _device_type(request.headers.get("User-Agent", ""))
    created_at = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO events (client_id, event_type, page, created_at, country, city, device_type, traffic_source, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (client_id, event_type, page, created_at, country, city, device_type, traffic_source, extra),
        )
        conn.commit()
    finally:
        conn.close()

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
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    app = web.Application(client_max_size=APP_MAX_BODY_BYTES)
    app.router.add_post("/subscribe", handle_subscribe)
    app.router.add_options("/subscribe", handle_options)
    app.router.add_post("/intake", handle_intake)
    app.router.add_options("/intake", handle_options)
    app.router.add_post("/intake-photos", handle_intake_photos)
    app.router.add_options("/intake-photos", handle_options)
    app.router.add_post("/event", handle_event)
    app.router.add_options("/event", handle_options)
    app.router.add_get("/live-count", handle_live_count)
    # allow_head=False: a security scanner's HEAD request to preview this
    # link must not be able to trigger the unsubscribe itself.
    app.router.add_get("/unsubscribe", handle_unsubscribe, allow_head=False)
    app.router.add_get("/health", handle_health)
    log.info("Subscribe service starting on %s:%s", LISTEN_HOST, LISTEN_PORT)
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()
