// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {purgePrivateClientState} from '../authIntent';
import {accountDigest} from '../workspace/persistence';

// Durable in-flight turn markers (issue #458). Written before a submission
// resolves so a request that never received a response survives reload and
// navigation, and reconciled against the server on the next load: the server
// is the single source of truth, the markers only cover the window it cannot
// see. Each turn gets its own storage key (conversation id + turn key) so
// concurrent turns or tabs never overwrite each other's recovery state and
// resolving one turn never clears another turn's marker.
export interface InFlightTurn {conversationId: number; content: string; key: string; ts: number}

export const INFLIGHT_PREFIX = 'crank:jobsearch:inflight:';
// Markers older than a day cannot still be in flight; prune them so storage
// cannot grow without bound.
export const INFLIGHT_MAX_AGE_MS = 24 * 60 * 60 * 1000;

export function inflightStorageKey(conversationId: number, key: string): string {
    return `${INFLIGHT_PREFIX}${conversationId}:${key}`;
}

export function draftKey(conversationId: number): string {
    return `crank:jobsearch:draft:${conversationId}`;
}

// Draft typed before the first conversation exists (issue #465): a
// signed-out visitor has no conversation id to key a draft against, so this
// single slot holds their in-progress text until the first conversation
// created after sign-in adopts it. Never sent anywhere — draft text stays
// out of every URL, including the sign-in `next`.
export const PENDING_DRAFT_KEY = 'crank:jobsearch:draft:pending';

export function readPendingDraft(): string {
    try {
        return window.localStorage.getItem(PENDING_DRAFT_KEY) || '';
    } catch {
        return '';
    }
}

export function writePendingDraft(text: string): void {
    try {
        if (text) {
            window.localStorage.setItem(PENDING_DRAFT_KEY, text);
        } else {
            window.localStorage.removeItem(PENDING_DRAFT_KEY);
        }
    } catch {
        // Storage unavailable; the draft stays memory-only.
    }
}

export function clearPendingDraft(): void {
    try {
        window.localStorage.removeItem(PENDING_DRAFT_KEY);
    } catch {
        // Storage unavailable; nothing durable to clear.
    }
}

// Name of the account that last wrote private artefacts into this browser's
// storage. Written by both the synchronous server-rendered reconciliation
// below and the asynchronous whoami hydration, which agree on the value
// (a digest of Django's username) so either can detect a switch the other
// missed. A legacy raw username is read as the same account and rewritten.
export const LAST_ACCOUNT_KEY = 'crank:last-account';

/**
 * Purge every private artefact when `accountKey` differs from the account
 * that last used this browser, then record `accountKey` as the current one.
 * Returns whether a purge happened.
 *
 * Deliberately synchronous (issue #465 AC-9/10): the resume fetch and
 * `adoptPendingDraft()` read storage from mount effects, so any check that
 * waits on the async whoami round trip loses the race and the previous
 * account's pending draft can surface in the new account's conversation.
 * An empty `accountKey` (signed-out render, or a caller with no trusted
 * discriminator) is a no-op — there is nothing to compare against, and
 * sign-out purges on its way out.
 */
export function reconcileAccountKey(accountKey: string): boolean {
    if (!accountKey) return false;
    const digest = accountDigest(accountKey);
    let lastAccount: string | null = null;
    try {
        lastAccount = window.localStorage.getItem(LAST_ACCOUNT_KEY);
    } catch {
        // Storage unavailable: nothing durable was stored for any account,
        // so there is nothing to leak and nothing to record.
        return false;
    }
    // A pre-digest raw username for the same account is not a switch; it is
    // rewritten as a digest below.
    const switched = !!lastAccount && lastAccount !== digest && lastAccount !== accountKey;
    if (switched) {
        purgePrivateClientState();
    }
    try {
        window.localStorage.setItem(LAST_ACCOUNT_KEY, digest);
    } catch {
        // Storage unavailable; switch detection cannot persist across
        // reloads, but nothing durable exists to expose either.
    }
    return switched;
}

// Timestamp of the last composer-draft write (issue #458 r2): lets
// reconciliation tell a marker written at a failed send apart from a draft
// the user typed/edited afterwards, so surfacing a recovered marker never
// clobbers the user's latest typing. A draft stored without a timestamp
// (legacy) counts as older than any marker.
export function draftTsKey(conversationId: number): string {
    return `crank:jobsearch:draftts:${conversationId}`;
}

export function readComposerDraftTs(conversationId: number): number {
    try {
        return Number(window.localStorage.getItem(draftTsKey(conversationId))) || 0;
    } catch {
        return 0;
    }
}

export function writeInflightTurn(turn: InFlightTurn): void {
    try {
        window.localStorage.setItem(
            inflightStorageKey(turn.conversationId, turn.key),
            JSON.stringify(turn),
        );
    } catch {
        // Storage unavailable (private mode/quota); the turn still works,
        // it just is not durable across reloads.
    }
}

export function clearInflightTurn(conversationId: number, key: string): void {
    try {
        window.localStorage.removeItem(inflightStorageKey(conversationId, key));
    } catch {
        // Storage unavailable; nothing durable to clear.
    }
}

// Clear every marker belonging to one conversation (reset/delete/gone).
// Markers are keyed per conversation, so this can never destroy another
// conversation's recovery state.
export function clearInflightTurns(conversationId: number): void {
    try {
        const doomed: string[] = [];
        for (let i = 0; i < window.localStorage.length; i++) {
            const storageKey = window.localStorage.key(i);
            if (storageKey && storageKey.startsWith(`${INFLIGHT_PREFIX}${conversationId}:`)) {
                doomed.push(storageKey);
            }
        }
        doomed.forEach((storageKey) => window.localStorage.removeItem(storageKey));
    } catch {
        // Storage unavailable; nothing durable to clear.
    }
}

// Every still-live marker for a conversation, pruning stale/corrupt ones.
export function readInflightTurns(conversationId: number): InFlightTurn[] {
    const turns: InFlightTurn[] = [];
    try {
        const expired: string[] = [];
        const now = Date.now();
        for (let i = 0; i < window.localStorage.length; i++) {
            const storageKey = window.localStorage.key(i);
            if (!storageKey || !storageKey.startsWith(INFLIGHT_PREFIX)) continue;
            try {
                const turn = JSON.parse(
                    window.localStorage.getItem(storageKey) || '',
                ) as InFlightTurn;
                if (!turn || typeof turn.conversationId !== 'number' || typeof turn.key !== 'string') {
                    expired.push(storageKey);
                    continue;
                }
                if (now - (turn.ts || 0) > INFLIGHT_MAX_AGE_MS) {
                    expired.push(storageKey);
                    continue;
                }
                if (turn.conversationId === conversationId) turns.push(turn);
            } catch {
                expired.push(storageKey);
            }
        }
        expired.forEach((storageKey) => window.localStorage.removeItem(storageKey));
    } catch {
        // Storage unavailable; markers simply are not durable.
    }
    return turns;
}

export function readComposerDraft(conversationId: number): string {
    try {
        return window.localStorage.getItem(draftKey(conversationId)) || '';
    } catch {
        return '';
    }
}

export function writeComposerDraft(conversationId: number | null, text: string): void {
    if (conversationId === null) return;
    try {
        if (text) {
            window.localStorage.setItem(draftKey(conversationId), text);
            window.localStorage.setItem(draftTsKey(conversationId), String(Date.now()));
        } else {
            window.localStorage.removeItem(draftKey(conversationId));
            window.localStorage.removeItem(draftTsKey(conversationId));
        }
    } catch {
        // Storage unavailable; the draft stays memory-only.
    }
}
