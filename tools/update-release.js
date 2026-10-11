// Moceol — release updater.
// Run with: node tools/update-release.js
//
// Pulls the latest Spotify release for the artist, cross-references it on
// Odesli (song.link) to find links on other platforms, walks you through a
// review step, then writes the result into data/releases.js as the new
// featured release.

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const readline = require("node:readline/promises");

const ARTIST_ID = "3BtAYqYcTzAm7CcoXs4MYE";
const ENV_PATH = "C:\\Users\\User\\Projects\\moceol-secrets.env";
const PROJECT_ROOT = path.join(__dirname, "..");
const DATA_FILE = path.join(PROJECT_ROOT, "data", "releases.js");
const IMAGES_DIR = path.join(PROJECT_ROOT, "images");

const PLATFORM_LABELS = {
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
    spinrilla: "Spinrilla"
};

function platformLabel(key) {
    return PLATFORM_LABELS[key] || key.replace(/([A-Z])/g, " $1").replace(/^./, (c) => c.toUpperCase()).trim();
}

function slugify(str) {
    return String(str)
        .toLowerCase()
        .trim()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "");
}

function buildPlaceholderSvg(title) {
    const upper = String(title || "").toUpperCase();
    const words = upper.split(/\s+/).filter(Boolean);
    const lines = [];
    let current = "";
    words.forEach((word) => {
        const next = current ? current + " " + word : word;
        if (next.length > 14 && current) {
            lines.push(current);
            current = word;
        } else {
            current = next;
        }
    });
    if (current) {
        lines.push(current);
    }
    const startY = 470 - (lines.length - 1) * 25;
    const textLines = lines
        .map((line, i) => `<text x="400" y="${startY + i * 50}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="42" font-weight="bold" letter-spacing="2" fill="#f2aa4c">${escapeXml(line)}</text>`)
        .join("\n  ");
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800" role="img" aria-label="${escapeXml(title)} cover art placeholder">
  <rect width="800" height="800" fill="#003f42"/>
  <rect x="28" y="28" width="744" height="744" fill="none" stroke="#f2aa4c" stroke-width="2"/>
  <circle cx="400" cy="330" r="64" fill="none" stroke="#f2aa4c" stroke-width="3" opacity="0.85"/>
  <path d="M378 304 L430 330 L378 356 Z" fill="#f2aa4c" opacity="0.85"/>
  ${textLines}
  <text x="400" y="${startY + lines.length * 50 + 18}" text-anchor="middle" font-family="Tahoma, Geneva, sans-serif" font-size="16" letter-spacing="3" fill="rgba(255,255,255,0.65)">COVER ART COMING SOON</text>
</svg>
`;
}

function escapeXml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&apos;"
    }[c]));
}

function loadEnv(filePath) {
    if (!fs.existsSync(filePath)) {
        throw new Error("Secrets file not found at " + filePath);
    }
    const content = fs.readFileSync(filePath, "utf8");
    const env = {};
    content.split(/\r?\n/).forEach((line) => {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith("#")) {
            return;
        }
        const idx = trimmed.indexOf("=");
        if (idx === -1) {
            return;
        }
        const key = trimmed.slice(0, idx).trim();
        let value = trimmed.slice(idx + 1).trim();
        if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
            value = value.slice(1, -1);
        }
        env[key] = value;
    });
    return env;
}

async function fetchWithTimeout(url, options, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || 10000);
    try {
        return await fetch(url, Object.assign({}, options, { signal: controller.signal }));
    } finally {
        clearTimeout(timer);
    }
}

async function getSpotifyToken(clientId, clientSecret) {
    const basic = Buffer.from(clientId + ":" + clientSecret).toString("base64");
    const res = await fetchWithTimeout("https://accounts.spotify.com/api/token", {
        method: "POST",
        headers: {
            Authorization: "Basic " + basic,
            "Content-Type": "application/x-www-form-urlencoded"
        },
        body: "grant_type=client_credentials"
    });
    if (!res.ok) {
        throw new Error("Spotify token request failed: " + res.status + " " + res.statusText);
    }
    const json = await res.json();
    if (!json.access_token) {
        throw new Error("Spotify token response had no access_token");
    }
    return json.access_token;
}

function parseSpotifyTrackId(url) {
    if (!url) {
        return null;
    }
    const match = String(url).match(/track[/:]([a-zA-Z0-9]+)/);
    return match ? match[1] : null;
}

function largestImageUrl(images) {
    if (!images || !images.length) {
        return null;
    }
    return images.reduce((a, b) => ((a.width || 0) > (b.width || 0) ? a : b)).url;
}

async function getLatestSpotifyRelease(token) {
    const res = await fetchWithTimeout(
        `https://api.spotify.com/v1/artists/${ARTIST_ID}/albums?include_groups=single,album&market=US&limit=50`,
        { headers: { Authorization: "Bearer " + token } }
    );
    if (!res.ok) {
        throw new Error("Spotify artist albums request failed: " + res.status + " " + res.statusText);
    }
    const json = await res.json();
    const items = json.items || [];
    if (!items.length) {
        throw new Error("No releases found for this artist on Spotify");
    }
    items.sort((a, b) => new Date(b.release_date) - new Date(a.release_date));
    const latest = items[0];

    const tracksRes = await fetchWithTimeout(
        `https://api.spotify.com/v1/albums/${latest.id}/tracks?limit=1`,
        { headers: { Authorization: "Bearer " + token } }
    );
    if (!tracksRes.ok) {
        throw new Error("Spotify album tracks request failed: " + tracksRes.status + " " + tracksRes.statusText);
    }
    const tracksJson = await tracksRes.json();
    const firstTrack = (tracksJson.items || [])[0];
    if (!firstTrack) {
        throw new Error("Latest album has no tracks");
    }

    return {
        title: latest.name,
        remoteCoverUrl: largestImageUrl(latest.images),
        releaseDate: latest.release_date || null,
        spotifyTrackUrl: "https://open.spotify.com/track/" + firstTrack.id
    };
}

async function getSpotifyTrackById(trackId, token) {
    const res = await fetchWithTimeout(
        `https://api.spotify.com/v1/tracks/${trackId}`,
        { headers: { Authorization: "Bearer " + token } }
    );
    if (!res.ok) {
        throw new Error("Spotify track request failed: " + res.status + " " + res.statusText);
    }
    const track = await res.json();

    return {
        title: track.name,
        remoteCoverUrl: largestImageUrl(track.album && track.album.images),
        releaseDate: (track.album && track.album.release_date) || null,
        spotifyTrackUrl: "https://open.spotify.com/track/" + track.id
    };
}

async function getOdesliLinks(spotifyTrackUrl, odesliApiKey) {
    let url = "https://api.song.link/v1-alpha.1/links?url=" + encodeURIComponent(spotifyTrackUrl);
    if (odesliApiKey) {
        url += "&api_key=" + encodeURIComponent(odesliApiKey);
    }
    let res;
    try {
        res = await fetchWithTimeout(url, {}, 10000);
    } catch (err) {
        console.log("Odesli lookup failed (" + err.message + "). You can add platform links manually.");
        return {};
    }
    if (res.status === 429) {
        console.log("Odesli rate-limited the request. Waiting a moment and retrying once...");
        await new Promise((resolve) => setTimeout(resolve, 3000));
        try {
            res = await fetchWithTimeout(url, {}, 10000);
        } catch (err) {
            console.log("Retry failed (" + err.message + "). You can add platform links manually.");
            return {};
        }
    }
    if (res.status === 401) {
        console.log("Odesli's free public API has been deprecated and now requires an API key.");
        console.log("Request one from developers@song.link, then add ODESLI_API_KEY=<key> to " + ENV_PATH + ".");
        console.log("Continuing without automatic cross-platform links — add them manually below.");
        return {};
    }
    if (!res.ok) {
        console.log("Odesli lookup returned " + res.status + ". You can add platform links manually.");
        return {};
    }
    const json = await res.json();
    if (!json.pageUrl) {
        console.log("Odesli found no match for this track. You can add platform links manually.");
        return {};
    }
    const links = {};
    Object.keys(json.linksByPlatform || {}).forEach((key) => {
        const entry = json.linksByPlatform[key];
        if (entry && entry.url) {
            links[key] = entry.url;
        }
    });
    return links;
}

function loadReleasesFile(filePath) {
    if (!fs.existsSync(filePath)) {
        return [];
    }
    const code = fs.readFileSync(filePath, "utf8");
    const sandbox = { window: {} };
    vm.createContext(sandbox);
    vm.runInContext(code, sandbox, { filename: filePath });
    return sandbox.window.RELEASES || [];
}

function serializeReleases(releases) {
    return (
        "// Moceol — release catalogue.\n" +
        "// Plain .js (not .json) so it loads with a <script> tag and works without a server.\n" +
        "// Edited by hand, or automatically by tools/update-release.js.\n\n" +
        "window.RELEASES = " + JSON.stringify(releases, null, 4) + ";\n"
    );
}

async function downloadCover(remoteUrl, slug) {
    const res = await fetchWithTimeout(remoteUrl, {}, 15000);
    if (!res.ok) {
        throw new Error("Cover download failed: " + res.status + " " + res.statusText);
    }
    const contentType = res.headers.get("content-type") || "";
    let ext = "jpg";
    if (contentType.includes("png")) {
        ext = "png";
    } else if (contentType.includes("webp")) {
        ext = "webp";
    }
    const buffer = Buffer.from(await res.arrayBuffer());
    const filename = slug + "-cover." + ext;
    fs.mkdirSync(IMAGES_DIR, { recursive: true });
    fs.writeFileSync(path.join(IMAGES_DIR, filename), buffer);
    return "images/" + filename;
}

async function main() {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    const ask = (question) => rl.question(question);

    try {
        console.log("Moceol release updater\n");

        let trackInfo = null;
        let env = {};
        let token = null;

        try {
            env = loadEnv(ENV_PATH);
            if (!env.SPOTIFY_CLIENT_ID || !env.SPOTIFY_CLIENT_SECRET) {
                throw new Error("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET missing from " + ENV_PATH);
            }
            console.log("Checking Spotify for your latest release...");
            token = await getSpotifyToken(env.SPOTIFY_CLIENT_ID, env.SPOTIFY_CLIENT_SECRET);
            trackInfo = await getLatestSpotifyRelease(token);
            console.log("Found: \"" + trackInfo.title + "\" (" + (trackInfo.releaseDate || "date unknown") + ")\n");
        } catch (err) {
            console.log("\nSpotify's artist catalog lookup didn't work (" + err.message + ").");
            console.log("That endpoint needs extra access that's often blocked in Development Mode, but the");
            console.log("single-track endpoint usually still works — paste the track and I'll try that.\n");

            const pastedUrl = (await ask("Paste the Spotify track URL for the release: ")).trim();
            const trackId = parseSpotifyTrackId(pastedUrl);

            if (!token) {
                console.log("No Spotify token to work with, so this one has to be manual.");
            } else if (!trackId) {
                console.log("Couldn't find a track ID in that URL, so this one has to be manual.");
            } else {
                try {
                    trackInfo = await getSpotifyTrackById(trackId, token);
                    console.log("Found: \"" + trackInfo.title + "\" (" + (trackInfo.releaseDate || "date unknown") + ")\n");
                } catch (trackErr) {
                    console.log("Spotify's track endpoint didn't work either (" + trackErr.message + ").");
                }
            }

            if (!trackInfo) {
                console.log("Let's fill in the rest manually.\n");
                const title = (await ask("Release title: ")).trim();
                const releaseDateInput = (await ask("Release date, YYYY-MM-DD (optional, press Enter to skip): ")).trim();
                trackInfo = {
                    title,
                    remoteCoverUrl: null,
                    releaseDate: releaseDateInput || null,
                    spotifyTrackUrl: pastedUrl
                };
            }
        }

        console.log("Looking up other platforms via Odesli...");
        const odesliLinks = await getOdesliLinks(trackInfo.spotifyTrackUrl, env.ODESLI_API_KEY);

        const reviewed = {};
        const otherKeys = Object.keys(odesliLinks).filter((k) => k !== "audiomack").sort();

        if (otherKeys.length) {
            console.log("\nPlatform links found:");
            otherKeys.forEach((key, i) => {
                console.log((i + 1) + ". " + platformLabel(key) + ": " + odesliLinks[key]);
            });
            console.log("");
            for (const key of otherKeys) {
                const answer = (await ask(
                    platformLabel(key) + " [Enter=keep, paste URL=replace, x=remove]: "
                )).trim();
                if (answer.toLowerCase() === "x") {
                    continue;
                }
                reviewed[key] = answer || odesliLinks[key];
            }
        } else {
            console.log("Odesli didn't find any other platform links.");
        }

        const audiomackFound = odesliLinks.audiomack || null;
        console.log("\nAudiomack needs your confirmation — Odesli often mismatches this artist there.");
        const audiomackAnswer = (await ask(
            "Audiomack (found: " + (audiomackFound || "none") + ") [Enter=" +
            (audiomackFound ? "confirm" : "skip") +
            ", paste URL=set, x=remove]: "
        )).trim();
        if (audiomackAnswer.toLowerCase() === "x") {
            // leave out
        } else if (audiomackAnswer) {
            reviewed.audiomack = audiomackAnswer;
        } else if (audiomackFound) {
            reviewed.audiomack = audiomackFound;
        }

        console.log("\nAdd any platform manually (e.g. a service Odesli missed).");
        while (true) {
            const key = (await ask("Platform key, or press Enter to finish (e.g. boomplay): ")).trim();
            if (!key) {
                break;
            }
            const url = (await ask("URL for " + key + ": ")).trim();
            if (url) {
                reviewed[key] = url;
            }
        }

        if (!reviewed.spotify) {
            reviewed.spotify = trackInfo.spotifyTrackUrl;
        }

        const gumroadUrl = (await ask("\nGumroad link for this release (optional, press Enter to skip): ")).trim();

        const slug = slugify(trackInfo.title);

        console.log("\n--- Review ---");
        console.log("Title: " + trackInfo.title);
        console.log("Slug: " + slug);
        console.log("Release date: " + (trackInfo.releaseDate || "(unknown)"));
        console.log("Platforms:");
        Object.keys(reviewed).sort().forEach((key) => {
            console.log("  " + platformLabel(key) + ": " + reviewed[key]);
        });
        console.log("Gumroad: " + (gumroadUrl || "(none)"));
        console.log("");

        const confirm = (await ask('Type "yes" to save this as the new featured release, anything else to abort: ')).trim();
        if (confirm.toLowerCase() !== "yes") {
            console.log("\nAborted — no changes written.");
            rl.close();
            return;
        }

        if (fs.existsSync(DATA_FILE)) {
            const stamp = new Date().toISOString().replace(/[:.]/g, "-");
            const backupPath = path.join(PROJECT_ROOT, "data", "releases.backup." + stamp + ".js");
            fs.copyFileSync(DATA_FILE, backupPath);
            console.log("Backed up current data/releases.js to " + path.relative(PROJECT_ROOT, backupPath));
        }

        let coverUrl;
        let coverIsPlaceholder;
        if (trackInfo.remoteCoverUrl) {
            console.log("Downloading cover art...");
            coverUrl = await downloadCover(trackInfo.remoteCoverUrl, slug);
            coverIsPlaceholder = false;
        } else {
            console.log("No cover art available — generating a placeholder.");
            const svg = buildPlaceholderSvg(trackInfo.title);
            fs.mkdirSync(IMAGES_DIR, { recursive: true });
            const filename = slug + "-cover.svg";
            fs.writeFileSync(path.join(IMAGES_DIR, filename), svg);
            coverUrl = "images/" + filename;
            coverIsPlaceholder = true;
        }

        const releases = loadReleasesFile(DATA_FILE);
        let previousFeaturedTitle = null;
        releases.forEach((r) => {
            if (r.featured && r.slug !== slug) {
                previousFeaturedTitle = r.title;
            }
            r.featured = false;
        });

        const newRelease = {
            slug,
            title: trackInfo.title,
            coverUrl,
            coverIsPlaceholder,
            releaseDate: trackInfo.releaseDate,
            spotifyTrackUrl: trackInfo.spotifyTrackUrl,
            platforms: reviewed,
            gumroadUrl: gumroadUrl || undefined,
            featured: true
        };

        const existingIndex = releases.findIndex((r) => r.slug === slug);
        if (existingIndex !== -1) {
            releases[existingIndex] = newRelease;
        } else {
            releases.push(newRelease);
        }

        fs.writeFileSync(DATA_FILE, serializeReleases(releases));

        console.log("\nDone.");
        console.log("Featured release is now: " + trackInfo.title);
        console.log("Platforms saved: " + Object.keys(reviewed).sort().join(", "));
        console.log("Cover: " + coverUrl);
        if (previousFeaturedTitle) {
            console.log("Un-featured (kept in file): " + previousFeaturedTitle);
        }
    } catch (err) {
        console.error("\nSomething went wrong: " + err.message);
        process.exitCode = 1;
    } finally {
        rl.close();
    }
}

if (require.main === module) {
    main();
}

module.exports = {
    slugify,
    loadEnv,
    buildPlaceholderSvg,
    serializeReleases,
    loadReleasesFile,
    platformLabel,
    parseSpotifyTrackId,
    getSpotifyToken,
    getLatestSpotifyRelease,
    getSpotifyTrackById,
    getOdesliLinks
};
