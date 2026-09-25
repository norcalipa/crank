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
        account: {status: account.status, key: account.key},
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

// Keeps the page's own surface, drops every entity and closes the panel.
function resetStore(): void {
    const surface = getWorkspaceSnapshot().context?.surface;
    replaceWorkspaceState('closed', surface ? {surface} : null);
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
        if (record.account.status !== account.status || record.account.key !== account.key) {
            deleteRecord();
            writeRecord();
            return;
        }
        const snapshot = getWorkspaceSnapshot();
        const surface = snapshot.context?.surface;
        const context = {...(snapshot.context ?? {}), ...record.context} as WorkspaceContext;
        if (!surface && !Object.keys(record.context).length) {
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
            {authenticated?: boolean; username?: string | null} | undefined;
        if (!detail) {
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
