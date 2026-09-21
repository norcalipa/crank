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

    // Removes every private client-side artefact (issue #465 AC-9): all
    // `crank:jobsearch:` keys (drafts, turn-recovery markers) plus the
    // sign-in intent. This is the plain-JS twin of
    // static/js/authIntent.ts's purgePrivateClientState() — app-nav.js is a
    // standalone script (not a webpack entry), so it cannot import that
    // TS module and keeps its own copy instead.
    function purgePrivateClientState() {
        try {
            window.sessionStorage.removeItem("crank:auth-intent");
        } catch (e) {
            // Storage unavailable; nothing durable to clear.
        }
        try {
            var doomed = [];
            for (var i = 0; i < window.localStorage.length; i++) {
                var key = window.localStorage.key(i);
                if (key && key.indexOf("crank:jobsearch:") === 0) {
                    doomed.push(key);
                }
            }
            doomed.forEach(function (key) {
                window.localStorage.removeItem(key);
            });
        } catch (e) {
            // Storage unavailable; nothing durable to purge.
        }
    }

    // Discard every private artefact and tell the mounted chat to abort its
    // in-flight request (issue #465 AC-9). Runs for *both* logout shapes:
    // the cached shell's JS-submitted form and the server-rendered form on
    // an authenticated page such as /chat/, which posts natively. Binding
    // this only to the JS form (the pre-review behaviour) left drafts,
    // in-flight markers and the sign-in intent in storage for every
    // server-authenticated page.
    function purgeForLogout() {
        purgePrivateClientState();
        try {
            window.localStorage.removeItem("crank:last-account");
        } catch (e) {
            // Storage unavailable; nothing durable to clear.
        }
        document.dispatchEvent(new CustomEvent("crank:private-state-purged"));
    }

    function handleLogoutSubmit(event) {
        var form = event.currentTarget;
        // Purge before the request settles: sign-out must discard every
        // private artefact regardless of whether the POST succeeds.
        purgeForLogout();
        if (!form.hasAttribute("data-nav-js-logout")) {
            // Server-rendered form: it carries its own {% csrf_token %}, so
            // let the native POST proceed untouched. The purge above has
            // already run synchronously, before navigation.
            return;
        }
        // The shared cached shell carries no session-bound CSRF token; the
        // whoami response guarantees a CSRF cookie before this form is
        // revealed, so the logout POST passes the same token check as any
        // other form.
        event.preventDefault();
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
        // auth state. The username travels in the detail (issue #465 AC-9)
        // so a mounted JobSearchChat can compare it against
        // `crank:last-account` and purge on an account switch — the
        // compare-and-purge decision lives there, not here, since it is the
        // one place that can act on both the old and new value together.
        document.dispatchEvent(new CustomEvent("crank:auth-hydrated", {
            detail: { authenticated: authenticated, username: authenticated ? data.username : null },
        }));
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
        // Every logout form, not just the cached shell's JS-submitted one.
        document.querySelectorAll("form[data-nav-logout-form]").forEach(function (form) {
            form.addEventListener("submit", handleLogoutSubmit);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", hydrateAccountState);
    } else {
        hydrateAccountState();
    }
})();
