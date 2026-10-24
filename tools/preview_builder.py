#!/usr/bin/env python3
"""Moceol -- Stage A automatic preview generator.

Builds a private, token-addressed static preview of an artist's site from
their intake_submissions row, reusing the real moceol.com template files
verbatim (checked out at TEMPLATE_REPO_PATH, kept current the same way
release_bot.py already refreshes its own checkout) with per-artist color
and content overrides layered on top via a small injected <style>/<script>
block in each page's <head> - the shared template files on disk are never
edited, only copied and lightly text-substituted per preview.

Landing Page tier -> listen.html only.
Full Website tier -> index, music, bio, media, shop (+ support if the
    artist opted in), all sharing one color theme and one data/releases.js
    entry built from their Spotify link.

Everything this module writes lives under PREVIEWS_DIR, entirely outside
the public git repo - subscribe_service.py serves it from there behind a
token-gated route, never as a static file Cloudflare Pages would publish.
"""

import html
import json
import os
import re
import shutil
import time

import requests

TEMPLATE_REPO_PATH = os.environ.get("TEMPLATE_REPO_PATH", "/root/moceol_website_repo")
PREVIEWS_DIR = os.environ.get("PREVIEWS_DIR", "/root/moceol_previews")
UPLOADS_DIR = os.environ.get("INTAKE_UPLOADS_DIR", "/root/moceol_uploads")

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

LANDING_PAGES = ["listen.html"]
FULL_PAGES = ["index.html", "music.html", "bio.html", "media.html", "shop.html", "listen.html"]
SHARED_ASSETS = ["style.css", "script.js", "favicon.ico", "images/favicon-32x32.png",
                  "images/favicon-192x192.png", "images/favicon-16x16.png", "images/apple-touch-icon.png"]

PREVIEW_PLATFORM_FIELDS = [
    # (submission field, footer cls, FA icon class, label)
    ("spotify_url", "spotify", "fa-brands fa-spotify", "Spotify"),
    ("apple_music_url", "itunes", "fa-brands fa-itunes-note", "Apple Music"),
    ("youtube_url", "youtube", "fa-brands fa-youtube", "YouTube"),
    ("amazon_music_url", "amazon", "fa-brands fa-amazon", "Amazon Music"),
    ("facebook_url", "facebook", "fa-brands fa-facebook", "Facebook"),
    ("x_url", "x-twitter", "fa-brands fa-x-twitter", "X"),
    ("tiktok_url", "tiktok", "fa-brands fa-tiktok", "TikTok"),
    ("instagram_url", "instagram", "fa-brands fa-instagram", "Instagram"),
    ("audiomack_url", "audiomack", "fa-solid fa-music", "Audiomack"),
    ("boomplay_url", "boomplay", "fa-solid fa-music", "Boomplay"),
]

PREVIEW_BANNER = (
    '<div style="position:sticky;top:0;left:0;right:0;z-index:99999;'
    'background:#000;color:#fff;text-align:center;padding:9px 12px;'
    'font-family:Tahoma,Geneva,sans-serif;font-size:12px;line-height:1.4;">'
    '<strong>PREVIEW</strong> &mdash; this is a private preview, not your live site. '
    '<button type="button" disabled style="margin-left:8px;padding:4px 12px;'
    'background:#555;color:#ccc;border:none;border-radius:4px;font-size:11px;'
    'cursor:not-allowed;">Approve &amp; Pay Deposit (coming soon)</button>'
    "</div>"
)


# ---------------------------------------------------------------------------
# Color / contrast - same formulas as services-form.js's readableTextColor,
# ported so generated previews get the same "automatic readable text" the
# live color-preview swatch on the form already demonstrates.
# ---------------------------------------------------------------------------
def _hex_to_rgb(hex_color):
    c = hex_color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _relative_luminance(rgb):
    def chan(v):
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _is_light(hex_color):
    try:
        return _relative_luminance(_hex_to_rgb(hex_color)) > 0.179
    except Exception:
        return False


def build_theme_block(main_color, accent_color):
    r, g, b = _hex_to_rgb(main_color)
    lines = [
        "<style>",
        ":root {",
        f"  --color-main: {main_color};",
        f"  --color-main-rgb: {r}, {g}, {b};",
        f"  --color-accent: {accent_color};",
        "}",
    ]
    if _is_light(main_color):
        lines += [
            ":root {",
            "  --color-white: #000000;",
            "  --color-white-soft: rgba(0,0,0,0.88);",
            "  --color-grey: rgb(70,70,70);",
            "}",
        ]
    if not _is_light(accent_color):
        lines += ["  :root { --color-ink: #ffffff; }"]
    lines.append("</style>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spotify - Client Credentials flow, same app/credentials release_bot.py
# already uses. Artist-scoped lookups work fine on this flow for any public
# artist, not just Moceol's own.
# ---------------------------------------------------------------------------
def _spotify_token():
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    try:
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception:
        return None


def _parse_spotify_artist_id(spotify_url):
    match = re.search(r"artist[/:]([a-zA-Z0-9]+)", spotify_url or "")
    return match.group(1) if match else None


def fetch_latest_release(spotify_url):
    """Best-effort: returns {title, cover_url, track_id} or None on any
    failure. Never raises - a broken/unusual Spotify link should degrade to
    a placeholder cover, not fail the whole preview."""
    artist_id = _parse_spotify_artist_id(spotify_url)
    if not artist_id:
        return None
    token = _spotify_token()
    if not token:
        return None
    try:
        resp = requests.get(
            f"https://api.spotify.com/v1/artists/{artist_id}/albums",
            params={"include_groups": "single,album", "market": "US", "limit": 10},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return None
        items.sort(key=lambda a: a.get("release_date") or "", reverse=True)
        latest = items[0]
        images = latest.get("images") or []
        cover_url = max(images, key=lambda im: im.get("width") or 0)["url"] if images else None

        track_id = None
        tracks_resp = requests.get(
            f"https://api.spotify.com/v1/albums/{latest['id']}/tracks",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if tracks_resp.ok:
            track_items = tracks_resp.json().get("items", [])
            if track_items:
                track_id = track_items[0].get("id")

        return {"title": latest.get("name") or "New Release", "cover_url": cover_url, "track_id": track_id}
    except Exception:
        return None


def _placeholder_cover_svg(title, accent_color):
    upper = (title or "").upper()
    words = [w for w in upper.split() if w]
    lines, current = [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if len(trial) > 14 and current:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    start_y = 420 - (len(lines) - 1) * 25
    text_lines = "\n  ".join(
        f'<text x="400" y="{start_y + i * 50}" text-anchor="middle" '
        f'font-family="Helvetica, Arial, sans-serif" font-size="42" font-weight="bold" '
        f'letter-spacing="2" fill="{accent_color}">{html.escape(line)}</text>'
        for i, line in enumerate(lines)
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800">
  <rect width="800" height="800" fill="#1a1a1a"/>
  <rect x="28" y="28" width="744" height="744" fill="none" stroke="{accent_color}" stroke-width="2"/>
  {text_lines}
</svg>"""


# ---------------------------------------------------------------------------
# Per-artist data files (data/releases.js, data/products.js) - the exact
# same shape the real site's script.js already knows how to render, so no
# page-rendering logic needs to differ between moceol.com and a preview.
# ---------------------------------------------------------------------------
def build_releases_js(fields, release):
    title = (release or {}).get("title") or fields["artist_name"]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "release"
    cover_url = (release or {}).get("cover_url")

    platforms = {}
    key_map = {
        "apple_music_url": "appleMusic", "youtube_url": "youtube",
        "amazon_music_url": "amazonMusic", "audiomack_url": "audiomack", "boomplay_url": "boomplay",
    }
    for field, key in key_map.items():
        if fields.get(field):
            platforms[key] = fields[field]
    # The form only collects the artist's Spotify *profile* link, but the
    # platform grid (and track embeds) need a *track* link - use the one
    # fetched from Spotify's API, falling back to the profile link only if
    # that lookup failed.
    track_id_for_platforms = (release or {}).get("track_id")
    platforms["spotify"] = (
        f"https://open.spotify.com/track/{track_id_for_platforms}"
        if track_id_for_platforms else (fields.get("spotify_url") or "")
    )

    gumroad_url = None
    if fields.get("gumroad_mode") == "own" and (fields.get("gumroad_handle") or "").startswith("http"):
        gumroad_url = fields["gumroad_handle"]
    elif fields.get("gumroad_mode") == "other" and fields.get("other_payment_link"):
        gumroad_url = fields["other_payment_link"]

    track_id = (release or {}).get("track_id")
    track_url = f"https://open.spotify.com/track/{track_id}" if track_id else (fields.get("spotify_url") or "")

    entry = {
        "slug": slug,
        "number": 1,
        "title": title,
        "coverUrl": "images/cover.jpg" if cover_url else "images/cover.svg",
        "coverIsPlaceholder": not bool(cover_url),
        "releaseDate": None,
        "spotifyTrackUrl": track_url,
        "platforms": platforms,
        "featured": True,
    }
    if gumroad_url:
        entry["gumroadUrl"] = gumroad_url

    return (
        "// Generated preview release data.\n"
        "window.RELEASES = " + json.dumps([entry], indent=4) + ";\n"
    ), cover_url, gumroad_url, title


def build_products_js():
    return (
        "window.PRODUCTS = [\n"
        "    { comingSoon: true },\n"
        "    { comingSoon: true },\n"
        "    { comingSoon: true }\n"
        "];\n"
    )


def build_footer_links_js(fields):
    entries = []
    for field, cls, icon, label in PREVIEW_PLATFORM_FIELDS:
        url = fields.get(field)
        if url:
            entries.append({"cls": cls, "href": url, "icon": icon, "label": label})
    return json.dumps(entries)


# ---------------------------------------------------------------------------
# HTML transforms
# ---------------------------------------------------------------------------
_TRACKING_BLOCK_RE = re.compile(
    r"<!-- TikTok Pixel Code Start -->.*?<!-- TRACKING PIXELS GO HERE.*?-->",
    re.DOTALL,
)


def _strip_tracking(html_text):
    """Previews shouldn't fire Moceol's own GA4/TikTok pixels - that would
    misattribute a prospective artist's test traffic to Moceol's own
    marketing data."""
    return _TRACKING_BLOCK_RE.sub("<!-- analytics stripped for preview -->", html_text, count=1)


def _inject_head(html_text, token, theme_block, footer_links_json, copyright_name, title_text):
    override_script = (
        "<script>\n"
        f"  window.PREVIEW_FOOTER_SOCIAL_LINKS = {footer_links_json};\n"
        f"  window.PREVIEW_COPYRIGHT_NAME = {json.dumps(copyright_name)};\n"
        "</script>\n"
        f'<base href="/preview/{token}/" />\n'
        f"{theme_block}\n"
    )
    html_text = html_text.replace("<head>", "<head>\n" + override_script, 1)
    html_text = re.sub(r"<title>.*?</title>", f"<title>{html.escape(title_text)}</title>", html_text, count=1)
    # The plain description meta, and every og:/twitter: meta, carry
    # Moceol-specific marketing copy per page (release names, "Efik
    # fluently", trading-signal mentions, etc.) - strip rather than risk
    # leaking that onto another artist's preview.
    generic_description = f"{html.escape(title_text)} - built with Moceol."
    html_text = re.sub(
        r'<meta\s+name="description"\s+content="[^"]*"\s*/>',
        f'<meta name="description" content="{generic_description}" />',
        html_text,
        count=1,
    )
    html_text = re.sub(r'<meta\s+(property|name)="(og|twitter):[^"]*"[^>]*/>\s*', "", html_text)
    return html_text


def _inject_body_banner(html_text):
    return html_text.replace("<body>", "<body>\n" + PREVIEW_BANNER, 1)


def _replace_logo(html_text, artist_name):
    return re.sub(
        r'<h2 class="logo">[^<]*</h2>',
        f'<h2 class="logo">{html.escape(artist_name)}</h2>',
        html_text,
    )


def transform_page(raw_html, token, fields, theme_block, footer_links_json, page_title_suffix):
    out = _strip_tracking(raw_html)
    out = _inject_head(out, token, theme_block, footer_links_json, fields["artist_name"],
                        f'{fields["artist_name"]} | {page_title_suffix}')
    out = _inject_body_banner(out)
    out = _replace_logo(out, fields["artist_name"])
    # The real nav links to Moceol's own services/sales page - that has no
    # business appearing in another artist's preview.
    out = re.sub(r'\s*<li><a href="/services">SERVICES</a></li>', "", out)
    return out


def transform_bio(raw_html, fields):
    bio_html = f"<p>{html.escape(fields.get('bio') or '')}</p>"
    return re.sub(
        r'<div class="split-content placeholder-section">.*?</div>\s*</div>',
        f'<div class="split-content placeholder-section">{bio_html}</div></div>',
        raw_html,
        count=1,
        flags=re.DOTALL,
    )


def transform_media(raw_html, gallery_filenames):
    if gallery_filenames:
        items = "\n".join(
            f'<div class="gallery-item"><img src="images/{name}" alt="" loading="lazy" /></div>'
            for name in gallery_filenames
        )
    else:
        items = '<p style="color:var(--color-grey); text-align:center; width:100%;">No gallery photos yet.</p>'
    return re.sub(
        r'<div class="gallery-grid">.*?</div>\s*</main>',
        f'<div class="gallery-grid">{items}</div></main>',
        raw_html,
        count=1,
        flags=re.DOTALL,
    )


def transform_music(raw_html, track_id):
    """The real music.html hardcodes Moceol's whole catalog as track-card
    divs - none of that belongs in another artist's preview. Stage 1 only
    collects one Spotify link, so this shows that one track; a full catalog
    is the same later-stage release-detection work as the live site's own."""
    if track_id:
        safe_id = html.escape(track_id)
        grid = (
            f'<div class="track-card" data-track-id="{safe_id}">'
            f'<iframe src="https://open.spotify.com/embed/track/{safe_id}" '
            'width="100%" height="152" frameborder="0" '
            'allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture" '
            'loading="lazy" title="Spotify track player"></iframe></div>'
        )
    else:
        grid = '<p style="color:var(--color-grey); text-align:center; width:100%;">No tracks found yet.</p>'
    return re.sub(
        r'<div class="track-grid">.*?</div>\s*</main>',
        f'<div class="track-grid">{grid}</div></main>',
        raw_html,
        count=1,
        flags=re.DOTALL,
    )


def transform_index(raw_html, release_title, gumroad_url):
    """index.html's hero subtitle and Own-It href are static fallback text
    that initHero() only corrects once its JS runs. Fixing them here too is
    defense in depth - a visitor must never see Moceol's own release title
    or be sent to Moceol's own Gumroad link from another artist's preview."""
    raw_html = raw_html.replace('id="hero-stream-btn" href="listen"', 'id="hero-stream-btn" href="listen.html"')
    raw_html = re.sub(
        r'(<p class="par" id="hero-subtitle">)[^<]*(</p>)',
        lambda m: m.group(1) + html.escape(release_title) + m.group(2),
        raw_html,
        count=1,
    )
    if gumroad_url:
        raw_html = re.sub(
            r'(id="hero-own-btn" href=")[^"]*(")',
            lambda m: m.group(1) + html.escape(gumroad_url) + m.group(2),
            raw_html,
            count=1,
        )
    else:
        raw_html = re.sub(
            r'<a class="btn btn-solid" id="hero-own-btn"[^>]*>.*?</a>\s*',
            "",
            raw_html,
            count=1,
            flags=re.DOTALL,
        )
    return raw_html


def build_support_page(fields):
    """support.html keeps the real template's layout exactly - only the
    link, button label and blurb change. No custom layout, per the earlier
    instruction to keep this page's structure as-is."""
    template_path = os.path.join(TEMPLATE_REPO_PATH, "support.html")
    with open(template_path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = re.sub(
        r'<div class="split-content support-section">.*?</div>\s*</div>',
        (
            '<div class="split-content support-section">'
            f'<p>{html.escape(fields.get("support_text") or "")}</p>'
            f'<a class="btn" href="{html.escape(fields.get("support_link") or "#")}" '
            'target="_blank" rel="noopener noreferrer">SUPPORT ME</a>'
            "</div></div>"
        ),
        raw,
        count=1,
        flags=re.DOTALL,
    )
    raw = re.sub(r"<h1>SUPPORT [A-Z]+</h1>", "<h1>SUPPORT</h1>", raw, count=1)
    raw = raw.replace("images/photo-4-web.jpg", "images/portrait.jpg")
    return raw


def _copy_asset(src, dest_dir, dest_name=None):
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, dest_name or os.path.basename(src))
    shutil.copyfile(src, dest)
    return dest


def build_preview_files(out_dir, token, fields, client_id):
    """Writes a complete preview site into out_dir. Returns a list of
    warnings (non-fatal issues worth surfacing, e.g. Spotify lookup
    failed) - never raises for a degraded-but-still-usable preview."""
    warnings = []
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "images"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)

    tier = fields["tier"]
    pages = FULL_PAGES if tier == "full" else LANDING_PAGES

    theme_block = build_theme_block(fields["main_color"], fields["accent_color"])
    footer_links_json = build_footer_links_js(fields)

    release = fetch_latest_release(fields.get("spotify_url") or "")
    if release is None:
        warnings.append("Spotify lookup failed or returned no releases - used a placeholder cover.")
    releases_js, cover_url, gumroad_url, release_title = build_releases_js(fields, release)
    with open(os.path.join(out_dir, "data", "releases.js"), "w", encoding="utf-8") as f:
        f.write(releases_js)
    with open(os.path.join(out_dir, "data", "products.js"), "w", encoding="utf-8") as f:
        f.write(build_products_js())

    if cover_url:
        try:
            resp = requests.get(cover_url, timeout=15)
            resp.raise_for_status()
            with open(os.path.join(out_dir, "images", "cover.jpg"), "wb") as f:
                f.write(resp.content)
        except Exception:
            cover_url = None
            warnings.append("Cover art download failed - used a placeholder cover.")
    if not cover_url:
        title = (release or {}).get("title") or fields["artist_name"]
        svg = _placeholder_cover_svg(title, fields["accent_color"])
        with open(os.path.join(out_dir, "images", "cover.svg"), "w", encoding="utf-8") as f:
            f.write(svg)

    for asset in SHARED_ASSETS:
        src = os.path.join(TEMPLATE_REPO_PATH, asset)
        if os.path.exists(src):
            _copy_asset(src, os.path.join(out_dir, os.path.dirname(asset)) or out_dir, os.path.basename(asset))
        else:
            warnings.append(f"Template asset missing: {asset}")

    gallery_filenames = []
    if tier == "full":
        client_upload_dir = os.path.join(UPLOADS_DIR, client_id)
        portrait_copied = False
        if os.path.isdir(client_upload_dir):
            for name in sorted(os.listdir(client_upload_dir)):
                if name.startswith("portrait."):
                    _copy_asset(os.path.join(client_upload_dir, name), os.path.join(out_dir, "images"), "portrait.jpg")
                    portrait_copied = True
                elif name.startswith("gallery-"):
                    dest_name = name.split(".")[0] + ".jpg"
                    _copy_asset(os.path.join(client_upload_dir, name), os.path.join(out_dir, "images"), dest_name)
                    gallery_filenames.append(dest_name)
        if not portrait_copied:
            warnings.append("No portrait photo found for this submission - bio page will show no image.")

    for page in pages:
        src = os.path.join(TEMPLATE_REPO_PATH, page)
        if not os.path.exists(src):
            warnings.append(f"Template page missing: {page}")
            continue
        with open(src, "r", encoding="utf-8") as f:
            raw = f.read()
        out = transform_page(raw, token, fields, theme_block, footer_links_json, page.replace(".html", "").title())
        if page == "bio.html":
            out = transform_bio(out, fields)
            out = out.replace("images/photo-3-web.jpg", "images/portrait.jpg")
        if page == "media.html":
            out = transform_media(out, gallery_filenames)
        if page == "music.html":
            out = transform_music(out, (release or {}).get("track_id"))
        if page == "index.html":
            out = transform_index(out, release_title, gumroad_url)
        with open(os.path.join(out_dir, page), "w", encoding="utf-8") as f:
            f.write(out)

    if tier == "full" and fields.get("support_enabled"):
        try:
            support_raw = build_support_page(fields)
            support_out = transform_page(support_raw, token, fields, theme_block, footer_links_json, "Support")
            with open(os.path.join(out_dir, "support.html"), "w", encoding="utf-8") as f:
                f.write(support_out)
        except Exception as e:
            warnings.append(f"Support page generation failed: {e}")

    return warnings
