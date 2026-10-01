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
            window.sessionStorage.removeItem("crank:workspace:v1");
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

    // Cross-tab signal (issue #479): private state lives in each tab's memory
    // and sessionStorage, so a logout, login or account switch here must reach
    // every other open tab. It is sent only from a hydration that has observed
    // the changed account (i.e. after the server processed the logout/login),
    // never from the submit handler: a receiving tab re-hydrates immediately
    // and would otherwise still be told the old account is signed in.
    // localStorage writes raise a `storage` event in the *other* tabs; the
    // value is an opaque nonce, never account data. Storage keys owned here:
    //   crank:account-epoch    localStorage, shared; a random nonce (no
    //                          timestamp) that is only ever overwritten,
    //                          never read for its value and never removed
    //                          by any purge.
    //   crank:nav-account-seen sessionStorage, per tab; "u:<digest>" or
    //                          "anon", where <digest> is a short one-way
    //                          hash of the username (equality checks only).
    //                          Deliberately not cleared by either purge: it
    //                          is what detects the next change. A legacy
    //                          raw "u:<username>" value is read as the same
    //                          account once and rewritten as a digest.
    function randomNonce() {
        var cryptoApi = window.crypto;
        if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
            return cryptoApi.randomUUID();
        }
        return Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
    }

    // Synchronous 64-bit cyrb53-style digest: stable across loads and
    // available in non-secure contexts, where crypto.subtle is not.
    function accountDigest(text) {
        var h1 = 0xdeadbeef;
        var h2 = 0x41c6ce57;
        for (var i = 0; i < text.length; i++) {
            var ch = text.charCodeAt(i);
            h1 = Math.imul(h1 ^ ch, 2654435761);
            h2 = Math.imul(h2 ^ ch, 1597334677);
        }
        h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
        h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
        return ("00000000" + (h2 >>> 0).toString(16)).slice(-8)
            + ("00000000" + (h1 >>> 0).toString(16)).slice(-8);
    }

    function announceAccountChange() {
        try {
            var nonce = randomNonce();
            window.localStorage.setItem("crank:account-epoch", nonce);
            lastSeenEpoch = nonce;
        } catch (e) {
            // Storage unavailable; other tabs cannot be signalled.
        }
    }

    // This tab's last hydrated account, tab-local. A hydration that differs
    // from it is the moment the account change is known to have happened.
    var ACCOUNT_SEEN_KEY = "crank:nav-account-seen";
    var quietNextHydration = false;

    // Both stay null until this document has observed an account through
    // readable storage; null means "unknown", never "changed".
    var lastObservedAccount = null;
    var lastObservedUsername = null;

    // The shared epoch nonce this document last saw (at load, after its own
    // announcement, or from a storage event). undefined = storage unreadable.
    function readAccountEpoch() {
        try {
            return window.localStorage.getItem("crank:account-epoch");
        } catch (e) {
            return undefined;
        }
    }
    var lastSeenEpoch = readAccountEpoch();

    function noteHydratedAccount(authenticated, username) {
        var current = authenticated ? "u:" + accountDigest(username) : "anon";
        var previous = null;
        var readable = true;
        try {
            previous = window.sessionStorage.getItem(ACCOUNT_SEEN_KEY);
            window.sessionStorage.setItem(ACCOUNT_SEEN_KEY, current);
        } catch (e) {
            // Storage unavailable; the account change cannot be detected.
            readable = false;
        }
        lastObservedAccount = readable ? current : null;
        lastObservedUsername = authenticated ? username : null;
        if (authenticated && previous === "u:" + username) {
            // Pre-digest value for the same account: not a change.
            previous = current;
        }
        var quiet = quietNextHydration;
        quietNextHydration = false;
        if (!quiet && previous !== null && previous !== current) {
            announceAccountChange();
        }
    }

    // Another tab changed the account: discard this tab's own state, then
    // re-hydrate so the nav and workspace pick up the account the cookie now
    // belongs to. The signal is only sent after the change completed, so the
    // whoami below already sees the new account. The next hydration is quiet
    // so tabs cannot ping-pong.
    function handleAccountEpoch(event) {
        if (event.key !== "crank:account-epoch") {
            return;
        }
        lastSeenEpoch = event.newValue;
        // Only tab-local state: localStorage is shared, and the tab that
        // changed the account has already purged (sign-out) or reconciled
        // (account switch) it. Deleting it here would destroy the #465
        // sign-in handoff draft and same-account recovery markers.
        try {
            window.sessionStorage.removeItem("crank:auth-intent");
            window.sessionStorage.removeItem("crank:workspace:v1");
        } catch (e) {
            // Storage unavailable; nothing tab-local to clear.
        }
        document.dispatchEvent(new CustomEvent("crank:private-state-purged"));
        quietNextHydration = true;
        fetchWhoami();
    }
    window.addEventListener("storage", handleAccountEpoch);

    // A page restored from the back/forward cache never re-runs its scripts
    // and may not receive the `storage` events it missed while cached. Two
    // synchronous, network-free signals say the account changed meanwhile:
    // the shared epoch nonce differs from the one this document last saw, or
    // this tab's recorded account differs from the one this document
    // observed. Either purges tab-local state at once; an unknown account or
    // unreadable storage is no evidence. Whoami is then re-checked quietly:
    // purging here is tab-local only and never re-announces.
    window.addEventListener("pageshow", function (event) {
        if (!event.persisted) {
            return;
        }
        var epoch = readAccountEpoch();
        var epochChanged = epoch !== undefined && lastSeenEpoch !== undefined
            && epoch !== lastSeenEpoch;
        var seen = null;
        try {
            seen = window.sessionStorage.getItem(ACCOUNT_SEEN_KEY);
        } catch (e) {
            // Storage unavailable; the change cannot be compared.
        }
        var seenChanged = seen !== null && lastObservedAccount !== null
            && seen !== lastObservedAccount
            && seen !== "u:" + lastObservedUsername;
        if (epochChanged || seenChanged) {
            handleAccountEpoch({ key: "crank:account-epoch", newValue: epoch });
        } else {
            quietNextHydration = true;
            fetchWhoami();
        }
    });

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
        // A failed whoami (null / fallback) says nothing about the account.
        if (data && typeof data.authenticated === "boolean" && !data.unobserved) {
            noteHydratedAccount(authenticated, authenticated ? data.username : null);
        }
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
            // `unobserved`: the whoami request failed, so `authenticated` is only
            // the anonymous fallback for the nav controls, not a fact about the
            // account; listeners that persist state must ignore it.
            detail: {
                authenticated: authenticated,
                username: authenticated ? data.username : null,
                unobserved: !data || !!data.unobserved,
            },
        }));
    }

    function fetchWhoami() {
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
                applyAuthState({ authenticated: false, unobserved: true });
            });
    }

    function hydrateAccountState() {
        fetchWhoami();
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
