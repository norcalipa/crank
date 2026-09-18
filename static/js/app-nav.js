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

// Per-user nav hydration (issue #470): the shared page cache serves the same
// shell to every account, so the shell embeds no username, no session-bound
// CSRF token, and no auth-dependent visibility. The client hydrates all
// auth-dependent state per request from the uncached whoami endpoint:
// account labels, the authenticated vs anonymous control groups, the admin
// link, and the React organization list's auth flags.
(function () {
    "use strict";

    function setVisible(selector, visible) {
        document.querySelectorAll(selector).forEach(function (element) {
            if (visible) {
                element.removeAttribute("hidden");
            } else {
                element.setAttribute("hidden", "hidden");
            }
        });
    }

    function csrfToken() {
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : null;
    }

    function submitLogoutWithToken(event) {
        // The shared cached shell carries no session-bound CSRF token; the
        // whoami response guarantees a CSRF cookie before this form is
        // revealed, so the logout POST passes the same token check as any
        // other form.
        event.preventDefault();
        var form = event.currentTarget;
        fetch(form.action, {
            method: "POST",
            credentials: "same-origin",
            headers: { "X-CSRFToken": csrfToken() || "" },
        })
            .then(function () {
                window.location.href = "/";
            })
            .catch(function () {
                window.location.href = "/";
            });
    }

    function applyAuthState(data) {
        var authenticated = !!(data && data.authenticated);
        setVisible("[data-nav-auth-only]", authenticated);
        setVisible("[data-nav-anon-only]", !authenticated);
        if (authenticated) {
            document.querySelectorAll("[data-nav-user-label]").forEach(function (label) {
                label.textContent = data.username;
            });
            var list = document.getElementById("organization-list");
            if (list) {
                list.dataset.authenticated = "true";
            }
            var config = document.getElementById("organization-list-config");
            if (config) {
                config.setAttribute("data-can-suggest-company", "true");
            }
        }
        // The React list mounts from the dataset attributes before this
        // async fetch resolves, so notify it to re-render with the hydrated
        // auth state.
        document.dispatchEvent(new CustomEvent("crank:auth-hydrated"));
    }

    function hydrateAccountState() {
        fetch("/api/account/whoami/", {
            headers: { Accept: "application/json" },
            credentials: "same-origin",
        })
            .then(function (response) {
                return response.ok ? response.json() : null;
            })
            .then(function (data) {
                applyAuthState(data);
            })
            .catch(function () {
                // Hydration must never break the nav: fall back to the
                // anonymous controls so Login stays reachable.
                applyAuthState({ authenticated: false });
            });
        document.querySelectorAll("form[data-nav-js-logout]").forEach(function (form) {
            form.addEventListener("submit", submitLogoutWithToken);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", hydrateAccountState);
    } else {
        hydrateAccountState();
    }
})();
