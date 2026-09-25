// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
/**
 * Sign-in intent handoff and private-state purge (issue #465).
 *
 * The intended route and company id are non-sensitive and travel through
 * sign-in in the validated `next` query built by `crank.auth.sign_in_url`;
 * `crank:auth-intent` is a client-side mirror of that same data so a
 * same-tab continuation can recover it without re-parsing the URL. Draft
 * text is sensitive and never goes here or in any URL — see
 * `crank:jobsearch:draft:pending` in JobSearchChat.tsx.
 */

export interface AuthIntent {
    route: string;
    companyId: number | null;
}

const INTENT_KEY = 'crank:auth-intent';
const WORKSPACE_KEY = 'crank:workspace:v1';
const JOBSEARCH_PREFIX = 'crank:jobsearch:';

export function writeIntent(intent: AuthIntent): void {
    try {
        window.sessionStorage.setItem(INTENT_KEY, JSON.stringify(intent));
    } catch {
        // Storage unavailable (private mode/quota); the intent simply does
        // not survive the round trip through sign-in.
    }
}

export function readIntent(): AuthIntent | null {
    try {
        const raw = window.sessionStorage.getItem(INTENT_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed.route !== 'string') return null;
        const companyId = typeof parsed.companyId === 'number' ? parsed.companyId : null;
        return {route: parsed.route, companyId};
    } catch {
        return null;
    }
}

export function clearIntent(): void {
    try {
        window.sessionStorage.removeItem(INTENT_KEY);
    } catch {
        // Storage unavailable; nothing durable to clear.
    }
}

/**
 * Remove every private client-side artefact: all `crank:jobsearch:` keys
 * (per-conversation drafts, the pending pre-conversation draft, and the
 * turn-recovery markers) plus the sign-in intent and the per-tab workspace record (issue #479). Called on sign-out and on
 * account switch (issue #465 AC-9) — never on a timer or plain page load,
 * since that would destroy legitimate same-account recovery state.
 */
export function purgePrivateClientState(): void {
    clearIntent();
    try {
        window.sessionStorage.removeItem(WORKSPACE_KEY);
    } catch {
        // Storage unavailable; nothing durable to purge.
    }
    try {
        const doomed: string[] = [];
        for (let i = 0; i < window.localStorage.length; i++) {
            const key = window.localStorage.key(i);
            if (key && key.startsWith(JOBSEARCH_PREFIX)) {
                doomed.push(key);
            }
        }
        doomed.forEach((key) => window.localStorage.removeItem(key));
    } catch {
        // Storage unavailable; nothing durable to purge.
    }
}
