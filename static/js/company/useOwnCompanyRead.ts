// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {getWorkspaceSnapshot, subscribeWorkspace} from '../workspace/store';
import {isCompanyId} from './companyId';

export type OwnReadKind = 'fit' | 'jobs';

export type OwnReadState<T> =
    | {kind: 'loading'}
    | {kind: 'ready'; data: T}
    // No session: a redirect to the login page or a 401. Never data, and never
    // an error that cannot clear (issue #486 AC 16).
    | {kind: 'signed-out'}
    | {kind: 'error'};

interface Held<T> {
    id: number | null;
    state: OwnReadState<T>;
}

const LOADING: OwnReadState<never> = {kind: 'loading'};

/**
 * One session read about one company, for the signed-in reader only
 * (`/api/organizations/<id>/fit/`, `/jobs/`).
 *
 * What it holds belongs to an account, so it is never stored outside this
 * component: a `crank:private-state-purged` event or an account change in the
 * workspace store empties it and makes any answer still in flight ignorable,
 * and an answer for another company is never returned (issue #486, rule 4).
 * Nothing is requested for an id that is not a company id, or when `enabled`
 * is false. The caller never sees a thrown error's text.
 *
 * With `followPriorities`, a saved change of the reader's priorities (the
 * workspace store's `prioritiesRevision` becoming a number it was not) reads
 * again. What is on screen stays, flagged `refreshing`, until the new answer
 * replaces it; an answer started before the change is never shown after it. A
 * revision of `null` (purge, account switch) never starts a read.
 */
export function useOwnCompanyRead<T>(
    kind: OwnReadKind,
    requestedId: number | null,
    enabled: boolean,
    isValid: (data: unknown) => data is T,
    followPriorities = false,
): {state: OwnReadState<T>; retry: () => void; refreshing: boolean} {
    const organizationId = isCompanyId(requestedId) ? requestedId : null;
    const [held, setHeld] = React.useState<Held<T>>({id: null, state: LOADING});
    const [attempt, setAttempt] = React.useState(0);
    const [refreshing, setRefreshing] = React.useState(false);
    const heldRef = React.useRef(held);
    heldRef.current = held;
    // Set when the next run of the read is a re-read after a priorities change.
    const quiet = React.useRef(false);
    // Counts account changes: an answer started before one is not shown after it.
    const epoch = React.useRef(0);

    React.useEffect(() => {
        const clear = () => {
            epoch.current += 1;
            setHeld({id: organizationId, state: {kind: 'signed-out'}});
        };
        let seen = getWorkspaceSnapshot().account;
        const unsubscribe = subscribeWorkspace(() => {
            const account = getWorkspaceSnapshot().account;
            const previous = seen;
            seen = account;
            if (previous.status !== 'unknown'
                && (previous.status !== account.status || previous.key !== account.key)) {
                clear();
            }
        });
        document.addEventListener('crank:private-state-purged', clear);
        return () => {
            unsubscribe();
            document.removeEventListener('crank:private-state-purged', clear);
        };
    }, [organizationId]);

    React.useEffect(() => {
        if (!followPriorities) return undefined;
        let seen = getWorkspaceSnapshot().prioritiesRevision;
        return subscribeWorkspace(() => {
            const revision = getWorkspaceSnapshot().prioritiesRevision;
            if (revision === null) {
                seen = null;
            } else if (revision !== seen) {
                seen = revision;
                const shown = heldRef.current;
                // Nothing of an account that is gone is read again.
                if (shown.state.kind === 'signed-out') return;
                quiet.current = shown.state.kind === 'ready';
                setAttempt(count => count + 1);
            }
        });
    }, [followPriorities]);

    React.useEffect(() => {
        const keep = quiet.current;
        quiet.current = false;
        if (organizationId === null || !enabled) {
            setHeld({id: organizationId, state: LOADING});
            return undefined;
        }
        let cancelled = false;
        const startedIn = epoch.current;
        const current = () => !cancelled && startedIn === epoch.current;
        if (keep) {
            setRefreshing(true);
        } else {
            setRefreshing(false);
            setHeld({id: organizationId, state: LOADING});
        }
        fetch(`/api/organizations/${organizationId}/${kind}/`, {
            credentials: 'same-origin',
            headers: {Accept: 'application/json'},
        })
            .then(async response => {
                if (response.redirected || response.status === 401) return {kind: 'signed-out'} as OwnReadState<T>;
                if (!response.ok) return {kind: 'error'} as OwnReadState<T>;
                let body: unknown;
                try {
                    body = await response.json();
                } catch {
                    // A login page followed into HTML is not JSON.
                    return {kind: 'signed-out'} as OwnReadState<T>;
                }
                return isValid(body) ? {kind: 'ready', data: body} as OwnReadState<T> : {kind: 'error'} as OwnReadState<T>;
            })
            .catch(() => ({kind: 'error'}) as OwnReadState<T>)
            .then(state => {
                if (current()) {
                    setRefreshing(false);
                    setHeld({id: organizationId, state});
                }
            });
        return () => {
            cancelled = true;
        };
    // `isValid` is a module-level guard in the callers.
    }, [kind, organizationId, enabled, attempt]);

    const retry = React.useCallback(() => setAttempt(count => count + 1), []);
    // Before the effect for a new id has run, its read is as good as started.
    const state = held.id === organizationId ? held.state : (LOADING as OwnReadState<T>);
    return {state, retry, refreshing: refreshing && state.kind === 'ready'};
}

/**
 * A Retry button is replaced by the loading placeholder the moment it is
 * pressed, which would drop the keyboard focus to the page body. Move it to the
 * section's own heading (already focusable) and read again. The heading is scrolled
 * into view with the view's scroll margin, so it is on screen, below the top bar.
 */
export function retryFrom(headingId: string, retry: () => void): void {
    document.getElementById(headingId)?.focus();
    retry();
}

/**
 * Keeps a reader who has scrolled past a section where they are when the
 * section's answer changes its height. Chromium does not anchor the scroll
 * position inside a size container (`.company-view`), so what follows the
 * section would jump by the change in height; Firefox corrects it itself, which
 * is why the movement is measured on screen rather than assumed.
 *
 * The position of what follows the section is read while rendering (before the
 * new content is in the page) and again after the commit. When the whole section
 * was above the top of the screen and what follows it moved, the page is
 * scrolled by the same amount.
 */
export function useHoldReaderBelow(sectionRef: React.RefObject<HTMLElement>, dependency: unknown): void {
    const before = React.useRef<{bottom: number; next: number} | null>(null);
    const section = sectionRef.current;
    if (section) {
        const bottom = section.getBoundingClientRect().bottom;
        before.current = {bottom, next: section.nextElementSibling?.getBoundingClientRect().top ?? bottom};
    }
    React.useLayoutEffect(() => {
        const element = sectionRef.current;
        const was = before.current;
        if (!element || !was) return;
        const next = element.nextElementSibling?.getBoundingClientRect().top ?? element.getBoundingClientRect().bottom;
        const moved = next - was.next;
        if (was.bottom <= 0 && Math.abs(moved) >= 1) window.scrollBy({top: moved, behavior: 'instant' as ScrollBehavior});
    }, [dependency, sectionRef]);
}
