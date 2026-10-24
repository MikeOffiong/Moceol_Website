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
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

import preview_builder
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

EVENT_TYPES = {
    "page_visit", "video_played", "form_started", "form_submitted",
    "preview_created", "preview_opened", "preview_expired",
    "preview_refresh_requested", "preview_refresh_limit_reached",
}
MAX_EVENT_EXTRA_LEN = 500
EVENT_RATE_LIMIT_MAX = int(os.environ.get("EVENT_RATE_LIMIT_MAX", "40"))

# --- Stage A: automatic preview (see preview_builder.py) ---
PREVIEWS_DIR = os.environ.get("PREVIEWS_DIR", "/root/moceol_previews")
PREVIEW_HOURS = 24
PREVIEW_PUBLIC_BASE = os.environ.get("PREVIEW_PUBLIC_BASE", "https://api.moceol.com/preview")

# --- Fraud protection: lifetime caps on how many preview-generating
# submissions a single identity can make. IP gets more headroom since
# carrier-grade NAT means several real artists can share one. Exceeding a
# cap rejects the submission outright (no preview, no row written); staying
# under the cap but submitting again just gets flagged in the Telegram
# alert so Moceol can see repeat submitters without blocking them. ---
MAX_SUBMISSIONS_PER_EMAIL = int(os.environ.get("MAX_SUBMISSIONS_PER_EMAIL", "3"))
MAX_SUBMISSIONS_PER_WHATSAPP = int(os.environ.get("MAX_SUBMISSIONS_PER_WHATSAPP", "3"))
MAX_SUBMISSIONS_PER_IP = int(os.environ.get("MAX_SUBMISSIONS_PER_IP", "10"))
SPOTIFY_ARTIST_LINK_RE = re.compile(r"artist[/:][a-zA-Z0-9]+")

# --- Owner dashboard: long unguessable URL is the only auth, no login.
# Generated once and persisted in the env file (not regenerated per
# restart, so the bookmarked link keeps working). ---
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")

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
        "main_color": "TEXT",
        "accent_color": "TEXT",
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
        "other_payment_link": "TEXT",
        "support_enabled": "INTEGER",
        "support_link": "TEXT",
        "support_text": "TEXT",
        "ip_address": "TEXT",
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

    # Stage A automatic previews. is_refresh marks the (at most one) refresh
    # generated for a submission; COUNT(*) WHERE submission_id = ? is how the
    # one-refresh-per-submission cap and the expired-state message are
    # decided - never a separate counter column to keep in sync.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS previews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            tier TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            is_refresh INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_previews_submission ON previews(submission_id)")

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
        ("Main color", fields["main_color"]),
        ("Accent color", fields["accent_color"]),
        ("Domain (preferred)", fields["domain_primary"]),
        ("Domain (backup)", fields["domain_backup"]),
        ("New-fan alert email", fields["fan_alert_email"]),
    ]
    for col in PLATFORM_URL_COLUMNS:
        if fields.get(col):
            row_specs.append((col.replace("_url", "").replace("_", " ").title(), fields[col]))
    if fields["tier"] == "full":
        row_specs.append(("Bio", fields["bio"]))
        payment_mode = fields["gumroad_mode"]
        if payment_mode == "own":
            payment_display = f'Gumroad - {fields["gumroad_handle"]}'
        elif payment_mode == "other":
            payment_display = f'Other link - {fields["other_payment_link"]}'
        else:
            payment_display = "Set one up with their contact email"
        row_specs.append(("Where fans buy/support", payment_display))
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


PREVIEW_EXPIRED_REFRESH_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="robots" content="noindex, nofollow" />
<title>Preview Expired | Moceol</title>
<style>
  body { background:#003f42; color:#fff; font-family:Tahoma,Geneva,sans-serif;
    display:flex; align-items:center; justify-content:center; min-height:100vh;
    margin:0; padding:24px; text-align:center; }
  .card { max-width:420px; }
  h1 { color:#f2aa4c; font-family:Helvetica,Arial,sans-serif; font-size:24px; margin-bottom:14px; }
  p { color:rgb(190,188,188); line-height:1.5; margin-bottom:20px; }
  button { background:#f2aa4c; color:#000; border:none; border-radius:6px; padding:12px 22px;
    font-family:Helvetica,Arial,sans-serif; font-weight:bold; font-size:14px; cursor:pointer; }
  button:disabled { opacity:0.6; cursor:default; }
  .err { color:#ff8a8a; font-size:13px; margin-top:14px; }
</style></head>
<body><div class="card">
<h1>This preview has expired.</h1>
<p>Previews stay live for 24 hours. You get one fresh link - request it below.</p>
<button id="refresh-btn" type="button">Request a new preview</button>
<p class="err" id="refresh-err" hidden></p>
</div>
<script>
document.getElementById("refresh-btn").addEventListener("click", function () {
  var btn = this;
  btn.disabled = true;
  btn.textContent = "Building your new preview...";
  fetch(window.location.pathname.replace(/\\/$/, "") + "/refresh", { method: "POST" })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data && data.ok && data.url) {
        window.location.href = data.url;
      } else {
        document.getElementById("refresh-err").textContent = "That didn't work - please try again, or email moceol.ent@gmail.com.";
        document.getElementById("refresh-err").hidden = false;
        btn.disabled = false;
        btn.textContent = "Request a new preview";
      }
    })
    .catch(function () {
      document.getElementById("refresh-err").textContent = "That didn't work - please try again, or email moceol.ent@gmail.com.";
      document.getElementById("refresh-err").hidden = false;
      btn.disabled = false;
      btn.textContent = "Request a new preview";
    });
});
</script>
</body></html>"""

PREVIEW_SUPPORT_REACHOUT_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="robots" content="noindex, nofollow" />
<title>Preview Expired | Moceol</title>
<style>
  body { background:#003f42; color:#fff; font-family:Tahoma,Geneva,sans-serif;
    display:flex; align-items:center; justify-content:center; min-height:100vh;
    margin:0; padding:24px; text-align:center; }
  .card { max-width:420px; }
  h1 { color:#f2aa4c; font-family:Helvetica,Arial,sans-serif; font-size:24px; margin-bottom:14px; }
  p { color:rgb(190,188,188); line-height:1.5; margin-bottom:20px; }
  .icons { display:flex; justify-content:center; gap:22px; }
  .icons a { display:flex; align-items:center; justify-content:center; width:52px; height:52px;
    border-radius:50%; border:1px solid #f2aa4c; color:#f2aa4c; text-decoration:none; font-size:22px; }
</style></head>
<body><div class="card">
<h1>Your preview has expired.</h1>
<p>Reach out to support to get started.</p>
<div class="icons">
  <a href="mailto:moceol.ent@gmail.com" aria-label="Email">&#9993;</a>
  <a href="https://wa.me/447729885183" target="_blank" rel="noopener noreferrer" aria-label="WhatsApp">&#9742;</a>
</div>
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


# ---------------------------------------------------------------------------
# Stage A: automatic preview generation
# ---------------------------------------------------------------------------
def _log_event_sync(conn, client_id, event_type, page="", extra=""):
    """Server-side event insert for preview lifecycle events - no geo/device
    lookup (these aren't always tied to a fresh incoming request the way
    the public /event endpoint's page_visit etc. are)."""
    conn.execute(
        "INSERT INTO events (client_id, event_type, page, created_at, country, city, device_type, traffic_source, extra) "
        "VALUES (?, ?, ?, ?, '', '', '', '', ?)",
        (client_id, event_type, page, datetime.now(timezone.utc).isoformat(), extra[:MAX_EVENT_EXTRA_LEN]),
    )


def _submission_row_to_fields(row):
    """row is a sqlite3.Row from intake_submissions - reshapes it into the
    flat dict preview_builder.build_preview_files() expects, the same shape
    handle_intake() already builds fresh at submission time."""
    return {
        "artist_name": row["artist_name"],
        "tier": row["tier"],
        "main_color": row["main_color"] or "#003f42",
        "accent_color": row["accent_color"] or "#f2aa4c",
        "spotify_url": row["spotify_url"] or "",
        "apple_music_url": row["apple_music_url"] or "",
        "youtube_url": row["youtube_url"] or "",
        "amazon_music_url": row["amazon_music_url"] or "",
        "facebook_url": row["facebook_url"] or "",
        "x_url": row["x_url"] or "",
        "tiktok_url": row["tiktok_url"] or "",
        "instagram_url": row["instagram_url"] or "",
        "audiomack_url": row["audiomack_url"] or "",
        "boomplay_url": row["boomplay_url"] or "",
        "bio": row["bio"] or "",
        "gumroad_mode": row["gumroad_mode"] or "",
        "gumroad_handle": row["gumroad_handle"] or "",
        "other_payment_link": row["other_payment_link"] or "",
        "support_enabled": bool(row["support_enabled"]),
        "support_link": row["support_link"] or "",
        "support_text": row["support_text"] or "",
    }


async def generate_preview(submission_id, client_id, fields, is_refresh=False):
    """Runs the (blocking, network-calling) preview builder off the event
    loop, writes the previews row, and logs preview_created. Returns the
    full preview URL. Never raises - a failure here must not break the
    submission or refresh request that triggered it; it logs and returns
    None instead."""
    token = secrets.token_urlsafe(32)
    out_dir = os.path.join(PREVIEWS_DIR, token)
    try:
        loop = asyncio.get_event_loop()
        warnings = await loop.run_in_executor(
            None, preview_builder.build_preview_files, out_dir, token, fields, client_id
        )
        if warnings:
            log.info("Preview %s built with warnings: %s", token, "; ".join(warnings))
    except Exception as e:
        log.error("Preview generation failed for submission %s: %s", submission_id, e)
        return None

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=PREVIEW_HOURS)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO previews (submission_id, token, tier, created_at, expires_at, is_refresh) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (submission_id, token, fields["tier"], now.isoformat(), expires_at.isoformat(), int(is_refresh)),
        )
        _log_event_sync(conn, client_id, "preview_created", extra="refresh" if is_refresh else "original")
        conn.commit()
    finally:
        conn.close()

    return f"{PREVIEW_PUBLIC_BASE}/{token}"


async def send_preview_email(artist_name, contact_email, preview_url):
    subject = f"Your preview is ready, {artist_name}"
    text_body = (
        f"Hey {artist_name},\n\n"
        "Here's your private preview, built from what you just sent:\n"
        f"{preview_url}\n\n"
        "This link is just for you - it's private and expires in 24 hours. "
        "If it expires, there's a button on that page to request one fresh "
        "link (good for another 24 hours).\n\n"
        f"Talk soon,\n{ARTIST_NAME}"
    )
    body_html = (
        f'<p style="margin:0 0 16px;">Hey {html.escape(artist_name)},</p>'
        '<p style="margin:0 0 20px;">Here&rsquo;s your private preview, built from what you just sent:</p>'
        f'<p style="margin:0 0 20px;"><a href="{html.escape(preview_url)}" '
        f'style="color:{PLATFORM_ACCENT};">{html.escape(preview_url)}</a></p>'
        '<p style="margin:0;">This link is just for you - it&rsquo;s private and expires in 24 hours. '
        "If it expires, there&rsquo;s a button on that page to request one fresh link "
        "(good for another 24 hours).</p>"
    )
    html_body = render_email_html(body_html, build_artist_footer(), PLATFORM_BG, PLATFORM_CARD_BG, PLATFORM_ACCENT)
    ok = await send_email(contact_email, subject, html_body, text_body)
    if not ok:
        log.error("Preview email failed to send")


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
    main_color = field("main_color", 10)
    accent_color = field("accent_color", 10)
    domain_primary = field("domain_primary", MAX_DOMAIN_LEN).lower()
    domain_backup = field("domain_backup", MAX_DOMAIN_LEN).lower()
    tier = field("tier", 10)
    fan_alert_email = field("fan_alert_email", MAX_EMAIL_LEN).lower()
    client_id = field("client_id", 64)

    platform_urls = {col: field(col, MAX_URL_LEN) for col in PLATFORM_URL_COLUMNS}

    bio = field("bio", MAX_BIO_LEN)
    gumroad_mode = field("gumroad_mode", 10)
    gumroad_handle = field("gumroad_handle", MAX_URL_LEN)
    other_payment_link = field("other_payment_link", MAX_URL_LEN)
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
    if not COLOR_RE.match(main_color):
        return web.json_response({"ok": False, "error": "invalid_main_color"}, status=400, headers=headers)
    if not COLOR_RE.match(accent_color):
        return web.json_response({"ok": False, "error": "invalid_accent_color"}, status=400, headers=headers)
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
        if gumroad_mode not in ("own", "setup", "other"):
            return web.json_response({"ok": False, "error": "invalid_gumroad_mode"}, status=400, headers=headers)
        if gumroad_mode == "own" and not gumroad_handle:
            return web.json_response({"ok": False, "error": "missing_gumroad_handle"}, status=400, headers=headers)
        if gumroad_mode == "other":
            if not other_payment_link.startswith("https://") or not URL_RE.match(other_payment_link):
                return web.json_response({"ok": False, "error": "invalid_other_payment_link"}, status=400, headers=headers)
        else:
            other_payment_link = ""
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
        other_payment_link = ""
        support_enabled = False
        support_link = ""
        support_text = ""

    created_at = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        # Fraud protection: lifetime caps per identity, checked and inserted
        # inside the same connection with no awaits in between, so two
        # near-simultaneous submissions from the same identity can't both
        # slip past the check before either one is counted.
        email_count = conn.execute(
            "SELECT COUNT(*) FROM intake_submissions WHERE email = ?", (contact_email,)
        ).fetchone()[0]
        whatsapp_count = conn.execute(
            "SELECT COUNT(*) FROM intake_submissions WHERE whatsapp = ?", (whatsapp,)
        ).fetchone()[0]
        ip_count = conn.execute(
            "SELECT COUNT(*) FROM intake_submissions WHERE ip_address = ?", (ip,)
        ).fetchone()[0]
        if email_count >= MAX_SUBMISSIONS_PER_EMAIL:
            return web.json_response({"ok": False, "error": "too_many_submissions_email"}, status=429, headers=headers)
        if whatsapp_count >= MAX_SUBMISSIONS_PER_WHATSAPP:
            return web.json_response({"ok": False, "error": "too_many_submissions_whatsapp"}, status=429, headers=headers)
        if ip_count >= MAX_SUBMISSIONS_PER_IP:
            return web.json_response({"ok": False, "error": "too_many_submissions_ip"}, status=429, headers=headers)

        conn.execute(
            "INSERT INTO intake_submissions "
            "(name, email, artist_name, tier, created_at, client_id, whatsapp, bio, spotify_url, "
            "main_color, accent_color, domain_primary, domain_backup, fan_alert_email, "
            "apple_music_url, youtube_url, amazon_music_url, facebook_url, x_url, tiktok_url, "
            "instagram_url, audiomack_url, boomplay_url, gumroad_mode, gumroad_handle, other_payment_link, "
            "support_enabled, support_link, support_text, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artist_name, contact_email, artist_name, tier, created_at, client_id, whatsapp, bio, spotify_url,
                main_color, accent_color, domain_primary, domain_backup, fan_alert_email,
                platform_urls["apple_music_url"], platform_urls["youtube_url"], platform_urls["amazon_music_url"],
                platform_urls["facebook_url"], platform_urls["x_url"], platform_urls["tiktok_url"],
                platform_urls["instagram_url"], platform_urls["audiomack_url"], platform_urls["boomplay_url"],
                gumroad_mode, gumroad_handle, other_payment_link, int(support_enabled), support_link, support_text, ip,
            ),
        )
        conn.commit()
        submission_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log.info("New intake submission accepted (tier=%s)", tier)
    finally:
        conn.close()

    is_repeat = email_count > 0 or whatsapp_count > 0
    spotify_looks_invalid = not SPOTIFY_ARTIST_LINK_RE.search(spotify_url)

    fields = {
        "artist_name": artist_name,
        "contact_email": contact_email,
        "whatsapp": whatsapp,
        "spotify_url": spotify_url,
        "main_color": main_color,
        "accent_color": accent_color,
        "domain_primary": domain_primary,
        "domain_backup": domain_backup,
        "tier": tier,
        "fan_alert_email": fan_alert_email,
        "bio": bio,
        "gumroad_mode": gumroad_mode,
        "gumroad_handle": gumroad_handle,
        "other_payment_link": other_payment_link,
        "support_enabled": support_enabled,
        "support_link": support_link,
        "support_text": support_text,
        **platform_urls,
    }
    fire_and_forget(notify_client_of_new_inquiry(fields, created_at))
    tier_label = INTAKE_TIER_LABELS.get(tier, tier)
    alert_lines = [f"\U0001F4EC New services inquiry\n<b>{html.escape(artist_name)}</b> ({html.escape(tier_label)})"]
    if is_repeat:
        alert_lines.append(
            f"⚠️ Repeat submitter — this email seen {email_count + 1}x, "
            f"this WhatsApp number seen {whatsapp_count + 1}x before"
        )
    if spotify_looks_invalid:
        alert_lines.append("⚠️ Spotify link doesn't look like an artist page — worth a manual check")
    fire_and_forget(send_telegram_notification("\n".join(alert_lines)))

    preview_url = await generate_preview(submission_id, client_id, fields, is_refresh=False)
    if preview_url:
        fire_and_forget(send_preview_email(artist_name, contact_email, preview_url))

    return web.json_response({"ok": True, "preview_url": preview_url}, headers=headers)


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


DASHBOARD_WINDOW_DAYS = 30


def _dashboard_rows(conn):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DASHBOARD_WINDOW_DAYS)).isoformat()

    visits_by_day = conn.execute(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(DISTINCT client_id) AS n FROM events "
        "WHERE event_type = 'page_visit' AND created_at >= ? GROUP BY day ORDER BY day",
        (cutoff,),
    ).fetchall()

    devices = conn.execute(
        "SELECT device_type, COUNT(DISTINCT client_id) AS n FROM events "
        "WHERE event_type = 'page_visit' AND created_at >= ? AND device_type != '' "
        "GROUP BY device_type ORDER BY n DESC",
        (cutoff,),
    ).fetchall()

    cities = conn.execute(
        "SELECT city, COUNT(DISTINCT client_id) AS n FROM events "
        "WHERE event_type = 'page_visit' AND created_at >= ? AND city != '' "
        "GROUP BY city ORDER BY n DESC LIMIT 8",
        (cutoff,),
    ).fetchall()

    sources = conn.execute(
        "SELECT traffic_source, COUNT(DISTINCT client_id) AS n FROM events "
        "WHERE event_type = 'page_visit' AND created_at >= ? AND traffic_source != '' "
        "GROUP BY traffic_source ORDER BY n DESC LIMIT 8",
        (cutoff,),
    ).fetchall()

    def funnel_count(event_type):
        return conn.execute(
            "SELECT COUNT(DISTINCT client_id) FROM events WHERE event_type = ? AND created_at >= ?",
            (event_type, cutoff),
        ).fetchone()[0]

    funnel = {
        "visited": funnel_count("page_visit"),
        "started": funnel_count("form_started"),
        "submitted": funnel_count("form_submitted"),
    }

    submissions = conn.execute(
        "SELECT artist_name, tier, email, created_at FROM intake_submissions "
        "ORDER BY created_at DESC LIMIT 20"
    ).fetchall()

    return visits_by_day, devices, cities, sources, funnel, submissions


def _dashboard_bar_chart(rows, max_label_len=14):
    if not rows:
        return '<p class="empty">No data yet.</p>'
    max_n = max(n for _, n in rows) or 1
    bars = "".join(
        f'<div class="bar-row"><span class="bar-label">{html.escape(str(label)[:max_label_len])}</span>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{max(4, round(n / max_n * 100))}%"></div></div>'
        f'<span class="bar-value">{n}</span></div>'
        for label, n in rows
    )
    return f'<div class="bar-chart">{bars}</div>'


DASHBOARD_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="robots" content="noindex, nofollow" />
<title>Dashboard | Moceol</title>
<style>
  body {{ background:#003f42; color:#fff; font-family:Tahoma,Geneva,sans-serif;
    margin:0; padding:20px 16px 60px; }}
  h1 {{ color:#f2aa4c; font-family:Helvetica,Arial,sans-serif; font-size:22px; margin:0 0 4px; }}
  .subtitle {{ color:rgb(190,188,188); font-size:12px; margin:0 0 24px; }}
  .card {{ background:rgba(255,255,255,0.04); border:1px solid rgba(242,170,76,0.2);
    border-radius:10px; padding:16px; margin-bottom:16px; max-width:640px; }}
  .card h2 {{ color:#f2aa4c; font-family:Helvetica,Arial,sans-serif; font-size:15px; margin:0 0 12px; }}
  .empty {{ color:rgb(190,188,188); font-size:13px; margin:0; }}
  .bar-chart {{ display:flex; flex-direction:column; gap:6px; }}
  .bar-row {{ display:flex; align-items:center; gap:8px; font-size:12px; }}
  .bar-label {{ width:90px; flex-shrink:0; color:rgb(190,188,188); overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; }}
  .bar-track {{ flex:1; background:rgba(255,255,255,0.08); border-radius:4px; height:14px; overflow:hidden; }}
  .bar-fill {{ background:#f2aa4c; height:100%; border-radius:4px; }}
  .bar-value {{ width:28px; text-align:right; flex-shrink:0; }}
  .funnel {{ display:flex; justify-content:space-between; text-align:center; gap:8px; flex-wrap:wrap; }}
  .funnel div {{ flex:1; min-width:80px; }}
  .funnel .n {{ font-size:22px; font-weight:bold; color:#f2aa4c; }}
  .funnel .label {{ font-size:11px; color:rgb(190,188,188); }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; }}
  th, td {{ text-align:left; padding:6px 4px; border-bottom:1px solid rgba(255,255,255,0.08); }}
  th {{ color:rgb(190,188,188); font-weight:normal; }}
  .daychart {{ display:flex; align-items:flex-end; gap:2px; height:60px; }}
  .daychart .col {{ flex:1; background:#f2aa4c; border-radius:2px 2px 0 0; min-height:2px; }}
</style></head>
<body>
<h1>Moceol Dashboard</h1>
<p class="subtitle">Last {window_days} days &middot; generated {generated_at}</p>

<div class="card">
  <h2>Visits over time</h2>
  {day_chart}
</div>

<div class="card">
  <h2>Funnel</h2>
  <div class="funnel">
    <div><div class="n">{visited}</div><div class="label">VISITED</div></div>
    <div><div class="n">{started}</div><div class="label">STARTED</div></div>
    <div><div class="n">{submitted}</div><div class="label">SUBMITTED</div></div>
  </div>
</div>

<div class="card">
  <h2>Devices</h2>
  {devices_chart}
</div>

<div class="card">
  <h2>Top cities</h2>
  {cities_chart}
</div>

<div class="card">
  <h2>Top traffic sources</h2>
  {sources_chart}
</div>

<div class="card">
  <h2>Recent submissions</h2>
  {submissions_table}
</div>
</body></html>"""


def _dashboard_day_chart(visits_by_day):
    if not visits_by_day:
        return '<p class="empty">No visits yet.</p>'
    max_n = max(n for _, n in visits_by_day) or 1
    cols = "".join(
        f'<div class="col" style="height:{max(4, round(n / max_n * 100))}%" title="{html.escape(day)}: {n}"></div>'
        for day, n in visits_by_day
    )
    return f'<div class="daychart">{cols}</div>'


def _dashboard_submissions_table(submissions):
    if not submissions:
        return '<p class="empty">No submissions yet.</p>'
    rows_html = "".join(
        f"<tr><td>{html.escape(artist_name)}</td><td>{html.escape(INTAKE_TIER_LABELS.get(tier, tier or ''))}</td>"
        f"<td>{html.escape(email)}</td><td>{html.escape(created_at[:10])}</td></tr>"
        for artist_name, tier, email, created_at in submissions
    )
    return (
        "<table><tr><th>Artist</th><th>Tier</th><th>Email</th><th>Date</th></tr>"
        f"{rows_html}</table>"
    )


async def handle_dashboard(request):
    token = request.match_info.get("token", "")
    if not DASHBOARD_TOKEN or token != DASHBOARD_TOKEN:
        return web.Response(status=404, text="Not found")

    conn = sqlite3.connect(DB_PATH)
    try:
        visits_by_day, devices, cities, sources, funnel, submissions = _dashboard_rows(conn)
    finally:
        conn.close()

    page = DASHBOARD_PAGE_TEMPLATE.format(
        window_days=DASHBOARD_WINDOW_DAYS,
        generated_at=datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC"),
        day_chart=_dashboard_day_chart(visits_by_day),
        visited=funnel["visited"], started=funnel["started"], submitted=funnel["submitted"],
        devices_chart=_dashboard_bar_chart(devices),
        cities_chart=_dashboard_bar_chart(cities),
        sources_chart=_dashboard_bar_chart(sources),
        submissions_table=_dashboard_submissions_table(submissions),
    )
    return web.Response(text=page, content_type="text/html", headers={"X-Robots-Tag": "noindex, nofollow"})


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


def _preview_row_and_submission(conn, token):
    row = conn.execute(
        "SELECT id, submission_id, tier, created_at, expires_at FROM previews WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return None, None
    conn.row_factory = sqlite3.Row
    sub = conn.execute(
        "SELECT * FROM intake_submissions WHERE id = ?", (row[1],)
    ).fetchone()
    return row, sub


async def handle_preview(request):
    token = request.match_info.get("token", "")
    sub_path = request.match_info.get("path", "").lstrip("/")

    conn = sqlite3.connect(DB_PATH)
    try:
        preview_row, submission = _preview_row_and_submission(conn, token)
        if not preview_row or not submission:
            return web.Response(status=404, text="Not found")

        _, submission_id, tier, _, expires_at = preview_row
        client_id = submission["client_id"] or ""
        artist_name = submission["artist_name"] or ""
        now = datetime.now(timezone.utc)
        expires_dt = datetime.fromisoformat(expires_at)

        if now > expires_dt:
            total_previews = conn.execute(
                "SELECT COUNT(*) FROM previews WHERE submission_id = ?", (submission_id,)
            ).fetchone()[0]
            if total_previews < 2:
                _log_event_sync(conn, client_id, "preview_expired", extra=token)
                conn.commit()
                return web.Response(text=PREVIEW_EXPIRED_REFRESH_PAGE, content_type="text/html",
                                     headers={"X-Robots-Tag": "noindex, nofollow"})
            else:
                _log_event_sync(conn, client_id, "preview_refresh_limit_reached", extra=token)
                conn.commit()
                fire_and_forget(send_telegram_notification(
                    f"\U0001F6D1 <b>{html.escape(artist_name)}</b> hit their expired preview with no refreshes left."
                ))
                return web.Response(text=PREVIEW_SUPPORT_REACHOUT_PAGE, content_type="text/html",
                                     headers={"X-Robots-Tag": "noindex, nofollow"})

        preview_dir = os.path.abspath(os.path.join(PREVIEWS_DIR, token))
        default_file = "index.html" if tier == "full" else "listen.html"
        file_path = sub_path or default_file
        full_path = os.path.abspath(os.path.join(preview_dir, file_path))

        if not full_path.startswith(preview_dir + os.sep) and full_path != preview_dir:
            return web.Response(status=404, text="Not found")
        if not os.path.isfile(full_path):
            return web.Response(status=404, text="Not found")

        if not sub_path or sub_path.endswith(".html"):
            _log_event_sync(conn, client_id, "preview_opened", page=file_path, extra=token)
            conn.commit()

        return web.FileResponse(full_path, headers={"X-Robots-Tag": "noindex, nofollow"})
    finally:
        conn.close()


async def handle_preview_refresh(request):
    token = request.match_info.get("token", "")
    headers = _cors_headers(request)

    conn = sqlite3.connect(DB_PATH)
    try:
        preview_row, submission = _preview_row_and_submission(conn, token)
        if not preview_row or not submission:
            return web.json_response({"ok": False, "error": "not_found"}, status=404, headers=headers)

        _, submission_id, tier, _, expires_at = preview_row
        now = datetime.now(timezone.utc)
        if now <= datetime.fromisoformat(expires_at):
            return web.json_response({"ok": False, "error": "not_expired_yet"}, status=400, headers=headers)

        total_previews = conn.execute(
            "SELECT COUNT(*) FROM previews WHERE submission_id = ?", (submission_id,)
        ).fetchone()[0]
        if total_previews >= 2:
            return web.json_response({"ok": False, "error": "refresh_limit_reached"}, status=403, headers=headers)

        client_id = submission["client_id"] or ""
        artist_name = submission["artist_name"] or ""
        fields = _submission_row_to_fields(submission)
    finally:
        conn.close()

    new_url = await generate_preview(submission_id, client_id, fields, is_refresh=True)
    if not new_url:
        return web.json_response({"ok": False, "error": "generation_failed"}, status=500, headers=headers)

    conn2 = sqlite3.connect(DB_PATH)
    try:
        _log_event_sync(conn2, client_id, "preview_refresh_requested", extra=new_url)
        conn2.commit()
    finally:
        conn2.close()

    fire_and_forget(send_telegram_notification(
        f"\U0001F504 Preview refreshed for <b>{html.escape(artist_name)}</b> - new 24h link is live."
    ))

    return web.json_response({"ok": True, "url": new_url}, headers=headers)


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
    os.makedirs(PREVIEWS_DIR, exist_ok=True)
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
    app.router.add_get("/dashboard/{token}", handle_dashboard)
    app.router.add_post("/preview/{token}/refresh", handle_preview_refresh)
    app.router.add_options("/preview/{token}/refresh", handle_options)
    app.router.add_get("/preview/{token}", handle_preview)
    app.router.add_get("/preview/{token}/{path:.*}", handle_preview)
    # allow_head=False: a security scanner's HEAD request to preview this
    # link must not be able to trigger the unsubscribe itself.
    app.router.add_get("/unsubscribe", handle_unsubscribe, allow_head=False)
    app.router.add_get("/health", handle_health)
    log.info("Subscribe service starting on %s:%s", LISTEN_HOST, LISTEN_PORT)
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()
