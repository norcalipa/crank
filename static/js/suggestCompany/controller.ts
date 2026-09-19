// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Cross-bundle "Suggest a company" controller (issue #471). The app renders
// three independent React roots from three separate webpack bundles (main,
// jobsearch, jobmatch), so a module-level store here is duplicated per
// bundle and cannot by itself be the cross-root channel. The existing
// `crank:suggest-company` window event is that channel: every trigger either
// calls `openSuggestCompany` directly (same-bundle callers, e.g.
// OrganizationList) or dispatches the window event (cross-bundle callers,
// e.g. JobMatchPanel), and `installSuggestCompanyBridge` — mounted once by
// the single host in the `main` bundle — is the one listener that turns the
// event into controller state.

export type SuggestCompanySource =
    'rankings' | 'rankings_empty' | 'company_details' | 'job_results' | 'assistant';

export interface SuggestCompanyContext {
    source: SuggestCompanySource;
    companyName?: string;
    searchTerm?: string;
    page?: number;
    organizationId?: number;
}

export interface SuggestCompanyState {
    open: boolean;
    context: SuggestCompanyContext | null;
}

export const SUGGEST_COMPANY_EVENT = 'crank:suggest-company';

// Caps a hostile or malformed `CustomEvent.detail` from inflating the
// prefilled form; the API itself never sees these values (they are not part
// of the submit payload).
const MAX_STRING_LENGTH = 200;

const VALID_SOURCES: readonly SuggestCompanySource[] = [
    'rankings', 'rankings_empty', 'company_details', 'job_results', 'assistant',
];

let state: SuggestCompanyState = {open: false, context: null};
const listeners = new Set<() => void>();

function notify(): void {
    for (const listener of listeners) {
        listener();
    }
}

function normalizeSource(value: unknown): SuggestCompanySource {
    return typeof value === 'string' && (VALID_SOURCES as readonly string[]).includes(value)
        ? (value as SuggestCompanySource)
        : 'assistant';
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

// Normalizes an untyped `CustomEvent.detail` (or a typed caller's partial
// context) into a well-formed context: a missing or non-object detail yields
// `{source: 'assistant'}` (AC-4), unknown keys are dropped, non-integer
// `page`/`organizationId` are dropped, and strings are length-capped.
export function normalizeSuggestCompanyContext(detail: unknown): SuggestCompanyContext {
    if (typeof detail !== 'object' || detail === null) {
        return {source: 'assistant'};
    }
    const raw = detail as Record<string, unknown>;
    const context: SuggestCompanyContext = {source: normalizeSource(raw.source)};

    const companyName = normalizeString(raw.companyName);
    if (companyName !== undefined) {
        context.companyName = companyName;
    }
    const searchTerm = normalizeString(raw.searchTerm);
    if (searchTerm !== undefined) {
        context.searchTerm = searchTerm;
    }
    const page = normalizeInteger(raw.page);
    if (page !== undefined) {
        context.page = page;
    }
    const organizationId = normalizeInteger(raw.organizationId);
    if (organizationId !== undefined) {
        context.organizationId = organizationId;
    }
    return context;
}

export function openSuggestCompany(context?: Partial<SuggestCompanyContext>): void {
    state = {open: true, context: normalizeSuggestCompanyContext(context ?? {source: 'assistant'})};
    notify();
}

export function closeSuggestCompany(): void {
    if (!state.open) {
        return;
    }
    state = {...state, open: false};
    notify();
}

export function subscribeSuggestCompany(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

export function getSuggestCompanySnapshot(): SuggestCompanyState {
    return state;
}

function handleSuggestCompanyWindowEvent(event: Event): void {
    const detail = (event as CustomEvent).detail;
    openSuggestCompany(normalizeSuggestCompanyContext(detail));
}

// Attaches the cross-bundle window-event listener; returns a teardown that
// removes it. Safe to call once per host mount (the host is a module-level
// singleton per page, see mount.tsx).
export function installSuggestCompanyBridge(): () => void {
    window.addEventListener(SUGGEST_COMPANY_EVENT, handleSuggestCompanyWindowEvent);
    return () => {
        window.removeEventListener(SUGGEST_COMPANY_EVENT, handleSuggestCompanyWindowEvent);
    };
}
