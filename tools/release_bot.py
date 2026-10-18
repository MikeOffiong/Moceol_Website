#!/usr/bin/env python3
"""
Moceol -- automated release watcher / publisher.
=================================================
Runs continuously on the droplet (systemd: moceol-release-bot.service).

Once a day (first loop tick after the UTC date rolls over), checks each
tracked artist's latest Spotify release against what's already known. On a
new release, creates a strms.link draft smartlink and DMs the owner (via a
dedicated Telegram bot, separate from the trading-signal bot) with the cover,
every platform link found (flagging anything that looks like a bare site
search link rather than a direct track link), and Approve/Reject buttons.

- Approve: publishes the strms.link smartlink, then updates data/releases.js
  (new entry, flips `featured`) and the matching cover art in images/, commits
  and pushes straight to main via a repo-scoped deploy key.
- Reject: DMs the dashboard edit link for the draft plus a "Publish Now"
  button, so the owner can fix bad platform links by hand in strms.link's UI
  and resume from here when ready -- no automatic re-notification nagging.

Built so ARTISTS can grow to more than one Spotify account later; only the
"moceol_website" site_target has an actual publish implementation so far.
"""

import base64
import html
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("RELEASE_BOT_TOKEN", "")
OWNER_USER_ID = os.environ.get("OWNER_USER_ID", "")
STRMS_API_KEY = os.environ.get("STRMS_API_KEY", "")
STRMS_WORKSPACE_ID = os.environ.get("STRMS_WORKSPACE_ID", "")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

STATE_FILE = "/root/moceol_release_state.json"
REPO_PATH = "/root/moceol_website_repo"

UPDATE_POLL_SECONDS = 5   # how often Telegram is checked for button taps

STRMS_BASE_URL = "https://app.strms.link/api/v1"
STRMS_DASHBOARD_URL = "https://app.strms.link/links/{id}"

# Tracked Spotify artists. Only "moceol_website" has a real publish target
# right now -- a future artist entry with no matching site_target handler
# just gets detected/notified with no auto-publish path wired up yet.
ARTISTS = [
    {
        "spotify_artist_id": "3BtAYqYcTzAm7CcoXs4MYE",
        "name": "Moceol",
        "site_target": "moceol_website",
    },
]

# strms.link's snake_case platform keys -> this site's existing camelCase
# convention (see data/releases.js). Anything not listed falls back to a
# generic snake_case -> camelCase conversion so nothing silently drops.
PLATFORM_KEY_MAP = {
    "spotify": "spotify",
    "apple_music": "appleMusic",
    "youtube_music": "youtubeMusic",
    "youtube": "youtube",
    "deezer": "deezer",
    "tidal": "tidal",
    "amazon_music": "amazonMusic",
    "soundcloud": "soundcloud",
    "pandora": "pandora",
    "boomplay": "boomplay",
    "audiomack": "audiomack",
    "napster": "napster",
    "anghami": "anghami",
    "yandex_music": "yandex",
    "google_play": "googleStore",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("release_bot")


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
def load_state():
    default = {
        "last_update_id": 0,
        "last_daily_check_date": None,  # ISO date (UTC); gates the once-a-day run
        "artists": {},
    }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            saved = json.load(f)
        default.update(saved)
    for artist in ARTISTS:
        default["artists"].setdefault(artist["spotify_artist_id"], {
            "last_applied_release_id": None,  # None until bootstrapped below
            "pending": None,
        })
    return default


def save_state(state):
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, STATE_FILE)


# ---------------------------------------------------------------------------
# SPOTIFY
# ---------------------------------------------------------------------------
def get_spotify_token():
    basic = base64.b64encode(
        (SPOTIFY_CLIENT_ID + ":" + SPOTIFY_CLIENT_SECRET).encode()
    ).decode()
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": "Basic " + basic,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def largest_image_url(images):
    if not images:
        return None
    return max(images, key=lambda im: im.get("width") or 0).get("url")


def get_latest_release(token, spotify_artist_id):
    resp = requests.get(
        f"https://api.spotify.com/v1/artists/{spotify_artist_id}/albums",
        params={"include_groups": "single,album", "market": "US", "limit": 50},
        headers={"Authorization": "Bearer " + token},
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if not items:
        return None
    items.sort(key=lambda a: a.get("release_date") or "", reverse=True)
    latest = items[0]

    tracks_resp = requests.get(
        f"https://api.spotify.com/v1/albums/{latest['id']}/tracks",
        params={"limit": 1},
        headers={"Authorization": "Bearer " + token},
        timeout=15,
    )
    tracks_resp.raise_for_status()
    track_items = tracks_resp.json().get("items", [])
    if not track_items:
        return None
    first_track = track_items[0]

    return {
        "release_id": latest["id"],
        "title": latest["name"],
        "release_date": latest.get("release_date"),
        "remote_cover_url": largest_image_url(latest.get("images")),
        "spotify_track_url": "https://open.spotify.com/track/" + first_track["id"],
    }


# ---------------------------------------------------------------------------
# STRMS.LINK
# ---------------------------------------------------------------------------
def strms_headers(extra=None):
    headers = {"Authorization": "Bearer " + STRMS_API_KEY}
    if extra:
        headers.update(extra)
    return headers


def strms_create_draft(spotify_track_url):
    resp = requests.post(
        f"{STRMS_BASE_URL}/workspaces/{STRMS_WORKSPACE_ID}/smartlinks",
        headers=strms_headers({"Content-Type": "application/json"}),
        json={"source": {"url": spotify_track_url}},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def strms_find_smartlink(smartlink_id):
    """No documented single-item GET, so page through the list endpoint."""
    cursor = ""
    while True:
        params = {"cursor": cursor} if cursor else {}
        resp = requests.get(
            f"{STRMS_BASE_URL}/workspaces/{STRMS_WORKSPACE_ID}/smartlinks",
            headers=strms_headers(),
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        for item in body.get("items", []):
            if item.get("id") == smartlink_id:
                return item
        cursor = body.get("next_cursor") or ""
        if not cursor:
            return None


def strms_publish(smartlink_id, version):
    resp = requests.post(
        f"{STRMS_BASE_URL}/workspaces/{STRMS_WORKSPACE_ID}/smartlinks/{smartlink_id}/publish",
        headers=strms_headers({"If-Match": str(version)}),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def is_search_link(url):
    return "/search" in (url or "").lower()


def map_platform_key(strms_key):
    if strms_key in PLATFORM_KEY_MAP:
        return PLATFORM_KEY_MAP[strms_key]
    parts = strms_key.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def platform_buttons(smartlink):
    return [
        b for b in smartlink.get("buttons", [])
        if b.get("kind") == "platform" and b.get("enabled")
    ]


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
def send_telegram(chat_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return None
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload, timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("result")
    except Exception as e:
        log.error("Telegram sendMessage failed: %s", e)
        return None


def send_telegram_photo(chat_id, photo_url, caption=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return None
    payload = {"chat_id": chat_id, "photo": photo_url}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            json=payload, timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        log.error("Telegram sendPhoto failed: %s", e)


def answer_callback_query(callback_query_id):
    if not TELEGRAM_BOT_TOKEN or not callback_query_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id}, timeout=10,
        )
    except Exception as e:
        log.error("Telegram answerCallbackQuery failed: %s", e)


def get_telegram_updates(offset):
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={
                "timeout": 0,
                "offset": offset or None,
                "allowed_updates": json.dumps(["callback_query"]),
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        log.error("Telegram getUpdates failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# GIT / SITE UPDATE
# ---------------------------------------------------------------------------
def slugify(title):
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def refresh_repo():
    subprocess.run(["git", "fetch", "origin", "main"], cwd=REPO_PATH, check=True)
    subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=REPO_PATH, check=True)


def download_cover(remote_url, slug):
    resp = requests.get(remote_url, timeout=20)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    ext = "png" if "png" in content_type else "webp" if "webp" in content_type else "jpg"
    filename = f"{slug}-cover.{ext}"
    images_dir = os.path.join(REPO_PATH, "images")
    os.makedirs(images_dir, exist_ok=True)
    with open(os.path.join(images_dir, filename), "wb") as f:
        f.write(resp.content)
    return "images/" + filename


def load_releases_js():
    path = os.path.join(REPO_PATH, "data", "releases.js")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    match = re.search(r"window\.RELEASES\s*=\s*(\[.*\])\s*;\s*$", text, re.DOTALL)
    if not match:
        raise RuntimeError("Could not find window.RELEASES array in releases.js")
    return json.loads(match.group(1))


def save_releases_js(releases):
    path = os.path.join(REPO_PATH, "data", "releases.js")
    header = (
        "// Moceol — release catalogue.\n"
        "// Plain .js (not .json) so it loads with a <script> tag and works without a server.\n"
        "// Edited by hand, or automatically by tools/update-release.js / tools/release_bot.py.\n\n"
    )
    body = "window.RELEASES = " + json.dumps(releases, indent=4) + ";\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + body)


def apply_release_to_site(pending, platforms):
    slug = slugify(pending["title"])
    cover_url = download_cover(pending["remote_cover_url"], slug) if pending.get("remote_cover_url") else None

    releases = load_releases_js()
    for r in releases:
        r["featured"] = False

    new_release = {
        "slug": slug,
        "title": pending["title"],
        "coverUrl": cover_url or "",
        "coverIsPlaceholder": cover_url is None,
        "releaseDate": pending.get("release_date"),
        "spotifyTrackUrl": pending["spotify_track_url"],
        "platforms": platforms,
        "featured": True,
    }

    existing_idx = next((i for i, r in enumerate(releases) if r["slug"] == slug), None)
    if existing_idx is not None:
        releases[existing_idx] = new_release
    else:
        releases.append(new_release)

    save_releases_js(releases)

    subprocess.run(["git", "add", "data/releases.js"], cwd=REPO_PATH, check=True)
    if cover_url:
        subprocess.run(["git", "add", cover_url], cwd=REPO_PATH, check=True)
    commit_msg = f"Auto-publish new release: {pending['title']}\n\nDetected via Spotify, platform links via strms.link."
    subprocess.run(["git", "commit", "-m", commit_msg], cwd=REPO_PATH, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=REPO_PATH, check=True)

    return slug


# ---------------------------------------------------------------------------
# CORE FLOW
# ---------------------------------------------------------------------------
def notify_new_release(artist, release):
    lines = [f"<b>New release detected: {html.escape(release['title'])}</b>"]
    if release.get("release_date"):
        lines.append(f"Released: {html.escape(release['release_date'])}")
    lines.append("")
    lines.append("Proposed platform links:")

    smartlink = strms_create_draft(release["spotify_track_url"])
    for button in platform_buttons(smartlink):
        key = map_platform_key(button.get("platform", ""))
        url = button.get("url", "")
        flag = " ⚠️ looks like a search link, not a direct track link" if is_search_link(url) else ""
        lines.append(f"{key}: {url}{flag}")

    lines.append("")
    lines.append("Approve to publish to strms.link + moceol.com, or Reject to fix the links first.")

    if release.get("remote_cover_url"):
        send_telegram_photo(OWNER_USER_ID, release["remote_cover_url"])

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve|{artist['spotify_artist_id']}"},
            {"text": "❌ Reject", "callback_data": f"reject|{artist['spotify_artist_id']}"},
        ]]
    }
    send_telegram(OWNER_USER_ID, "\n".join(lines), reply_markup=keyboard)

    return {
        "release_id": release["release_id"],
        "title": release["title"],
        "release_date": release.get("release_date"),
        "spotify_track_url": release["spotify_track_url"],
        "remote_cover_url": release.get("remote_cover_url"),
        "strms_id": smartlink["id"],
        "status": "awaiting_approval",
    }


def finalize_and_publish(artist, pending):
    current = strms_find_smartlink(pending["strms_id"])
    if not current:
        send_telegram(OWNER_USER_ID, "Couldn't find that strms.link draft anymore — it may have been deleted. Aborting this release.")
        return False

    strms_publish(current["id"], current["version"])

    platforms = {
        map_platform_key(b.get("platform", "")): b.get("url", "")
        for b in platform_buttons(current)
    }

    if artist["site_target"] == "moceol_website":
        refresh_repo()
        slug = apply_release_to_site(pending, platforms)
        send_telegram(
            OWNER_USER_ID,
            f"<b>Published:</b> {html.escape(pending['title'])}\n"
            f"Live now: https://moceol.com/listen.html?r={html.escape(slug)}",
        )
    else:
        send_telegram(OWNER_USER_ID, f"strms.link published for {html.escape(pending['title'])}, but no site-publish handler exists yet for this artist.")

    return True


def run_daily_check(state):
    token = get_spotify_token()
    for artist in ARTISTS:
        artist_state = state["artists"][artist["spotify_artist_id"]]

        if artist_state.get("pending"):
            continue  # already awaiting approval or a manual fix; don't pile on

        try:
            release = get_latest_release(token, artist["spotify_artist_id"])
        except Exception as e:
            log.exception("Spotify lookup failed for %s: %s", artist["name"], e)
            continue
        if not release:
            continue

        if artist_state["last_applied_release_id"] is None:
            # First-ever run for this artist: adopt the current latest release
            # as the known baseline instead of notifying about it.
            artist_state["last_applied_release_id"] = release["release_id"]
            log.info("Bootstrapped %s baseline release to %s", artist["name"], release["release_id"])
            continue

        if release["release_id"] == artist_state["last_applied_release_id"]:
            continue

        log.info("New release detected for %s: %s", artist["name"], release["title"])
        try:
            artist_state["pending"] = notify_new_release(artist, release)
        except Exception as e:
            log.exception("Failed to notify/create draft for %s: %s", artist["name"], e)


def handle_callback(state, callback_query):
    answer_callback_query(callback_query.get("id"))
    data = callback_query.get("data", "")
    if "|" not in data:
        return
    action, artist_id = data.split("|", 1)
    artist = next((a for a in ARTISTS if a["spotify_artist_id"] == artist_id), None)
    artist_state = state["artists"].get(artist_id)
    if not artist or not artist_state or not artist_state.get("pending"):
        return
    pending = artist_state["pending"]

    if action == "approve" and pending["status"] == "awaiting_approval":
        try:
            if finalize_and_publish(artist, pending):
                artist_state["last_applied_release_id"] = pending["release_id"]
                artist_state["pending"] = None
        except Exception as e:
            log.exception("Publish failed for %s: %s", artist["name"], e)
            send_telegram(OWNER_USER_ID, f"Publishing \"{html.escape(pending['title'])}\" failed: {html.escape(str(e))}")

    elif action == "reject" and pending["status"] == "awaiting_approval":
        pending["status"] = "awaiting_manual_fix"
        dashboard_url = STRMS_DASHBOARD_URL.format(id=pending["strms_id"])
        keyboard = {
            "inline_keyboard": [[
                {"text": "Publish Now", "callback_data": f"publish_now|{artist_id}"},
            ]]
        }
        send_telegram(
            OWNER_USER_ID,
            f"Edit the draft here, then tap Publish Now when it's ready:\n{dashboard_url}",
            reply_markup=keyboard,
        )

    elif action == "publish_now" and pending["status"] == "awaiting_manual_fix":
        try:
            if finalize_and_publish(artist, pending):
                artist_state["last_applied_release_id"] = pending["release_id"]
                artist_state["pending"] = None
        except Exception as e:
            log.exception("Publish failed for %s: %s", artist["name"], e)
            send_telegram(OWNER_USER_ID, f"Publishing \"{html.escape(pending['title'])}\" failed: {html.escape(str(e))}")


def main():
    state = load_state()
    log.info("Release bot starting.")

    while True:
        try:
            updates = get_telegram_updates(state.get("last_update_id", 0) + 1)
            for upd in updates:
                state["last_update_id"] = upd["update_id"]
                callback_query = upd.get("callback_query")
                if callback_query:
                    handle_callback(state, callback_query)
            save_state(state)
        except Exception as e:
            log.exception("Error handling Telegram updates: %s", e)

        today = datetime.now(timezone.utc).date().isoformat()
        if state.get("last_daily_check_date") != today:
            try:
                run_daily_check(state)
                state["last_daily_check_date"] = today
                save_state(state)
            except Exception as e:
                log.exception("Daily release check failed: %s", e)

        time.sleep(UPDATE_POLL_SECONDS)


if __name__ == "__main__":
    main()
