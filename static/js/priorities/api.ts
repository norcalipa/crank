// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Client for the priorities read / propose / apply / undo / reset endpoints
// (issue #480). Field metadata is server-owned: this module never restates
// the preference schema.

import {csrfFetch} from './csrf';
import type {PreferenceChange} from './format';

export type FieldType = 'int' | 'float' | 'str' | 'bool' | 'str_list' | 'float_map';

export interface EditorField {
    path: string;
    label: string;
    type: FieldType;
    value: unknown;
    set: boolean;
    supported: boolean;
    hard: boolean;
    hard_locked: boolean;
    editable: boolean;
    choices?: string[];
}

export interface PriorityChip {
    path: string;
    label: string;
    display: string;
    hard: boolean;
    supported: boolean;
    items?: string[];
}

export interface PrioritiesSnapshot {
    exists: boolean;
    revision: number;
    schema_version: number;
    preferences: Record<string, unknown>;
    fields: EditorField[];
    chips: PriorityChip[];
    unsupported_criteria: string[];
}

export interface PreferencePatch {
    set?: Record<string, unknown>;
    remove?: Record<string, unknown>;
}

export type ProposalScope = 'account' | 'search';

export interface ProposalToken {
    patch: PreferencePatch;
    scope: ProposalScope;
    base_revision: number;
    origin?: string;
}

export interface Proposal {
    id: string;
    scope: ProposalScope;
    changes: PreferenceChange[];
    change_count: number;
    base_revision: number;
    unsupported_criteria: string[];
    token: ProposalToken;
}

export interface UndoToken {
    expected_revision: number;
    document: Record<string, unknown>;
}

export interface AppliedResult {
    revision: number | null;
    changes: PreferenceChange[];
    undo: UndoToken | null;
    scope: ProposalScope;
    matchCount?: number;
    matchCapped?: boolean;
}

// The server caps each match list at this many entries, so a full list means "at least".
export const MATCH_CAP = 25;

export class ApiFailure extends Error {
    status: number;
    type: string | null;
    fieldErrors: Record<string, string[]>;
    currentRevision: number | null;

    constructor(status: number, type: string | null, message: string,
                fieldErrors: Record<string, string[]> = {}, currentRevision: number | null = null) {
        super(message);
        this.status = status;
        this.type = type;
        this.fieldErrors = fieldErrors;
        this.currentRevision = currentRevision;
    }

    get stale(): boolean {
        return this.status === 409 || this.type === 'preference_stale' || this.type === 'forbidden';
    }

    get authRequired(): boolean {
        return this.type === 'auth_required';
    }
}

export const GENERIC_ERROR_MESSAGE = 'Something went wrong. Please try again.';
export const SESSION_EXPIRED_MESSAGE = 'Your session has expired. Sign in to continue.';


async function request<T>(url: string, init?: RequestInit): Promise<T> {
    let res: Response;
    try {
        res = await csrfFetch(url, init);
    } catch {
        throw new ApiFailure(0, 'network', 'Could not reach the server. Check your connection and try again.');
    }
    // An expired session redirects to the login page, which fetch follows into HTML.
    if (res.redirected || res.status === 401) {
        throw new ApiFailure(401, 'auth_required', SESSION_EXPIRED_MESSAGE);
    }
    if (res.ok) {
        try {
            return (await res.json()) as T;
        } catch {
            throw new ApiFailure(401, 'auth_required', SESSION_EXPIRED_MESSAGE);
        }
    }
    let type: string | null = null;
    let message = GENERIC_ERROR_MESSAGE;
    let fieldErrors: Record<string, string[]> = {};
    let currentRevision: number | null = null;
    try {
        const body = await res.json();
        type = body?.error?.type ?? null;
        message = body?.error?.message || message;
        fieldErrors = body?.error?.field_errors ?? {};
        currentRevision = typeof body?.error?.current_revision === 'number' ? body.error.current_revision : null;
    } catch {
        // non-JSON body: keep the generic message
    }
    throw new ApiFailure(res.status, type, message, fieldErrors, currentRevision);
}

const post = (body: unknown, signal?: AbortSignal): RequestInit => ({
    method: 'POST',
    body: JSON.stringify(body),
    signal,
});

export function readPriorities(signal?: AbortSignal): Promise<PrioritiesSnapshot> {
    return request<PrioritiesSnapshot>('/api/agent/preferences/', {signal});
}

export function proposePriorities(patch: PreferencePatch, scope: ProposalScope, signal?: AbortSignal): Promise<Proposal> {
    return request<Proposal>('/api/agent/preferences/propose/', post({patch, scope}, signal));
}

interface ApplyResponse {
    scope?: ProposalScope;
    revision?: number | null;
    changes?: PreferenceChange[];
    undo?: UndoToken | null;
    matches?: {job_matches?: unknown[]; organization_matches?: unknown[]};
}

export async function applyProposal(token: ProposalToken, signal?: AbortSignal): Promise<AppliedResult> {
    const data = await request<ApplyResponse>(
        '/api/agent/preferences/apply/', post({proposal: token, decision: 'apply'}, signal),
    );
    if (token.scope === 'search') {
        const count = data.matches?.job_matches?.length || 0;
        return {
            revision: null, changes: [], undo: null, scope: 'search',
            matchCount: count, matchCapped: count >= MATCH_CAP,
        };
    }
    return {revision: data.revision ?? null, changes: data.changes || [], undo: data.undo || null, scope: 'account'};
}

export async function undoApplied(undo: UndoToken, signal?: AbortSignal): Promise<number | null> {
    const data = await request<ApplyResponse>('/api/agent/preferences/undo/', post({undo}, signal));
    return data.revision ?? null;
}

export async function resetPriorities(expectedRevision: number, signal?: AbortSignal): Promise<AppliedResult> {
    const data = await request<ApplyResponse>(
        '/api/agent/preferences/reset/', post({expected_revision: expectedRevision}, signal),
    );
    return {revision: data.revision ?? null, changes: data.changes || [], undo: data.undo || null, scope: 'account'};
}
