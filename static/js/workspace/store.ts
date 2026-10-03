// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Cross-root workspace store (issue #472). The app mounts several
// independent React roots from separate webpack entries, and webpack gives
// each entry bundle its own module instance — so the store lives on
// `window.__crankWorkspace__` (versioned) and every bundle shares exactly
// one instance. Two window CustomEvents are the cross-bundle transport, the
// same mechanism #471 uses for `crank:suggest-company`.

import {
    AssistantVisibility,
    CompanyTargetHandler,
    FilterTargetHandler,
    PrioritiesEditorHost,
    PrioritiesEditorSeed,
    WorkspaceAccount,
    RtoPolicyCode,
    WireContext,
    WorkspaceContext,
    WorkspaceFilters,
    WorkspaceMode,
    WorkspaceSnapshot,
    WORKSPACE_CONTEXT_EVENT,
    WORKSPACE_OPEN_EVENT,
} from './types';

// Caps a hostile or malformed CustomEvent.detail from inflating the UI; the
// values are display-only and never reach an API payload.
const MAX_STRING_LENGTH = 200;

const VALID_SURFACES: readonly WorkspaceContext['surface'][] = [
    'rankings', 'company', 'jobs', 'comparison', 'help', 'chat',
];

const STORE_KEY = '__crankWorkspace__';
const STORE_VERSION = 2;

interface WorkspaceStoreShape {
    version: number;
    snapshot: WorkspaceSnapshot;
    listeners: Set<() => void>;
    filterTargets?: FilterTargetHandler[];
    companyTargets?: CompanyTargetHandler[];
}

declare global {
    interface Window {
        __crankWorkspace__?: WorkspaceStoreShape;
    }
}

function initialSnapshot(): WorkspaceSnapshot {
    return {
        visibility: 'closed',
        mode: 'sheet',
        context: null,
        loaded: false,
        contextRevision: 0,
        account: {status: 'unknown', key: ''},
        conversationId: null,
        userOpened: false,
        prioritiesRevision: null,
        prioritiesEditorOpenIn: null,
        prioritiesEditorSeed: null,
    };
}

function store(): WorkspaceStoreShape {
    if (!window.__crankWorkspace__ || window.__crankWorkspace__.version !== STORE_VERSION) {
        window.__crankWorkspace__ = {
            version: STORE_VERSION,
            snapshot: initialSnapshot(),
            listeners: new Set(),
        };
    }
    return window.__crankWorkspace__;
}

function notify(): void {
    for (const listener of store().listeners) {
        listener();
    }
}

function update(partial: Partial<WorkspaceSnapshot>): void {
    const s = store();
    s.snapshot = {...s.snapshot, ...partial};
    notify();
}

// Fixed key order, so equal contexts always serialize equally regardless of
// the order their keys were first set in.
const CONTEXT_KEY_ORDER: readonly (keyof WorkspaceContext)[] = [
    'surface', 'organizationId', 'organizationName', 'jobId', 'comparisonIds',
    'searchTerm', 'page', 'algorithmId', 'filters', 'resultGeneration', 'preferenceRevision',
];

function canonicalContext(context: WorkspaceContext | null): unknown {
    if (!context) {
        return null;
    }
    const ordered: Record<string, unknown> = {};
    for (const key of CONTEXT_KEY_ORDER) {
        if (context[key] !== undefined) {
            ordered[key] = context[key];
        }
    }
    return ordered;
}

export function sameContext(a: WorkspaceContext | null, b: WorkspaceContext | null): boolean {
    return JSON.stringify(canonicalContext(a)) === JSON.stringify(canonicalContext(b));
}

function revisionFor(snapshot: WorkspaceSnapshot, next: WorkspaceContext | null): number {
    return sameContext(snapshot.context, next) ? snapshot.contextRevision : snapshot.contextRevision + 1;
}

function normalizeString(value: unknown): string | undefined {
    if (typeof value !== 'string') {
        return undefined;
    }
    return value.length > MAX_STRING_LENGTH ? value.slice(0, MAX_STRING_LENGTH) : value;
}

function normalizeInteger(value: unknown): number | undefined {
    return typeof value === 'number' && Number.isInteger(value) ? value : undefined;
}

const MAX_COMPARISON_IDS = 4;

function normalizeNonNegativeInteger(value: unknown): number | undefined {
    const n = normalizeInteger(value);
    return n !== undefined && n >= 0 ? n : undefined;
}

function normalizePositiveInteger(value: unknown): number | undefined {
    const n = normalizeInteger(value);
    return n !== undefined && n > 0 ? n : undefined;
}

// Normalizes an untyped CustomEvent.detail (or a typed caller's partial
// context) into a well-formed partial context: a missing or non-object
// detail yields {}, unknown keys are dropped, non-integer
// organizationId/page are dropped, and strings are length-capped.
export function normalizeWorkspaceContext(detail: unknown): Partial<WorkspaceContext> {
    if (typeof detail !== 'object' || detail === null) {
        return {};
    }
    const raw = detail as Record<string, unknown>;
    const context: Partial<WorkspaceContext> = {};
    if (typeof raw.surface === 'string'
        && (VALID_SURFACES as readonly string[]).includes(raw.surface)) {
        context.surface = raw.surface as WorkspaceContext['surface'];
    }
    const organizationId = normalizeInteger(raw.organizationId);
    if (organizationId !== undefined) {
        context.organizationId = organizationId;
    }
    const organizationName = normalizeString(raw.organizationName);
    if (organizationName !== undefined) {
        context.organizationName = organizationName;
    }
    const searchTerm = normalizeString(raw.searchTerm);
    if (searchTerm !== undefined) {
        context.searchTerm = searchTerm;
    }
    const page = normalizeInteger(raw.page);
    if (page !== undefined) {
        context.page = page;
    }
    const jobId = normalizePositiveInteger(raw.jobId);
    if (jobId !== undefined) {
        context.jobId = jobId;
    }
    const algorithmId = normalizePositiveInteger(raw.algorithmId);
    if (algorithmId !== undefined) {
        context.algorithmId = algorithmId;
    }
    if (typeof raw.filters === 'object' && raw.filters !== null) {
        // A present filters object replaces the previous one wholesale; an
        // empty one means "no filters" and is kept so a removed chip clears.
        const rawFilters = raw.filters as Record<string, unknown>;
        const filters: WorkspaceFilters = {};
        if (typeof rawFilters.rtoPolicy === 'string'
            && ['R', 'H', 'O'].includes(rawFilters.rtoPolicy)) {
            filters.rtoPolicy = rawFilters.rtoPolicy as RtoPolicyCode;
        }
        if (rawFilters.acceleratedVesting === true) {
            filters.acceleratedVesting = true;
        }
        context.filters = filters;
    }
    const resultGeneration = normalizeNonNegativeInteger(raw.resultGeneration);
    if (resultGeneration !== undefined) {
        context.resultGeneration = resultGeneration;
    }
    const preferenceRevision = normalizeNonNegativeInteger(raw.preferenceRevision);
    if (preferenceRevision !== undefined) {
        context.preferenceRevision = preferenceRevision;
    }
    if (Array.isArray(raw.comparisonIds)) {
        const ids = Array.from(new Set(
            raw.comparisonIds
                .map(normalizePositiveInteger)
                .filter((id): id is number => id !== undefined),
        ));
        if (ids.length > 0 && ids.length <= MAX_COMPARISON_IDS
            && ids.length === raw.comparisonIds.length) {
            context.comparisonIds = ids;
        }
    }
    return context;
}

export function openAssistant(context?: Partial<WorkspaceContext>): void {
    const s = store();
    const merged = context
        ? {...(s.snapshot.context ?? {}), ...normalizeWorkspaceContext(context)}
        : s.snapshot.context;
    const next = (merged && Object.keys(merged).length > 0
        ? merged
        : null) as WorkspaceContext | null;
    s.snapshot = {
        ...s.snapshot,
        visibility: 'open',
        userOpened: true,
        context: next,
        contextRevision: revisionFor(s.snapshot, next),
    };
    notify();
}

export function closeAssistant(): void {
    const s = store();
    if (s.snapshot.visibility === 'closed') {
        return;
    }
    s.snapshot = {...s.snapshot, visibility: 'closed'};
    notify();
}

export function minimizeAssistant(): void {
    const s = store();
    if (s.snapshot.visibility !== 'open') {
        return;
    }
    s.snapshot = {...s.snapshot, visibility: 'minimized'};
    notify();
}

// Merges a partial context without clobbering keys the caller did not set.
export function setWorkspaceContext(context: Partial<WorkspaceContext>): void {
    const s = store();
    const normalized = normalizeWorkspaceContext(context);
    const merged = {...(s.snapshot.context ?? {}), ...normalized};
    const next = (Object.keys(merged).length > 0 ? merged : null) as WorkspaceContext | null;
    s.snapshot = {
        ...s.snapshot,
        context: next,
        contextRevision: revisionFor(s.snapshot, next),
    };
    notify();
}

// Replaces the whole entity group (id, name, job, comparison) with the given
// one, keeping the rest of the context. Unlike setWorkspaceContext this never
// leaves a name that belongs to a different id behind.
export function setWorkspaceEntity(entity: Partial<WorkspaceContext>): void {
    const s = store();
    const base = {...(s.snapshot.context ?? {})};
    delete base.organizationId;
    delete base.organizationName;
    delete base.jobId;
    delete base.comparisonIds;
    const merged = {...base, ...normalizeWorkspaceContext(entity)};
    const next = (Object.keys(merged).length > 0 ? merged : null) as WorkspaceContext | null;
    s.snapshot = {
        ...s.snapshot,
        context: next,
        contextRevision: revisionFor(s.snapshot, next),
    };
    notify();
}

// URL params that seed an entity into the context (`/chat/?company=12`). A
// cleared context must not be re-seeded by reloading the same URL.
const ENTITY_QUERY_PARAMS = ['company'];

function removeEntityQueryParams(): void {
    try {
        const params = new URLSearchParams(window.location.search);
        const present = ENTITY_QUERY_PARAMS.filter((key) => params.has(key));
        if (present.length === 0) {
            return;
        }
        present.forEach((key) => params.delete(key));
        const query = params.toString();
        window.history.replaceState(
            window.history.state,
            '',
            `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`,
        );
    } catch {
        // history unavailable; the URL keeps its parameter.
    }
}

// Removes the entity keys but keeps the surface; bumps the revision only
// when something was actually removed (issue #479 AC-3).
export function clearWorkspaceContext(): void {
    removeEntityQueryParams();
    const s = store();
    const current = s.snapshot.context;
    if (!current) {
        return;
    }
    const cleared = {...current};
    delete cleared.organizationId;
    delete cleared.organizationName;
    delete cleared.jobId;
    delete cleared.comparisonIds;
    s.snapshot = {
        ...s.snapshot,
        context: cleared,
        contextRevision: revisionFor(s.snapshot, cleared),
    };
    notify();
}

export function setWorkspaceAccount(account: WorkspaceAccount): void {
    const s = store();
    const current = s.snapshot.account;
    if (current.status === account.status && current.key === account.key) {
        return;
    }
    update({account: {status: account.status, key: account.key}});
}

export function setWorkspaceConversation(conversationId: number | null): void {
    const s = store();
    if (s.snapshot.conversationId === conversationId) {
        return;
    }
    update({conversationId});
}

// Replaces visibility + context wholesale (persistence restore, account
// reset). Not a merge: a `null` context clears everything.
export function replaceWorkspaceState(
    visibility: AssistantVisibility,
    context: WorkspaceContext | null,
    conversationId: number | null = null,
): void {
    const s = store();
    s.snapshot = {
        ...s.snapshot,
        visibility,
        userOpened: false,
        context,
        conversationId,
        contextRevision: revisionFor(s.snapshot, context),
    };
    notify();
}

// Marks the lazy chat chunk resolved; called once by the panel after the
// Suspense boundary commits real content.
export function markWorkspaceLoaded(): void {
    const s = store();
    if (s.snapshot.loaded) {
        return;
    }
    s.snapshot = {...s.snapshot, loaded: true};
    notify();
}

// The mode is owned by the matchMedia hook but mirrored into the snapshot so
// non-React bundles can read it.
export function setWorkspaceMode(mode: WorkspaceMode): void {
    const s = store();
    if (s.snapshot.mode === mode) {
        return;
    }
    update({mode});
}

export function setPrioritiesRevision(revision: number): void {
    if (store().snapshot.prioritiesRevision === revision) {
        return;
    }
    update({prioritiesRevision: revision});
}

export function setPrioritiesEditorOpen(host: PrioritiesEditorHost | null, seed: PrioritiesEditorSeed | null = null): void {
    const current = store().snapshot;
    if (current.prioritiesEditorOpenIn === host && current.prioritiesEditorSeed === seed) {
        return;
    }
    update({prioritiesEditorOpenIn: host, prioritiesEditorSeed: host === null ? null : seed});
}

// Account switch / private-state purge: drops every priorities signal.
export function resetPriorities(): void {
    update({prioritiesRevision: null, prioritiesEditorOpenIn: null, prioritiesEditorSeed: null});
}

export function subscribeWorkspace(listener: () => void): () => void {
    const s = store();
    s.listeners.add(listener);
    return () => {
        s.listeners.delete(listener);
    };
}

export function getWorkspaceSnapshot(): WorkspaceSnapshot {
    return store().snapshot;
}

// Attaches the cross-bundle window-event listeners; returns a teardown that
// removes both. `crank:assistant-open` opens the assistant (merging any
// normalized context detail); `crank:workspace-context` only merges context.
export function installWorkspaceBridge(): () => void {
    const handleOpen = (event: Event): void => {
        openAssistant(normalizeWorkspaceContext((event as CustomEvent).detail));
    };
    const handleContext = (event: Event): void => {
        setWorkspaceContext(normalizeWorkspaceContext((event as CustomEvent).detail));
    };
    window.addEventListener(WORKSPACE_OPEN_EVENT, handleOpen);
    window.addEventListener(WORKSPACE_CONTEXT_EVENT, handleContext);
    return () => {
        window.removeEventListener(WORKSPACE_OPEN_EVENT, handleOpen);
        window.removeEventListener(WORKSPACE_CONTEXT_EVENT, handleContext);
    };
}

// Test-only escape hatch: drops the singleton so a fresh module/registry
// state can be observed. Not exported for production callers.
export function resetWorkspaceForTests(): void {
    delete window.__crankWorkspace__;
}

// Human label for the entity part of a context (strip text and the chat's
// "Asked while viewing …" note). Empty when the context names no entity.
export function describeWorkspaceContext(context: WorkspaceContext | null): string {
    if (!context) {
        return '';
    }
    if (context.organizationName) {
        return `About ${context.organizationName}`;
    }
    if (context.organizationId !== undefined) {
        return 'About this company';
    }
    if (context.jobId !== undefined) {
        return `About job #${context.jobId}`;
    }
    if (context.comparisonIds && context.comparisonIds.length > 0) {
        return `Comparing ${context.comparisonIds.length} companies`;
    }
    return '';
}

// Registers the mounted surface that can apply assistant-proposed filters
// (the rankings list). Returns the unregister function; the newest
// registration wins and releasing it restores the previous one.
export function registerFilterTarget(handler: FilterTargetHandler): () => void {
    const s = store();
    s.filterTargets = [...(s.filterTargets ?? []), handler];
    return () => {
        const current = store();
        current.filterTargets = (current.filterTargets ?? []).filter((h) => h !== handler);
    };
}

// Applies filters on the mounted target; false when none is mounted or it
// declined, so the caller can fall back to navigation.
export function applyWorkspaceFilters(filters: WorkspaceFilters): boolean {
    const targets = store().filterTargets ?? [];
    const target = targets[targets.length - 1];
    return target ? target(filters) === true : false;
}

// Registers the mounted surface that can open a company's details by id
// (the rankings list); same newest-wins rules as the filter target.
export function registerCompanyTarget(handler: CompanyTargetHandler): () => void {
    const s = store();
    s.companyTargets = [...(s.companyTargets ?? []), handler];
    return () => {
        const current = store();
        current.companyTargets = (current.companyTargets ?? []).filter((h) => h !== handler);
    };
}

export type CompanyOpenOutcome = 'opened' | 'not-found' | 'no-target';

// 'not-found' means a list is mounted but the company is not in it, so the
// caller must not claim it opened; 'no-target' means navigate instead.
export function openWorkspaceCompany(organizationId: number): CompanyOpenOutcome {
    const targets = store().companyTargets ?? [];
    const target = targets[targets.length - 1];
    if (!target) {
        return 'no-target';
    }
    return target(organizationId) === true ? 'opened' : 'not-found';
}

// Maps the live context to the snake_case wire shape. Names and search text
// are display-only and are never sent.
export function buildWireContext(snapshot: WorkspaceSnapshot): WireContext | undefined {
    const context = snapshot.context;
    if (!context) {
        return undefined;
    }
    const wire: WireContext = {revision: snapshot.contextRevision};
    if (context.surface !== undefined) {
        wire.surface = context.surface;
    }
    if (context.organizationId !== undefined && context.organizationId > 0) {
        wire.organization_id = context.organizationId;
    }
    if (context.jobId !== undefined) {
        wire.job_id = context.jobId;
    }
    if (context.comparisonIds && context.comparisonIds.length > 0) {
        wire.comparison_ids = [...context.comparisonIds];
    }
    if (context.algorithmId !== undefined) {
        wire.algorithm_id = context.algorithmId;
    }
    if (context.page !== undefined && context.page > 0) {
        wire.page = Math.min(context.page, 10000);
    }
    if (context.filters) {
        const filters: NonNullable<WireContext['filters']> = {};
        if (context.filters.rtoPolicy) {
            filters.rto_policy = context.filters.rtoPolicy;
        }
        if (context.filters.acceleratedVesting) {
            filters.accelerated_vesting = true;
        }
        if (Object.keys(filters).length > 0) {
            wire.filters = filters;
        }
    }
    if (context.preferenceRevision !== undefined) {
        wire.preference_revision = context.preferenceRevision;
    }
    if (context.resultGeneration !== undefined) {
        wire.result_generation = context.resultGeneration;
    }
    return wire;
}
