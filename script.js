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

// TIKTOK PIXEL GOES HERE — once the TikTok Pixel base snippet is pasted into
// each page's <head>, this will pick up window.ttq automatically.
window.moceolTikTok = {
    trackClick: function (eventName, data) {
        if (window.ttq && typeof window.ttq.track === "function") {
            window.ttq.track(eventName, data);
        }
    }
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
    return '<svg viewBox="0 0 40 40" width="28" height="28" aria-hidden="true">' +
        '<circle cx="20" cy="20" r="19" fill="none" stroke="currentColor" stroke-width="2"/>' +
        '<text x="20" y="25" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13" font-weight="bold" fill="currentColor">' + initials + "</text>" +
        "</svg>";
}

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
            btn.href = "https://open.spotify.com/track/" + trackId;
            btn.target = "_blank";
            btn.rel = "noopener noreferrer";
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
    var release = (slug && releases.find(function (r) { return r.slug === slug; })) || getFeaturedRelease();

    var coverEl = document.getElementById("listen-cover-img");
    var titleEl = document.getElementById("listen-title");

    if (!release) {
        grid.innerHTML = '<p class="listen-empty">No release found.</p>';
        return;
    }

    if (coverEl) {
        coverEl.src = release.coverUrl;
        coverEl.alt = release.title + " cover art";
    }
    if (titleEl) {
        titleEl.textContent = release.title;
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

        var btn = document.createElement("a");
        btn.className = "platform-btn";
        btn.href = url;
        btn.target = "_blank";
        btn.rel = "noopener noreferrer";
        btn.innerHTML =
            '<span class="platform-icon">' + buildPlatformIconSvg(label) + "</span>" +
            '<span class="platform-label">' + label + "</span>";

        btn.addEventListener("click", function () {
            // TIKTOK PIXEL GOES HERE — fires a click event per platform button once the pixel is installed
            window.moceolTikTok.trackClick("ClickButton", { platform: key, release: release.slug });
        });

        grid.appendChild(btn);
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
