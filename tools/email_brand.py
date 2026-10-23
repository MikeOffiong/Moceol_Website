#!/usr/bin/env python3
"""
Moceol -- shared email branding for subscribe_service.py and
send_announcement.py. Not a standalone script - imported by both so the
HTML layout, colors, and "from" identity live in exactly one place
instead of drifting between two copies.

Two separate color palettes, by design (see PLATFORM_* vs ARTIST_*
below) - which one an email uses depends on who it's FOR, not who sent
it:

  - Emails TO the artist/client (the new-fan alert) always use Moceol
    Entertainment's own brand - the operator's identity stays constant
    across every client this system ever runs for, so PLATFORM_* is a
    fixed constant, not an env var.
  - Emails TO fans (welcome, announcements) use THAT ARTIST's own brand,
    pulled from their site and set per client via ARTIST_* env vars. For
    moceol.com these happen to be the same colors, but that's a
    coincidence of this being Moceol's own first deployment, not a rule.

Colors below are copied exactly from the site's own style.css
(--color-teal, --color-amber) - never approximated. BRAND_CARD_BG is the
flat-color equivalent of that same site's .platform-row panel
(rgba(0,0,0,0.38) over the teal background), computed once rather than
guessed, since email clients render alpha-transparency over a background
inconsistently (Outlook in particular) - a solid color is the safe
choice there.

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
  ARTIST_BG            - the artist's own site background color
  ARTIST_CARD_BG       - the artist's own "panel on background" color
  ARTIST_ACCENT        - the artist's own accent color
"""

import os

ARTIST_NAME = os.environ.get("ARTIST_NAME", "Moceol")
ARTIST_EMAIL = os.environ.get("ARTIST_EMAIL", "moceol@hello.moceol.com")
RESEND_FROM = os.environ.get("RESEND_FROM") or f"{ARTIST_NAME} <{ARTIST_EMAIL}>"
FAVICON_URL = os.environ.get("FAVICON_URL", "https://moceol.com/images/favicon-192x192.png")
ARTIST_SITE_URL = os.environ.get("ARTIST_SITE_URL", "https://moceol.com")
ARTIST_SITE_DISPLAY = os.environ.get("ARTIST_SITE_DISPLAY", "moceol.com")

# Moceol Entertainment's own brand - fixed, not per-client. Exact values
# from style.css: --color-teal: #003f42; --color-amber: #f2aa4c.
PLATFORM_BG = "#003f42"
PLATFORM_CARD_BG = "#002729"  # rgba(0,0,0,0.38) over #003f42, composited
PLATFORM_ACCENT = "#f2aa4c"

# The artist's own brand - per-client. Defaults match moceol.com's own
# style.css values (same math as PLATFORM_CARD_BG above) since, for this
# deployment, the artist and the platform happen to share a palette.
ARTIST_BG = os.environ.get("ARTIST_BG", "#003f42")
ARTIST_CARD_BG = os.environ.get("ARTIST_CARD_BG", "#002729")
ARTIST_ACCENT = os.environ.get("ARTIST_ACCENT", "#f2aa4c")

BRAND_TEXT = "#e8e8e8"
BRAND_MUTED = "#8fa3a3"

UNSUB_FOOTNOTE = "New music and updates from the artist, unsubscribe anytime."


def render_email_html(body_html, footer_html, bg, card_bg, accent):
    """Wraps already-built inner content in the shared layout: circular
    favicon brand mark up top, a card in card_bg, then whatever footer
    the caller supplies. bg/card_bg/accent let each caller pick the
    platform palette (artist-facing alert) or the artist's own palette
    (fan-facing emails) - see the module docstring.

    Table layout with inline styles throughout, PLUS the legacy bgcolor
    attribute and a dark-mode-safe <style> block - Gmail in particular
    has been known to override a background set only via CSS background
    shorthand on <body>, especially when the viewer's Gmail is in dark
    mode. Setting bgcolor (an HTML attribute, not CSS) on every table/td,
    using background-color (not the background shorthand) inline, and
    re-asserting the same colors inside @media (prefers-color-scheme:
    dark) with !important is the standard "bulletproof background"
    pattern for this exact failure mode."""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="color-scheme" content="light dark" />
<meta name="supported-color-schemes" content="light dark" />
<style>
  @media (prefers-color-scheme: dark) {{
    .email-bg {{ background-color: {bg} !important; }}
    .email-card {{ background-color: {card_bg} !important; }}
  }}
</style>
</head>
<body style="margin:0; padding:0; background-color:{bg};" bgcolor="{bg}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{bg}" class="email-bg" style="background-color:{bg};">
<tr><td align="center" bgcolor="{bg}" class="email-bg" style="background-color:{bg}; padding:40px 20px;">
<table role="presentation" width="100%" style="max-width:480px;" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding-bottom:24px;">
<img src="{FAVICON_URL}" width="56" height="56" alt="{ARTIST_NAME}" style="display:block; border-radius:50%; border:2px solid {accent};" />
</td></tr>
<tr><td bgcolor="{card_bg}" class="email-card" style="background-color:{card_bg}; border-radius:14px; padding:32px 28px; font-family:Tahoma,Geneva,sans-serif; font-size:15px; line-height:1.6; color:{BRAND_TEXT};">
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


def build_fan_footer(unsubscribe_url, accent=ARTIST_ACCENT):
    return (
        f'<p style="margin:0 0 14px;">There&rsquo;s more to explore - music, updates, and more - at '
        f'<a href="{ARTIST_SITE_URL}" style="color:{accent};">{ARTIST_SITE_DISPLAY}</a>.</p>'
        f'<p style="margin:0 0 8px;">{UNSUB_FOOTNOTE}</p>'
        f'<p style="margin:0;"><a href="{unsubscribe_url}" style="color:{BRAND_MUTED};">Unsubscribe</a></p>'
    )


def build_artist_footer():
    return f'<p style="margin:0;">Powered by {ARTIST_NAME}</p>'


def build_cta_button(label, url, accent=ARTIST_ACCENT):
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:20px 0;"><tr>'
        f'<td bgcolor="{accent}" style="background-color:{accent}; border-radius:6px;">'
        f'<a href="{url}" style="display:inline-block; padding:13px 28px; '
        f'font-family:Helvetica,Arial,sans-serif; font-size:14px; font-weight:bold; '
        f'color:#000000; text-decoration:none; letter-spacing:0.5px;">{label}</a>'
        f"</td></tr></table>"
    )
