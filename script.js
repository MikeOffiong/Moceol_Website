// Moceol — shared site script
// Handles the responsive navbar hamburger menu on every page.

document.addEventListener("DOMContentLoaded", function () {
    var yearEl = document.getElementById("copyright-year");
    if (yearEl) {
        yearEl.textContent = new Date().getFullYear();
    }

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
