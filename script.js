// Moceol — shared site script
// Handles the navbar, the shared footer, and data-driven rendering
// (hero release info, music-page stream links, shop grid).

var FOOTER_SOCIAL_LINKS = [
    { cls: "instagram", href: "https://instagram.com/moceol", icon: "fa-brands fa-instagram", label: "Instagram" },
    { cls: "tiktok", href: "https://www.tiktok.com/@moceol", icon: "fa-brands fa-tiktok", label: "TikTok" },
    { cls: "x-twitter", href: "https://x.com/moceol", icon: "fa-brands fa-x-twitter", label: "X" },
    { cls: "facebook", href: "https://facebook.com/moceol", icon: "fa-brands fa-facebook", label: "Facebook" },
    { cls: "spotify", href: "https://open.spotify.com/artist/3BtAYqYcTzAm7CcoXs4MYE", icon: "fa-brands fa-spotify", label: "Spotify" },
    { cls: "itunes", href: "https://music.apple.com/ng/artist/moceol/1750323484", icon: "fa-brands fa-itunes-note", label: "Apple Music" },
    { cls: "youtube", href: "https://www.youtube.com/@moceolofficial", icon: "fa-brands fa-youtube", label: "YouTube" },
    { cls: "amazon", href: "https://music.amazon.com/artists/B0D64K6NCP/moceol", icon: "fa-brands fa-amazon", label: "Amazon Music" }
];

// TikTok Pixel is installed (see each page's <head>) and wired up below.
// Add other platforms' event calls here too (e.g. window.fbq('track', ...),
// gtag('event', ...), window.snaptr('track', ...)) — every tracked click/button
// on the site calls this one function, so adding a new platform never means
// touching the call sites below.
window.moceolTracking = {
    fireEvent: function (eventName, data) {
        if (window.ttq && typeof window.ttq.track === "function") {
            window.ttq.track(eventName, data);
        }
    }
};

// Endpoint for the "New drop alerts" email capture - a small aiohttp service
// on the droplet (systemd: moceol-subscribe.service), behind Caddy's HTTPS
// reverse proxy. See initDropAlerts() near the bottom of this file.
var ALERTS_SUBSCRIBE_URL = "https://api.moceol.com/subscribe";

// Fires a tracking event, then follows the link. For same-tab links this
// delays navigation briefly so the tracking request isn't cancelled by the
// page unload; target="_blank" links aren't navigating this tab away at all,
// so they fire and proceed immediately. Doesn't change where the link goes.
function wireClickTracking(anchor, eventName, data) {
    anchor.addEventListener("click", function (e) {
        if (anchor.target === "_blank") {
            window.moceolTracking.fireEvent(eventName, data);
            return;
        }
        e.preventDefault();
        window.moceolTracking.fireEvent(eventName, data);
        var destination = anchor.href;
        setTimeout(function () {
            window.location.href = destination;
        }, 300);
    });
}

function renderFooter() {
    var footer = document.getElementById("site-footer");
    if (!footer) {
        return;
    }
    var year = new Date().getFullYear();
    var iconsHtml = FOOTER_SOCIAL_LINKS.map(function (link) {
        return '<a class="' + link.cls + '" href="' + link.href + '" target="_blank" rel="noopener noreferrer" aria-label="' + link.label + '"><i class="' + link.icon + '"></i><span class="sr-only">' + link.label + '</span></a>';
    }).join("");
    footer.innerHTML =
        '<div class="footer-bottom"><p>copyright &copy;<span id="copyright-year">' + year + '</span> Moceol Entertainment. designed by <span class="designer">@moceol</span></p></div>' +
        '<div class="social-icons">' + iconsHtml + '</div>';
}

function getReleases() {
    return window.RELEASES || [];
}

function getFeaturedRelease() {
    var releases = getReleases();
    return releases.find(function (r) { return r.featured; }) || releases[0] || null;
}

var PLATFORM_META = {
    spotify: "Spotify",
    appleMusic: "Apple Music",
    itunes: "iTunes",
    youtube: "YouTube",
    youtubeMusic: "YouTube Music",
    boomplay: "Boomplay",
    audiomack: "Audiomack",
    tidal: "Tidal",
    deezer: "Deezer",
    amazonMusic: "Amazon Music",
    amazonStore: "Amazon",
    soundcloud: "SoundCloud",
    napster: "Napster",
    pandora: "Pandora",
    anghami: "Anghami",
    yandex: "Yandex Music",
    googleStore: "Google Play",
    soundiiz: "Soundiiz",
    audius: "Audius",
    spinrilla: "Spinrilla",
    tiktok: "TikTok"
};

var PLATFORM_ORDER = [
    "spotify", "appleMusic", "youtubeMusic", "audiomack", "youtube",
    "amazonMusic", "boomplay", "tidal", "deezer", "soundcloud",
    "itunes", "pandora", "anghami", "yandex", "napster",
    "amazonStore", "googleStore"
];

var PLATFORM_ICON_SLUG = {
    spotify: "spotify",
    appleMusic: "applemusic",
    itunes: "itunes",
    youtube: "youtube",
    youtubeMusic: "youtubemusic",
    audiomack: "audiomack",
    tidal: "tidal",
    deezer: "deezer",
    amazonMusic: "amazonmusic",
    soundcloud: "soundcloud",
    napster: "napster",
    pandora: "pandora",
    googleStore: "googleplay"
};

var PLATFORM_BRAND_COLOR = {
    spotify: "#1ED760",
    appleMusic: "#FA243C",
    itunes: "#FB5BC5",
    youtube: "#FF0000",
    youtubeMusic: "#FF0000",
    audiomack: "#FFA200",
    tidal: "#000000",
    deezer: "#A238FF",
    amazonMusic: "#46C3D0",
    soundcloud: "#FF5500",
    napster: "#2259FF",
    pandora: "#224099",
    googleStore: "#414141"
};

var SIMPLE_ICONS_BASE = "https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/";
var brandIconCache = {};

function fetchBrandIconSvg(key) {
    var slug = PLATFORM_ICON_SLUG[key];
    if (!slug) {
        return Promise.resolve(null);
    }
    if (brandIconCache[slug]) {
        return brandIconCache[slug];
    }
    var color = PLATFORM_BRAND_COLOR[key] || "#f2aa4c";
    var promise = fetch(SIMPLE_ICONS_BASE + slug + ".svg")
        .then(function (res) {
            if (!res.ok) {
                throw new Error("icon fetch failed");
            }
            return res.text();
        })
        .then(function (svgText) {
            return svgText.replace("<svg ", '<svg fill="' + color + '" ');
        })
        .catch(function () {
            return null;
        });
    brandIconCache[slug] = promise;
    return promise;
}

function platformLabel(key) {
    if (PLATFORM_META[key]) {
        return PLATFORM_META[key];
    }
    return key.replace(/([A-Z])/g, " $1").replace(/^./, function (c) { return c.toUpperCase(); }).trim();
}

function platformInitials(label) {
    var words = label.split(" ").filter(Boolean);
    if (words.length >= 2) {
        return (words[0][0] + words[1][0]).toUpperCase();
    }
    return label.slice(0, 2).toUpperCase();
}

function buildPlatformIconSvg(label) {
    var initials = platformInitials(label);
    return '<svg viewBox="0 0 40 40" width="24" height="24" aria-hidden="true">' +
        '<circle cx="20" cy="20" r="19" fill="none" stroke="currentColor" stroke-width="2"/>' +
        '<text x="20" y="25" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13" font-weight="bold" fill="currentColor">' + initials + "</text>" +
        "</svg>";
}

var PLATFORM_ARROW_SVG =
    '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17L17 7M17 7H8M17 7v9"/></svg>';

function getSpotifyTrackId(url) {
    if (!url) {
        return null;
    }
    var match = url.match(/track\/([a-zA-Z0-9]+)/);
    return match ? match[1] : null;
}

function initHero() {
    var subtitleEl = document.getElementById("hero-subtitle");
    var streamBtn = document.getElementById("hero-stream-btn");
    var ownBtn = document.getElementById("hero-own-btn");

    if (!subtitleEl && !streamBtn && !ownBtn) {
        return;
    }

    var featured = getFeaturedRelease();
    if (!featured) {
        return;
    }

    if (subtitleEl) {
        subtitleEl.textContent = featured.title;
    }
    if (streamBtn) {
        streamBtn.href = "listen.html";
        wireClickTracking(streamBtn, "ClickButton", { button: "stream_now", release: featured.slug });
    }
    if (ownBtn) {
        if (featured.gumroadUrl) {
            ownBtn.href = featured.gumroadUrl;
            ownBtn.style.display = "";
        } else {
            ownBtn.style.display = "none";
        }
        wireClickTracking(ownBtn, "ClickButton", { button: "own_it", release: featured.slug });
    }
}

function initTrackStreamButtons() {
    var cards = document.querySelectorAll(".track-card[data-track-id]");
    if (!cards.length) {
        return;
    }
    var releases = getReleases();

    cards.forEach(function (card) {
        var trackId = card.getAttribute("data-track-id");
        var release = releases.find(function (r) {
            return getSpotifyTrackId(r.spotifyTrackUrl) === trackId;
        });

        var btn = document.createElement("a");
        btn.className = "btn track-stream-btn";
        btn.textContent = "STREAM NOW";

        if (release) {
            btn.href = "listen.html?r=" + encodeURIComponent(release.slug);
        } else {
            btn.href = "listen.html?track=" + encodeURIComponent(trackId);
        }

        wireClickTracking(btn, "ClickButton", { button: "stream_now", release: release ? release.slug : trackId });

        card.appendChild(btn);
    });
}

function initListenPage() {
    var grid = document.getElementById("platform-grid");
    if (!grid) {
        return;
    }

    var releases = getReleases();
    var params = new URLSearchParams(window.location.search);
    var slug = params.get("r");
    var trackParam = params.get("track");

    var release = (slug && releases.find(function (r) { return r.slug === slug; })) || null;

    if (!release && trackParam) {
        release = {
            slug: null,
            title: null,
            coverUrl: null,
            coverIsPlaceholder: true,
            spotifyTrackUrl: "https://open.spotify.com/track/" + trackParam,
            platforms: { spotify: "https://open.spotify.com/track/" + trackParam }
        };
    }

    if (!release) {
        release = getFeaturedRelease();
    }

    var bgEl = document.getElementById("listen-bg");
    var coverWrap = document.getElementById("listen-cover-wrap");
    var coverEl = document.getElementById("listen-cover-img");
    var titleEl = document.getElementById("listen-title");

    if (!release) {
        grid.innerHTML = '<p class="listen-empty">No release found.</p>';
        return;
    }

    if (bgEl) {
        if (release.coverUrl && !release.coverIsPlaceholder) {
            bgEl.style.backgroundImage =
                "linear-gradient(rgba(0, 63, 66, .55), rgba(0, 63, 66, .72)), url('" + release.coverUrl + "')";
        } else {
            bgEl.style.backgroundImage = "none";
        }
    }

    if (release.coverUrl) {
        if (coverWrap) {
            coverWrap.style.display = "";
        }
        if (coverEl) {
            coverEl.src = release.coverUrl;
            coverEl.alt = (release.title || "Moceol") + " cover art";
        }
    } else if (coverWrap) {
        coverWrap.style.display = "none";
    }

    if (titleEl) {
        titleEl.textContent = "Moceol" + (release.title ? " - " + release.title : "");
    }

    // FIRE VIEW EVENT HERE (page load)
    window.moceolTracking.fireEvent("ViewContent", {
        release: release.slug || trackParam || "unknown",
        contents: [{
            content_id: release.slug || trackParam || "unknown",
            content_name: release.title || release.slug || trackParam || "Unknown"
        }]
    });

    var platforms = release.platforms || {};
    var keys = Object.keys(platforms).filter(function (k) { return !!platforms[k]; });

    if (!keys.length) {
        grid.innerHTML = '<p class="listen-empty">Platform links coming soon.</p>';
        return;
    }

    var orderedKeys = PLATFORM_ORDER.filter(function (k) { return keys.indexOf(k) !== -1; });
    var remaining = keys.filter(function (k) { return orderedKeys.indexOf(k) === -1; }).sort();
    orderedKeys = orderedKeys.concat(remaining);

    orderedKeys.forEach(function (key) {
        var url = platforms[key];
        var label = platformLabel(key);

        var row = document.createElement("a");
        row.className = "platform-row";
        row.href = url;
        row.target = "_blank";
        row.rel = "noopener noreferrer";
        row.innerHTML =
            '<span class="platform-row-icon">' + buildPlatformIconSvg(label) + "</span>" +
            '<span class="platform-row-label">' + label + "</span>" +
            '<span class="platform-row-arrow">' + PLATFORM_ARROW_SVG + "</span>";

        row.addEventListener("click", function () {
            // Opens in a new tab, so this tab never unloads - no navigation-timing concern here.
            window.moceolTracking.fireEvent("ClickButton", { platform: key, release: release.slug || trackParam || "unknown" });
        });

        grid.appendChild(row);

        fetchBrandIconSvg(key).then(function (svg) {
            if (svg) {
                var iconEl = row.querySelector(".platform-row-icon");
                if (iconEl) {
                    iconEl.innerHTML = svg;
                }
            }
        });
    });
}

function initShopGrid() {
    var grid = document.getElementById("product-grid");
    if (!grid || !window.PRODUCTS) {
        return;
    }

    window.PRODUCTS.forEach(function (product) {
        var card = document.createElement("div");
        card.className = "product-card";

        if (product.comingSoon) {
            card.classList.add("product-card--soon");
            card.innerHTML =
                '<div class="product-cover product-cover--empty"></div>' +
                '<div class="product-info">' +
                '<p class="product-title product-title--muted">Merch coming soon</p>' +
                "</div>";
        } else {
            var coverHtml = product.coverUrl
                ? '<img src="' + product.coverUrl + '" alt="' + product.title + ' cover art" loading="lazy" />'
                : "";
            card.innerHTML =
                '<div class="product-cover">' + coverHtml + "</div>" +
                '<div class="product-info">' +
                '<p class="product-title">' + product.title + "</p>" +
                '<p class="product-price">' + product.price + "</p>" +
                '<a class="btn" href="' + product.link + '" target="_blank" rel="noopener noreferrer">BUY</a>' +
                "</div>";
        }

        grid.appendChild(card);

        if (!product.comingSoon) {
            var buyBtn = card.querySelector(".btn");
            if (buyBtn) {
                buyBtn.addEventListener("click", function () {
                    // FIRE PURCHASE-INTENT EVENT HERE (Shop Buy button)
                    window.moceolTracking.fireEvent("AddToCart", { product: product.title });
                });
            }
        }
    });
}

// ---------------------------------------------------------------------------
// "New drop alerts" email capture - a header button on listen.html, a
// floating widget everywhere else, sharing one modal and one endpoint.
// ---------------------------------------------------------------------------
var ALERTS_EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

var alertsState = {
    lastTrigger: null,
    placement: null,
    submitting: false
};

function buildBellIconSvg() {
    return '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>' +
        '<path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>';
}

function buildCloseIconSvg() {
    return '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M18 6 6 18M6 6l12 12"/></svg>';
}

function getPageSlug() {
    var path = window.location.pathname.replace(/^\/+|\/+$/g, "").replace(/\.html$/, "");
    return path === "" || path === "index" ? "homepage" : path;
}

function buildAlertsModal() {
    if (document.getElementById("alerts-modal-backdrop")) {
        return;
    }
    var backdrop = document.createElement("div");
    backdrop.className = "alerts-modal-backdrop";
    backdrop.id = "alerts-modal-backdrop";
    backdrop.hidden = true;
    backdrop.innerHTML =
        '<div class="alerts-modal" role="dialog" aria-modal="true" aria-labelledby="alerts-modal-heading" id="alerts-modal">' +
        '<button type="button" class="alerts-close-btn" id="alerts-close-btn" aria-label="Close">' + buildCloseIconSvg() + "</button>" +
        '<h2 id="alerts-modal-heading">Hey, I&rsquo;m glad you&rsquo;re here!</h2>' +
        '<p class="alerts-modal-copy">I&rsquo;ll email you myself the moment new music drops.</p>' +
        '<form id="alerts-form" novalidate>' +
        '<label for="alerts-email-input" class="alerts-label">Email</label>' +
        '<input type="email" id="alerts-email-input" name="email" class="alerts-input" placeholder="you@example.com" autocomplete="email" required />' +
        '<label for="alerts-firstname-input" class="alerts-label">First name</label>' +
        '<input type="text" id="alerts-firstname-input" name="first_name" class="alerts-input" placeholder="First name" autocomplete="given-name" required />' +
        '<label for="alerts-lastname-input" class="alerts-label">Last name <span class="alerts-optional">(optional)</span></label>' +
        '<input type="text" id="alerts-lastname-input" name="last_name" class="alerts-input" placeholder="Last name" autocomplete="family-name" />' +
        '<div class="alerts-honeypot" aria-hidden="true">' +
        '<label for="alerts-website-input">Leave this field blank</label>' +
        '<input type="text" id="alerts-website-input" name="website" tabindex="-1" autocomplete="off" />' +
        "</div>" +
        '<p class="alerts-error" id="alerts-error" role="alert" hidden></p>' +
        '<p class="alerts-success" id="alerts-success" role="status" hidden></p>' +
        '<button type="submit" class="btn btn-solid alerts-submit-btn" id="alerts-submit-btn">Sign up</button>' +
        "</form>" +
        '<p class="alerts-unsub-note">New music and updates from the artist, unsubscribe anytime.</p>' +
        "</div>";
    document.body.appendChild(backdrop);

    document.getElementById("alerts-close-btn").addEventListener("click", closeAlertsModal);
    backdrop.addEventListener("click", function (e) {
        if (e.target === backdrop) {
            closeAlertsModal();
        }
    });
    document.getElementById("alerts-form").addEventListener("submit", handleAlertsSubmit);
}

function openAlertsModal(triggerEl, placement) {
    buildAlertsModal();
    alertsState.lastTrigger = triggerEl;
    alertsState.placement = placement;

    var form = document.getElementById("alerts-form");
    form.reset();
    form.hidden = false;
    document.getElementById("alerts-error").hidden = true;
    document.getElementById("alerts-success").hidden = true;
    document.getElementById("alerts-submit-btn").disabled = false;

    document.getElementById("alerts-modal-backdrop").hidden = false;
    document.addEventListener("keydown", handleAlertsKeydown, true);
    document.getElementById("alerts-email-input").focus();

    if (typeof gtag === "function") {
        gtag("event", "alerts_open", { placement: placement });
    }
}

function closeAlertsModal() {
    var backdrop = document.getElementById("alerts-modal-backdrop");
    if (!backdrop || backdrop.hidden) {
        return;
    }
    backdrop.hidden = true;
    document.removeEventListener("keydown", handleAlertsKeydown, true);
    if (alertsState.lastTrigger && typeof alertsState.lastTrigger.focus === "function") {
        alertsState.lastTrigger.focus();
    }
}

function handleAlertsKeydown(e) {
    if (e.key === "Escape") {
        closeAlertsModal();
        return;
    }
    if (e.key !== "Tab") {
        return;
    }
    var modal = document.getElementById("alerts-modal");
    var focusable = modal.querySelectorAll('button:not([disabled]), [href], input:not([tabindex="-1"]), select, textarea');
    if (!focusable.length) {
        return;
    }
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
    }
}

function handleAlertsSubmit(e) {
    e.preventDefault();
    if (alertsState.submitting) {
        return;
    }
    var emailInput = document.getElementById("alerts-email-input");
    var firstNameInput = document.getElementById("alerts-firstname-input");
    var lastNameInput = document.getElementById("alerts-lastname-input");
    var websiteInput = document.getElementById("alerts-website-input");
    var errorEl = document.getElementById("alerts-error");
    var email = emailInput.value.trim();
    var firstName = firstNameInput.value.trim();
    var lastName = lastNameInput.value.trim();

    if (!firstName) {
        errorEl.textContent = "Let us know your first name.";
        errorEl.hidden = false;
        firstNameInput.focus();
        return;
    }
    if (!ALERTS_EMAIL_RE.test(email)) {
        errorEl.textContent = "That doesn't look like a valid email address.";
        errorEl.hidden = false;
        emailInput.focus();
        return;
    }
    errorEl.hidden = true;
    alertsState.submitting = true;
    document.getElementById("alerts-submit-btn").disabled = true;

    fetch(ALERTS_SUBSCRIBE_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            email: email,
            first_name: firstName,
            last_name: lastName,
            source: alertsState.placement,
            website: websiteInput.value
        })
    })
        .then(function (resp) {
            if (!resp.ok) {
                throw new Error("bad_status");
            }
            return resp.json();
        })
        .then(function (data) {
            if (!data || data.ok !== true) {
                throw new Error("not_ok");
            }
            document.getElementById("alerts-form").hidden = true;
            var successEl = document.getElementById("alerts-success");
            successEl.textContent = "You’re in, " + firstName + " - watch your inbox.";
            successEl.hidden = false;

            if (typeof gtag === "function") {
                gtag("event", "email_signup", { placement: alertsState.placement });
            }
            setTimeout(closeAlertsModal, 2000);
        })
        .catch(function () {
            errorEl.textContent = "Something went wrong - please try again.";
            errorEl.hidden = false;
        })
        .finally(function () {
            alertsState.submitting = false;
            document.getElementById("alerts-submit-btn").disabled = false;
        });
}

// Nudges the floating widget up if it would overlap the footer (or any
// future element marked data-alerts-avoid, e.g. a cookie banner), rather
// than sitting on top of it.
function adjustFloatingAlertsPosition() {
    var btn = document.getElementById("alerts-float-btn");
    if (!btn) {
        return;
    }
    var baseOffset = 20;
    var margin = 16;
    var rect = btn.getBoundingClientRect();
    var push = 0;
    document.querySelectorAll(".site-footer, [data-alerts-avoid]").forEach(function (el) {
        var r = el.getBoundingClientRect();
        var overlaps = r.top < rect.bottom && r.bottom > rect.top && r.top < window.innerHeight;
        if (overlaps) {
            var needed = (window.innerHeight - r.top) + margin;
            if (needed > push) {
                push = needed;
            }
        }
    });
    btn.style.bottom = (baseOffset + push) + "px";
}

var alertsAdjustTicking = false;
function throttledAdjustFloatingAlerts() {
    if (alertsAdjustTicking) {
        return;
    }
    alertsAdjustTicking = true;
    window.requestAnimationFrame(function () {
        adjustFloatingAlertsPosition();
        alertsAdjustTicking = false;
    });
}

function initDropAlerts() {
    buildAlertsModal();

    if (document.querySelector(".listen-main")) {
        var navbar = document.querySelector(".navbar");
        if (!navbar) {
            return;
        }
        var headerBtn = document.createElement("button");
        headerBtn.type = "button";
        headerBtn.className = "alerts-header-btn";
        headerBtn.id = "alerts-header-btn";
        headerBtn.setAttribute("aria-label", "New drop alerts: sign up by email");
        headerBtn.innerHTML =
            '<span class="alerts-header-btn-text">New drop alerts</span>' +
            '<span class="alerts-bell-icon">' + buildBellIconSvg() + "</span>";
        headerBtn.addEventListener("click", function () {
            openAlertsModal(headerBtn, "listen.html");
        });
        navbar.appendChild(headerBtn);
        return;
    }

    var floatBtn = document.createElement("button");
    floatBtn.type = "button";
    floatBtn.className = "alerts-float-btn";
    floatBtn.id = "alerts-float-btn";
    floatBtn.setAttribute("aria-label", "New drop alerts: sign up by email");
    floatBtn.innerHTML = buildBellIconSvg();
    var placement = getPageSlug() + "-widget";
    floatBtn.addEventListener("click", function () {
        openAlertsModal(floatBtn, placement);
    });
    document.body.appendChild(floatBtn);

    adjustFloatingAlertsPosition();
    window.addEventListener("scroll", throttledAdjustFloatingAlerts, { passive: true });
    window.addEventListener("resize", throttledAdjustFloatingAlerts);
}

document.addEventListener("DOMContentLoaded", function () {
    renderFooter();

    var yearEl = document.getElementById("copyright-year");
    if (yearEl) {
        yearEl.textContent = new Date().getFullYear();
    }

    initHero();
    initTrackStreamButtons();
    initListenPage();
    initShopGrid();
    initDropAlerts();

    var hamburger = document.querySelector(".hamburger");
    var navLinks = document.querySelector(".nav-links");

    if (!hamburger || !navLinks) {
        return;
    }

    function closeMenu() {
        navLinks.classList.remove("open");
        hamburger.classList.remove("open");
        hamburger.setAttribute("aria-expanded", "false");
    }

    hamburger.addEventListener("click", function () {
        var isOpen = navLinks.classList.toggle("open");
        hamburger.classList.toggle("open", isOpen);
        hamburger.setAttribute("aria-expanded", isOpen ? "true" : "false");
    });

    navLinks.querySelectorAll("a").forEach(function (link) {
        link.addEventListener("click", closeMenu);
    });
});
