// Moceol — services.html form logic (Stage 1).
// Kept separate from script.js since this page's logic (tiers, photo
// uploads, the live-count line, funnel events) only ever runs here.

var INTAKE_URL = "https://api.moceol.com/intake";
var INTAKE_PHOTOS_URL = "https://api.moceol.com/intake-photos";
var LIVE_COUNT_URL = "https://api.moceol.com/live-count";
var EVENT_URL = "https://api.moceol.com/event";

var MAX_GALLERY_PHOTOS = 10;
var MAX_PHOTO_BYTES = 6 * 1024 * 1024;
var MIN_PHOTO_WIDTH = 1000;

// ---------------------------------------------------------------------------
// client_id - a random id persisted in localStorage so every event this
// visitor fires (and any photos they upload) can be tied to the same funnel
// and, on submit, the same intake row. Falls back to a non-persisted id if
// localStorage is blocked (private browsing, etc.) - events still fire, they
// just won't correlate across a reload.
// ---------------------------------------------------------------------------
function getClientId() {
    try {
        var existing = localStorage.getItem("moceol_client_id");
        if (existing) {
            return existing;
        }
        var id = "c_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 10);
        localStorage.setItem("moceol_client_id", id);
        return id;
    } catch (e) {
        return "c_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 10);
    }
}

function guessTrafficSource() {
    var params = new URLSearchParams(window.location.search);
    var utmSource = params.get("utm_source");
    if (utmSource) {
        return utmSource.slice(0, 200);
    }
    if (document.referrer) {
        try {
            return new URL(document.referrer).hostname.slice(0, 200);
        } catch (e) {
            return "";
        }
    }
    return "direct";
}

function fireFunnelEvent(eventType, extra) {
    try {
        fetch(EVENT_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                client_id: getClientId(),
                event_type: eventType,
                page: "services",
                traffic_source: guessTrafficSource(),
                extra: extra || ""
            })
        }).catch(function () {});
    } catch (e) {
        // Never let event logging break the page.
    }
}

// ---------------------------------------------------------------------------
// Photo upload: client-side preview, a soft dimension check, and the actual
// multipart upload to /intake-photos. Each photo is uploaded as soon as it's
// picked (not held in memory for a later batch) so a slow connection shows
// progress rather than one big wait at submit time.
// ---------------------------------------------------------------------------
var portraitState = { uploaded: false, uploading: false };
var galleryState = { count: 0 };

function checkPhotoFile(file, warningEl) {
    warningEl.hidden = true;
    if (!["image/jpeg", "image/png"].includes(file.type)) {
        warningEl.textContent = "Please choose a JPEG or PNG photo.";
        warningEl.hidden = false;
        return false;
    }
    if (file.size > MAX_PHOTO_BYTES) {
        warningEl.textContent = "That photo is over 6MB — please choose a smaller one.";
        warningEl.hidden = false;
        return false;
    }
    return true;
}

function checkPhotoDimensions(file, warningEl) {
    var img = new Image();
    var url = URL.createObjectURL(file);
    img.onload = function () {
        if (img.naturalWidth < MIN_PHOTO_WIDTH) {
            warningEl.textContent = "This photo is " + img.naturalWidth + "px wide — at least " + MIN_PHOTO_WIDTH + "px is best, but it'll still work.";
            warningEl.hidden = false;
        }
        URL.revokeObjectURL(url);
    };
    img.src = url;
}

function uploadPhoto(file, kind) {
    var formData = new FormData();
    formData.append("client_id", getClientId());
    formData.append("kind", kind);
    formData.append("file", file);
    return fetch(INTAKE_PHOTOS_URL, { method: "POST", body: formData })
        .then(function (resp) {
            if (!resp.ok) {
                throw new Error("upload_failed");
            }
            return resp.json();
        })
        .then(function (data) {
            if (!data || data.ok !== true) {
                throw new Error("upload_not_ok");
            }
            return data;
        });
}

function addThumb(gridEl, file, onRemove) {
    var wrap = document.createElement("div");
    wrap.className = "photo-thumb";
    var img = document.createElement("img");
    img.src = URL.createObjectURL(file);
    img.alt = "";
    wrap.appendChild(img);
    if (onRemove) {
        var removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "photo-thumb-remove";
        removeBtn.setAttribute("aria-label", "Remove photo");
        removeBtn.textContent = "×";
        removeBtn.addEventListener("click", function () {
            wrap.remove();
            onRemove();
        });
        wrap.appendChild(removeBtn);
    }
    gridEl.appendChild(wrap);
    return wrap;
}

function initPortraitUpload() {
    var dropzone = document.getElementById("portrait-dropzone");
    var input = document.getElementById("portrait-input");
    var thumbGrid = document.getElementById("portrait-thumb");
    var warningEl = document.getElementById("portrait-warning");
    if (!dropzone || !input) {
        return;
    }

    dropzone.addEventListener("click", function () {
        input.click();
    });

    input.addEventListener("change", function () {
        var file = input.files && input.files[0];
        if (!file) {
            return;
        }
        if (!checkPhotoFile(file, warningEl)) {
            input.value = "";
            return;
        }
        checkPhotoDimensions(file, warningEl);
        thumbGrid.innerHTML = "";
        addThumb(thumbGrid, file, function () {
            portraitState.uploaded = false;
            input.value = "";
        });
        portraitState.uploading = true;
        dropzone.textContent = "Uploading...";
        uploadPhoto(file, "portrait")
            .then(function () {
                portraitState.uploaded = true;
                dropzone.textContent = "Tap to choose a different photo";
            })
            .catch(function () {
                warningEl.textContent = "That photo didn't upload — please try again.";
                warningEl.hidden = false;
                dropzone.textContent = "Tap to choose a photo";
            })
            .finally(function () {
                portraitState.uploading = false;
            });
    });
}

function initGalleryUpload() {
    var dropzone = document.getElementById("gallery-dropzone");
    var input = document.getElementById("gallery-input");
    var thumbGrid = document.getElementById("gallery-thumb");
    var warningEl = document.getElementById("gallery-warning");
    var countEl = document.getElementById("gallery-count");
    if (!dropzone || !input) {
        return;
    }

    dropzone.addEventListener("click", function () {
        input.click();
    });

    function updateCount() {
        countEl.textContent = galleryState.count + " of " + MAX_GALLERY_PHOTOS + " added";
    }

    input.addEventListener("change", function () {
        var files = Array.prototype.slice.call(input.files || []);
        input.value = "";
        files.forEach(function (file) {
            if (galleryState.count >= MAX_GALLERY_PHOTOS) {
                warningEl.textContent = "You've reached the limit of " + MAX_GALLERY_PHOTOS + " photos.";
                warningEl.hidden = false;
                return;
            }
            if (!checkPhotoFile(file, warningEl)) {
                return;
            }
            checkPhotoDimensions(file, warningEl);
            galleryState.count += 1;
            updateCount();
            var thumb = addThumb(thumbGrid, file, function () {
                galleryState.count -= 1;
                updateCount();
            });
            thumb.classList.add("photo-thumb-uploading");
            uploadPhoto(file, "gallery")
                .then(function () {
                    thumb.classList.remove("photo-thumb-uploading");
                })
                .catch(function () {
                    thumb.remove();
                    galleryState.count -= 1;
                    updateCount();
                    warningEl.textContent = "One of your photos didn't upload — please try adding it again.";
                    warningEl.hidden = false;
                });
        });
    });
}

// ---------------------------------------------------------------------------
// Tier-conditional sections: show/hide, and keep `required` in sync so the
// browser's own validation never blocks a Landing Page submission on a
// Full Website-only field (or vice versa).
// ---------------------------------------------------------------------------
function setRequired(ids, isRequired) {
    ids.forEach(function (id) {
        var el = document.getElementById(id);
        if (el) {
            el.required = isRequired;
        }
    });
}

function initTierToggle() {
    var landingRadio = document.getElementById("tier-landing");
    var fullRadio = document.getElementById("tier-full");
    var fullSection = document.getElementById("full-website-section");
    if (!landingRadio || !fullRadio || !fullSection) {
        return;
    }

    function apply() {
        var isFull = fullRadio.checked;
        fullSection.hidden = !isFull;
        setRequired(["intake-bio"], isFull);
    }

    landingRadio.addEventListener("change", apply);
    fullRadio.addEventListener("change", apply);
    apply();
}

function initGumroadToggle() {
    var ownRadio = document.getElementById("gumroad-own");
    var setupRadio = document.getElementById("gumroad-setup");
    var otherRadio = document.getElementById("gumroad-other");
    var handleField = document.getElementById("gumroad-handle-field");
    var otherField = document.getElementById("other-payment-link-field");
    if (!ownRadio || !setupRadio || !otherRadio || !handleField || !otherField) {
        return;
    }
    function apply() {
        var showHandle = ownRadio.checked;
        var showOther = otherRadio.checked;
        handleField.hidden = !showHandle;
        otherField.hidden = !showOther;
        setRequired(["intake-gumroad-handle"], showHandle);
        setRequired(["intake-other-payment-link"], showOther);
    }
    ownRadio.addEventListener("change", apply);
    setupRadio.addEventListener("change", apply);
    otherRadio.addEventListener("change", apply);
}

function initSupportToggle() {
    var yesRadio = document.getElementById("support-yes");
    var noRadio = document.getElementById("support-no");
    var fields = document.getElementById("support-fields");
    if (!yesRadio || !noRadio || !fields) {
        return;
    }
    function apply() {
        var show = yesRadio.checked;
        fields.hidden = !show;
        setRequired(["intake-support-link", "intake-support-text"], show);
    }
    yesRadio.addEventListener("change", apply);
    noRadio.addEventListener("change", apply);
}

// ---------------------------------------------------------------------------
// Live-sites proof line - reads the current count fresh on every page load.
// Hidden until it reaches 2; the wording keeps counting up automatically.
// ---------------------------------------------------------------------------
function initProofLine() {
    var el = document.getElementById("services-proof-line");
    if (!el) {
        return;
    }
    fetch(LIVE_COUNT_URL)
        .then(function (resp) { return resp.json(); })
        .then(function (data) {
            var count = data && typeof data.count === "number" ? data.count : 0;
            if (count >= 2) {
                el.textContent = "Built for " + count + " artists.";
                el.hidden = false;
            }
        })
        .catch(function () {});
}

// ---------------------------------------------------------------------------
// Color preview + contrast check - WCAG relative luminance, used both to
// pick a readable text color for the live swatch (same math a future
// generated site would use to auto-adjust its own text color against
// whatever main color an artist picks) and to warn if the two picked
// colors are too close in brightness to read well against each other.
// ---------------------------------------------------------------------------
function hexToRgb(hex) {
    var clean = hex.replace("#", "");
    return {
        r: parseInt(clean.substr(0, 2), 16),
        g: parseInt(clean.substr(2, 2), 16),
        b: parseInt(clean.substr(4, 2), 16)
    };
}

function relativeLuminance(rgb) {
    function channel(c) {
        var s = c / 255;
        return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * channel(rgb.r) + 0.7152 * channel(rgb.g) + 0.0722 * channel(rgb.b);
}

function contrastRatio(hex1, hex2) {
    var l1 = relativeLuminance(hexToRgb(hex1));
    var l2 = relativeLuminance(hexToRgb(hex2));
    var lighter = Math.max(l1, l2);
    var darker = Math.min(l1, l2);
    return (lighter + 0.05) / (darker + 0.05);
}

function readableTextColor(bgHex) {
    return relativeLuminance(hexToRgb(bgHex)) > 0.179 ? "#000000" : "#ffffff";
}

function initColorPreview() {
    var mainInput = document.getElementById("intake-main-color");
    var accentInput = document.getElementById("intake-accent-color");
    var preview = document.getElementById("color-preview");
    var headingEl = document.getElementById("color-preview-heading");
    var textEl = document.getElementById("color-preview-text");
    var btnEl = document.getElementById("color-preview-btn");
    var warningEl = document.getElementById("color-contrast-warning");
    if (!mainInput || !accentInput || !preview) {
        return;
    }

    function apply() {
        var main = mainInput.value;
        var accent = accentInput.value;

        preview.style.backgroundColor = main;
        headingEl.style.color = accent;
        textEl.style.color = readableTextColor(main);
        btnEl.style.backgroundColor = accent;
        btnEl.style.color = readableTextColor(accent);

        // Contrast between the two colors themselves (accent text/buttons
        // shown against the main background) - warn under WCAG's 3:1
        // minimum for large text / UI components.
        warningEl.hidden = contrastRatio(main, accent) >= 3;
    }

    mainInput.addEventListener("input", apply);
    accentInput.addEventListener("input", apply);
    apply();
}

var URL_RE = /^https?:\/\/[^\s]+\.[^\s]+$/;
var DOMAIN_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/i;
var EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function initServicesForm() {
    var form = document.getElementById("intake-form");
    if (!form) {
        return;
    }

    initTierToggle();
    initGumroadToggle();
    initSupportToggle();
    initPortraitUpload();
    initGalleryUpload();
    initProofLine();
    initColorPreview();

    fireFunnelEvent("page_visit");

    var startedFired = false;
    form.addEventListener("input", function () {
        if (!startedFired) {
            startedFired = true;
            fireFunnelEvent("form_started");
        }
    }, { once: false });

    var errorEl = document.getElementById("intake-error");
    var successEl = document.getElementById("intake-success");
    var submitBtn = document.getElementById("intake-submit-btn");

    function showError(message, focusEl) {
        errorEl.textContent = message;
        errorEl.hidden = false;
        if (focusEl) {
            focusEl.focus();
        }
    }

    form.addEventListener("submit", function (e) {
        e.preventDefault();
        errorEl.hidden = true;

        if ((document.getElementById("intake-website") || {}).value) {
            // Honeypot tripped - behave like a normal submit, send nothing real.
            form.hidden = true;
            successEl.hidden = false;
            return;
        }

        var tierEl = form.querySelector('input[name="tier"]:checked');
        if (!tierEl) {
            showError("Please choose Landing Page or Full Website.");
            return;
        }
        var tier = tierEl.value;
        var isFull = tier === "full";

        var artistName = document.getElementById("intake-artist-name").value.trim();
        var contactEmail = document.getElementById("intake-contact-email").value.trim();
        var whatsapp = document.getElementById("intake-whatsapp").value.trim();
        var spotifyUrl = document.getElementById("intake-spotify").value.trim();
        var mainColor = document.getElementById("intake-main-color").value.trim();
        var accentColor = document.getElementById("intake-accent-color").value.trim();
        var domainPrimary = document.getElementById("intake-domain-primary").value.trim();
        var domainBackup = document.getElementById("intake-domain-backup").value.trim();
        var fanAlertEmail = document.getElementById("intake-fan-alert-email").value.trim();

        if (!artistName) {
            showError("Please fill in your artist name.", document.getElementById("intake-artist-name"));
            return;
        }
        if (!EMAIL_RE.test(contactEmail)) {
            showError("That contact email doesn't look right.", document.getElementById("intake-contact-email"));
            return;
        }
        if (!whatsapp) {
            showError("Please fill in your WhatsApp number.", document.getElementById("intake-whatsapp"));
            return;
        }
        if (!URL_RE.test(spotifyUrl)) {
            showError("Please paste a full Spotify artist link.", document.getElementById("intake-spotify"));
            return;
        }
        if (!DOMAIN_RE.test(domainPrimary) || !DOMAIN_RE.test(domainBackup)) {
            showError("Please enter both domain names without \"www\" or \"https://\".", document.getElementById("intake-domain-primary"));
            return;
        }
        if (!EMAIL_RE.test(fanAlertEmail)) {
            showError("That new-fan alert email doesn't look right.", document.getElementById("intake-fan-alert-email"));
            return;
        }

        var platformFields = ["apple_music_url", "youtube_url", "amazon_music_url", "facebook_url", "x_url", "tiktok_url", "instagram_url", "audiomack_url", "boomplay_url"];
        var platformValues = {};
        for (var i = 0; i < platformFields.length; i++) {
            var input = form.elements[platformFields[i]];
            var value = input ? input.value.trim() : "";
            if (value && !URL_RE.test(value)) {
                showError("One of your platform links doesn't look like a full URL.", input);
                return;
            }
            platformValues[platformFields[i]] = value;
        }

        var bio = "", gumroadMode = "", gumroadHandle = "", otherPaymentLink = "", supportEnabled = false, supportLink = "", supportText = "";

        if (isFull) {
            bio = document.getElementById("intake-bio").value.trim();
            if (!bio) {
                showError("Please fill in a short bio.", document.getElementById("intake-bio"));
                return;
            }
            if (!document.getElementById("intake-photo-consent").checked) {
                showError("Please confirm you own or have permission to use your photos.");
                return;
            }
            if (!portraitState.uploaded) {
                showError("Please add your portrait photo and wait for it to finish uploading.");
                return;
            }

            var gumroadEl = form.querySelector('input[name="gumroad_mode"]:checked');
            if (!gumroadEl) {
                showError("Please choose where fans should buy or support you.");
                return;
            }
            gumroadMode = gumroadEl.value;
            if (gumroadMode === "own") {
                gumroadHandle = document.getElementById("intake-gumroad-handle").value.trim();
                if (!gumroadHandle) {
                    showError("Please fill in your Gumroad account.", document.getElementById("intake-gumroad-handle"));
                    return;
                }
            } else if (gumroadMode === "other") {
                otherPaymentLink = document.getElementById("intake-other-payment-link").value.trim();
                if (!otherPaymentLink.startsWith("https://") || !URL_RE.test(otherPaymentLink)) {
                    showError("Your payment link must start with https://", document.getElementById("intake-other-payment-link"));
                    return;
                }
            }

            var supportEl = form.querySelector('input[name="support_enabled"]:checked');
            if (!supportEl) {
                showError("Please let us know whether to include a support/donation page.");
                return;
            }
            supportEnabled = supportEl.value === "yes";
            if (supportEnabled) {
                supportLink = document.getElementById("intake-support-link").value.trim();
                supportText = document.getElementById("intake-support-text").value.trim();
                if (!URL_RE.test(supportLink)) {
                    showError("Please paste a full support/donation link.", document.getElementById("intake-support-link"));
                    return;
                }
                if (!supportText) {
                    showError("Please add a short line about your support link.", document.getElementById("intake-support-text"));
                    return;
                }
            }
        }

        submitBtn.disabled = true;

        var payload = {
            client_id: getClientId(),
            website: (document.getElementById("intake-website") || {}).value || "",
            tier: tier,
            artist_name: artistName,
            contact_email: contactEmail,
            whatsapp: whatsapp,
            spotify_url: spotifyUrl,
            main_color: mainColor,
            accent_color: accentColor,
            domain_primary: domainPrimary,
            domain_backup: domainBackup,
            fan_alert_email: fanAlertEmail,
            bio: bio,
            gumroad_mode: gumroadMode,
            gumroad_handle: gumroadHandle,
            other_payment_link: otherPaymentLink,
            support_enabled: supportEnabled,
            support_link: supportLink,
            support_text: supportText
        };
        Object.keys(platformValues).forEach(function (key) {
            payload[key] = platformValues[key];
        });

        fetch(INTAKE_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
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
                form.hidden = true;
                successEl.hidden = false;
                fireFunnelEvent("form_submitted", tier);
                if (typeof gtag === "function") {
                    gtag("event", "intake_form_submit", { tier: tier });
                }
            })
            .catch(function () {
                showError("Something went wrong — please try again, or email moceol.ent@gmail.com directly.");
            })
            .finally(function () {
                submitBtn.disabled = false;
            });
    });
}
