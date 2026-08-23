// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Mobile navigation drawer: open/close, focus trapping, Escape dismissal,
// focus restoration, and reduced-motion handling.
(function () {
    "use strict";

    var toggle = document.querySelector("[data-nav-toggle]");
    var drawer = document.querySelector("[data-nav-drawer]");
    var overlay = document.querySelector("[data-nav-overlay]");
    var closeBtn = document.querySelector("[data-nav-close]");
    var skipLink = document.querySelector("[data-skip-to-content]");
    var mainContent = document.getElementById("main-content");

    if (!toggle || !drawer || !overlay) return;

    var lastFocused = null;

    function getFocusable(container) {
        return container.querySelectorAll(
            'a[href], button:not([disabled]), input:not([disabled]), ' +
            'select:not([disabled]), textarea:not([disabled]), ' +
            '[tabindex]:not([tabindex="-1"])'
        );
    }

    function openDrawer() {
        lastFocused = document.activeElement;
        drawer.hidden = false;
        overlay.hidden = false;
        toggle.setAttribute("aria-expanded", "true");
        document.body.classList.add("app-nav-drawer-open");
        var focusable = getFocusable(drawer);
        if (focusable.length) {
            focusable[0].focus();
        }
        document.addEventListener("keydown", handleKeydown);
    }

    function closeDrawer() {
        drawer.hidden = true;
        overlay.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
        document.body.classList.remove("app-nav-drawer-open");
        document.removeEventListener("keydown", handleKeydown);
        if (lastFocused && typeof lastFocused.focus === "function") {
            lastFocused.focus();
        }
    }

    function handleKeydown(e) {
        if (e.key === "Escape") {
            e.preventDefault();
            closeDrawer();
            return;
        }
        if (e.key === "Tab") {
            var focusable = getFocusable(drawer);
            if (!focusable.length) return;
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
    }

    toggle.addEventListener("click", openDrawer);
    if (closeBtn) {
        closeBtn.addEventListener("click", closeDrawer);
    }
    overlay.addEventListener("click", closeDrawer);

    if (skipLink && mainContent) {
        skipLink.addEventListener("click", function (e) {
            e.preventDefault();
            mainContent.focus();
            mainContent.scrollIntoView();
        });
    }
})();
