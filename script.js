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

// TRACKING PIXELS GO HERE (TikTok, Meta, Google Ads, Snapchat, etc.) — once pixel
// base snippets are pasted into each page's <head>, call their event methods here
// (e.g. window.ttq.track(...), window.fbq('track', ...), gtag('event', ...),
// window.snaptr('track', ...)). Every tracked click/button on the site calls this
// one function, so adding a new platform never means touching the call sites below.
window.moceolTracking = {
    fireEvent: function (eventName, data) {}
};

function renderFooter() {
    var footer = document.getElementById("site-footer");
    if (!footer) {
        return;
    }
    var year = new Date().getFullYear();
    var iconsHtml = FOOTER_SOCIAL_LINKS.map(function (link) {
        return '<a class="' + link.cls + '" href="' + link.href + '" target="_blank" rel="noopener noreferrer" aria-label="' + link.label + '"><i class="' + link.icon + '"></i></a>';
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
    "spotify", "appleMusic", "youtubeMusic", "youtube", "amazonMusic",
    "boomplay", "audiomack", "tidal", "deezer", "soundcloud",
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
    }
    if (ownBtn) {
        if (featured.gumroadUrl) {
            ownBtn.href = featured.gumroadUrl;
            ownBtn.style.display = "";
        } else {
            ownBtn.style.display = "none";
        }
        ownBtn.addEventListener("click", function () {
            // FIRE PURCHASE-INTENT EVENT HERE (Own It button)
            window.moceolTracking.fireEvent("InitiateCheckout", { release: featured.slug });
        });
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
            // FIRE VIEW/CLICK EVENT HERE (per platform button)
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
