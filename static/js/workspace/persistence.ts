// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Per-tab workspace persistence (issue #479). Writes only `visibility` and
// the entity part of `context` plus an account stamp to sessionStorage — never
// priorities, drafts, conversation ids or compensation values. The record is
// restored only after the account is known and matches, and is deleted on
// account mismatch or `crank:private-state-purged`.

import {
    getWorkspaceSnapshot,
    normalizeWorkspaceContext,
    replaceWorkspaceState,
    setWorkspaceAccount,
    subscribeWorkspace,
} from './store';
import {currentMode} from './useWorkspaceLayout';
import {
    AssistantVisibility,
    WorkspaceAccount,
    WorkspaceContext,
    WORKSPACE_SESSION_KEY,
} from './types';

interface WorkspaceRecord {
    v: 1;
    account: WorkspaceAccount;
    visibility: AssistantVisibility;
    context: Partial<WorkspaceContext>;
    savedAt: number;
}

// One-way digest of the account key for the persisted record: it is only
// ever compared for equality, so the raw username need not sit in storage.
function accountDigest(text: string): string {
    if (!text) {
        return '';
    }
    let h1 = 0xdeadbeef;
    let h2 = 0x41c6ce57;
    for (let i = 0; i < text.length; i++) {
        const ch = text.charCodeAt(i);
        h1 = Math.imul(h1 ^ ch, 2654435761);
        h2 = Math.imul(h2 ^ ch, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return `d:${(h2 >>> 0).toString(16).padStart(8, '0')}${(h1 >>> 0).toString(16).padStart(8, '0')}`;
}

function readRecord(): WorkspaceRecord | null {
    try {
        const raw = window.sessionStorage.getItem(WORKSPACE_SESSION_KEY);
        if (!raw) {
            return null;
        }
        const parsed = JSON.parse(raw);
        if (!parsed || parsed.v !== 1 || !parsed.account
            || !['anonymous', 'authenticated'].includes(parsed.account.status)
            || typeof parsed.account.key !== 'string'
            || !['open', 'minimized', 'closed'].includes(parsed.visibility)) {
            return null;
        }
        return {
            v: 1,
            account: {status: parsed.account.status, key: parsed.account.key},
            visibility: parsed.visibility,
            context: entityContext(normalizeWorkspaceContext(parsed.context) as WorkspaceContext),
            savedAt: typeof parsed.savedAt === 'number' ? parsed.savedAt : 0,
        };
    } catch {
        return null;
    }
}

function deleteRecord(): void {
    try {
        window.sessionStorage.removeItem(WORKSPACE_SESSION_KEY);
    } catch {
        // Storage unavailable; nothing durable to delete.
    }
}

function entityContext(context: WorkspaceContext | null): Partial<WorkspaceContext> {
    if (!context) {
        return {};
    }
    const {organizationId, organizationName, jobId, comparisonIds} = context;
    const entity: Partial<WorkspaceContext> = {};
    if (organizationId !== undefined) entity.organizationId = organizationId;
    if (organizationName !== undefined) entity.organizationName = organizationName;
    if (jobId !== undefined) entity.jobId = jobId;
    if (comparisonIds !== undefined) entity.comparisonIds = comparisonIds;
    return entity;
}

// Only called once the account is known (`restored` gates every caller).
function writeRecord(): void {
    const {account, visibility, context} = getWorkspaceSnapshot();
    const record: WorkspaceRecord = {
        v: 1,
        account: {status: account.status, key: accountDigest(account.key)},
        visibility,
        context: entityContext(context),
        savedAt: Date.now(),
    };
    try {
        window.sessionStorage.setItem(WORKSPACE_SESSION_KEY, JSON.stringify(record));
    } catch {
        // Private mode / quota: state degrades to memory only.
    }
}

// Keeps the page's own surface and drops every entity. The panel closes
// unless the page is the pinned /chat/ host, where the assistant is the
// page's primary content and a cross-tab sign-out must not remove it.
function resetStore(): void {
    const surface = getWorkspaceSnapshot().context?.surface;
    const host = document.getElementById('assistant-workspace');
    const pinned = host?.dataset.assistantPinned === 'true';
    replaceWorkspaceState(pinned ? 'open' : 'closed', surface ? {surface} : null);
}

let installed = false;

export function installWorkspacePersistence(): () => void {
    if (installed) {
        return () => undefined;
    }
    installed = true;
    let restored = false;

    const tryRestore = (): void => {
        const {account} = getWorkspaceSnapshot();
        if (restored || account.status === 'unknown') {
            return;
        }
        restored = true;
        const record = readRecord();
        if (!record) {
            writeRecord();
            return;
        }
        // A pre-digest record holds the raw key; accept it once as the same
        // account so the upgrade does not drop the restored state.
        const keyMatches = record.account.key === accountDigest(account.key)
            || record.account.key === account.key;
        if (record.account.status !== account.status || !keyMatches) {
            deleteRecord();
            writeRecord();
            return;
        }
        const snapshot = getWorkspaceSnapshot();
        const surface = snapshot.context?.surface;
        // Explicit page context wins: when the page already names an entity
        // (e.g. ?company=12) the persisted entity group is dropped whole, so
        // a stale id/name pair can never be field-merged with the URL's.
        // The one exception: the same company id, whose persisted name is
        // consistent with it and which the URL alone cannot supply.
        const pageEntity = entityContext(snapshot.context);
        let entity: Partial<WorkspaceContext> = record.context;
        if (Object.keys(pageEntity).length) {
            const sameCompany = pageEntity.organizationId !== undefined
                && pageEntity.organizationId === record.context.organizationId
                && pageEntity.organizationName === undefined
                && record.context.organizationName !== undefined;
            entity = sameCompany ? {organizationName: record.context.organizationName} : {};
        }
        const context = {...(snapshot.context ?? {}), ...entity} as WorkspaceContext;
        if (!surface && !Object.keys(entity).length) {
            writeRecord();
            return;
        }
        // A pinned page has already opened the panel; never downgrade it.
        let visibility = snapshot.visibility;
        if (visibility === 'closed' && record.visibility !== 'closed') {
            visibility = record.visibility === 'open' && currentMode() === 'sheet'
                ? 'minimized'
                : record.visibility;
        }
        replaceWorkspaceState(visibility, Object.keys(context).length ? context : null);
    };

    // Account unknown first so the write-on-change subscriber stays silent
    // while the store resets; the record is then deleted for good.
    function wipe(): void {
        restored = false;
        setWorkspaceAccount({status: 'unknown', key: ''});
        resetStore();
        deleteRecord();
    }

    const handleHydrated = (event: Event): void => {
        const detail = (event as CustomEvent).detail as
            {authenticated?: boolean; username?: string | null; unobserved?: boolean} | undefined;
        // A failed whoami says nothing about the account: keep the current
        // record and store rather than treating it as a sign-out.
        if (!detail || detail.unobserved) {
            return;
        }
        const next: WorkspaceAccount = detail.authenticated && detail.username
            ? {status: 'authenticated', key: detail.username}
            : {status: 'anonymous', key: ''};
        const prev = getWorkspaceSnapshot().account;
        if (prev.status !== 'unknown' && (prev.status !== next.status || prev.key !== next.key)) {
            wipe();
        }
        setWorkspaceAccount(next);
        tryRestore();
    };

    const handlePurged = wipe;

    const unsubscribe = subscribeWorkspace(() => {
        if (restored) {
            writeRecord();
        }
    });
    document.addEventListener('crank:auth-hydrated', handleHydrated);
    document.addEventListener('crank:private-state-purged', handlePurged);
    tryRestore();

    return () => {
        installed = false;
        unsubscribe();
        document.removeEventListener('crank:auth-hydrated', handleHydrated);
        document.removeEventListener('crank:private-state-purged', handlePurged);
    };
}
