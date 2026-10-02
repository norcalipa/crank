// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

import {getWorkspaceSnapshot, subscribeWorkspace} from '../workspace/store';
import {reconcileAccountKey} from './storage';

export interface AccountGateOptions {
    isAuthenticated: boolean;
    accountKey: string;
    workspaceMode: 'docked' | 'drawer' | 'sheet' | undefined;
    // Discards every in-memory private artefact (sign-out or account switch).
    onPurge: () => void;
}

/**
 * Account gate for the chat (issues #465, #479, #527): the synchronous
 * first-render reconcile of the trusted account key, the pending state while
 * the shared store does not yet know the account, the effective
 * authentication flag, and the purge/hydration listeners.
 */
export function useAccountGate({isAuthenticated, accountKey, workspaceMode, onPurge}: AccountGateOptions) {
    // Cross-account purge, synchronously, before the first render commits
    // (issue #465 review round 2). Conversation resume and
    // adoptPendingDraft() both read local storage from mount effects, which
    // run long before the async whoami hydration below can compare
    // `crank:last-account`; until it resolved, the *previous* account's
    // pending draft could be adopted into the new account's conversation.
    // Reconciling the trusted server-rendered key here happens first, so
    // there is nothing stale left to read. Idempotent: the second call of a
    // StrictMode double render sees the key already stored.
    const accountGuardRef = React.useRef(false);
    if (!accountGuardRef.current) {
        accountGuardRef.current = true;
        reconcileAccountKey(accountKey || (workspaceMode !== undefined
            && getWorkspaceSnapshot().account.status === 'authenticated'
            ? getWorkspaceSnapshot().account.key
            : ''));
    }
    // Account gate (issue #479): a lazily mounted workspace chat has no
    // server-rendered accountKey, so until the shared store knows the account
    // (crank:auth-hydrated) it must not adopt a pending draft or read stored
    // drafts. The store is read at mount, so a mount after hydration is not
    // left waiting for an event it can no longer receive.
    const workspaceAccount = React.useSyncExternalStore(
        subscribeWorkspace,
        () => getWorkspaceSnapshot().account,
    );
    const accountPending = workspaceMode !== undefined
        && !accountKey
        && workspaceAccount.status === 'unknown';

    const [effectiveAuthenticated, setEffectiveAuthenticated] = React.useState(isAuthenticated);

    const onPurgeRef = React.useRef(onPurge);
    onPurgeRef.current = onPurge;

    React.useEffect(() => {
        // Sign-out (issue #465 AC-9): app-nav.js purges storage and
        // dispatches this directly before its redirect.
        const handlePurged = () => onPurgeRef.current();
        // Account switch while this page is already open (issue #465 AC-9):
        // app-nav.js's whoami hydration dispatches this on every load, and
        // it is the only signal for a switch that happened after the server
        // rendered `accountKey`. The load-time case is already handled
        // synchronously by reconcileAccountKey() above — this covers the
        // rest, using the same comparison so the two cannot disagree.
        const handleHydrated = (e: Event) => {
            const detail = (e as CustomEvent).detail as {authenticated?: boolean; username?: string; unobserved?: boolean} | undefined;
            // A failed whoami says nothing about the account: keep the
            // current auth state so the signed-in draft never moves to the
            // shared anonymous `pending` slot.
            if (!detail || detail.unobserved) return;
            setEffectiveAuthenticated(!!detail.authenticated);
            if (!detail.authenticated || !detail.username) return;
            if (reconcileAccountKey(detail.username)) {
                onPurgeRef.current();
            }
        };
        document.addEventListener('crank:private-state-purged', handlePurged);
        document.addEventListener('crank:auth-hydrated', handleHydrated);
        return () => {
            document.removeEventListener('crank:private-state-purged', handlePurged);
            document.removeEventListener('crank:auth-hydrated', handleHydrated);
        };
    }, []);

    return {accountPending, effectiveAuthenticated, setEffectiveAuthenticated, workspaceAccount};
}
