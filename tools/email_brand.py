#!/usr/bin/env python3
"""
Moceol -- shared email branding for subscribe_service.py and
send_announcement.py. Not a standalone script - imported by both so the
HTML layout, accent color, and "from" identity live in exactly one place
instead of drifting between two copies.

Per-client settings (env vars, so a future client is just a different
env file, not a code change):
  ARTIST_NAME          - display name used in the "from" field and in
                          email copy/sign-offs, e.g. "Moceol"
  ARTIST_EMAIL         - the mailbox part of the sender address, e.g.
                          "moceol@hello.moceol.com"
  RESEND_FROM          - optional full override ("Name <email>"); if
                          unset, built from ARTIST_NAME + ARTIST_EMAIL
  FAVICON_URL          - brand mark shown at the top of every email -
                          must be a public https PNG/JPG URL (email
                          clients can't fetch .ico or inline SVG)
  ARTIST_SITE_URL      - the artist's site, linked in fan-facing emails
  ARTIST_SITE_DISPLAY  - how that link reads, e.g. "moceol.com"
"""

import os

ARTIST_NAME = os.environ.get("ARTIST_NAME", "Moceol")
ARTIST_EMAIL = os.environ.get("ARTIST_EMAIL", "moceol@hello.moceol.com")
RESEND_FROM = os.environ.get("RESEND_FROM") or f"{ARTIST_NAME} <{ARTIST_EMAIL}>"
FAVICON_URL = os.environ.get("FAVICON_URL", "https://moceol.com/images/favicon-192x192.png")
ARTIST_SITE_URL = os.environ.get("ARTIST_SITE_URL", "https://moceol.com")
ARTIST_SITE_DISPLAY = os.environ.get("ARTIST_SITE_DISPLAY", "moceol.com")

BRAND_ACCENT = "#f2aa4c"
BRAND_BG = "#003f42"
BRAND_CARD_BG = "#00282a"
BRAND_TEXT = "#e8e8e8"
BRAND_MUTED = "#8fa3a3"

UNSUB_FOOTNOTE = "New music and updates from the artist, unsubscribe anytime."


def render_email_html(body_html, footer_html):
    """Wraps already-built inner content in the shared layout: circular
    favicon brand mark up top, an accent-bordered card, then whatever
    footer the caller supplies (fan-facing emails pass the unsubscribe
    footnote + link; the artist-facing alert passes a plain "Powered by"
    line instead, since unsubscribing doesn't apply to it).

    Table layout with inline styles throughout - the usual constraints
    for rendering consistently across Gmail/Outlook/Apple Mail, not just
    modern browsers."""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
</head>
<body style="margin:0; padding:0; background:{BRAND_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BRAND_BG};">
<tr><td align="center" style="padding:40px 20px;">
<table role="presentation" width="100%" style="max-width:480px;" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding-bottom:24px;">
<img src="{FAVICON_URL}" width="56" height="56" alt="{ARTIST_NAME}" style="display:block; border-radius:50%; border:2px solid {BRAND_ACCENT};" />
</td></tr>
<tr><td style="background:{BRAND_CARD_BG}; border-radius:14px; padding:32px 28px; font-family:Tahoma,Geneva,sans-serif; font-size:15px; line-height:1.6; color:{BRAND_TEXT};">
{body_html}
</td></tr>
<tr><td align="center" style="padding-top:22px; font-family:Tahoma,Geneva,sans-serif; font-size:11px; color:{BRAND_MUTED}; line-height:1.7;">
{footer_html}
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def build_fan_footer(unsubscribe_url):
    return (
        f'<p style="margin:0 0 14px;">There&rsquo;s more to explore - music, updates, and more - at '
        f'<a href="{ARTIST_SITE_URL}" style="color:{BRAND_ACCENT};">{ARTIST_SITE_DISPLAY}</a>.</p>'
        f'<p style="margin:0 0 8px;">{UNSUB_FOOTNOTE}</p>'
        f'<p style="margin:0;"><a href="{unsubscribe_url}" style="color:{BRAND_MUTED};">Unsubscribe</a></p>'
    )


def build_artist_footer():
    return f'<p style="margin:0;">Powered by {ARTIST_NAME}</p>'


def build_cta_button(label, url):
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:20px 0;"><tr>'
        f'<td style="background:{BRAND_ACCENT}; border-radius:6px;">'
        f'<a href="{url}" style="display:inline-block; padding:13px 28px; '
        f'font-family:Helvetica,Arial,sans-serif; font-size:14px; font-weight:bold; '
        f'color:#000000; text-decoration:none; letter-spacing:0.5px;">{label}</a>'
        f"</td></tr></table>"
    )
