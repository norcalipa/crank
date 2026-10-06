// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import type {EvidenceSummaryData} from '../labels';

export interface JobResult {
    id: number;
    title: string;
    organization_name: string;
    location: string;
    remote: boolean;
    compensation: {
        min: number | null;
        max: number | null;
        currency: string;
        interval: string;
    } | null;
    canonical_url: string;
    observed_at: string | null;
    updated_at: string | null;
}

export interface OrganizationResult {
    id: number;
    name: string;
    url: string;
    funding_round: string;
    rto_policy: string;
    // Fact summary at reply time (issue #473). Missing or null on replies
    // stored before it existed.
    evidence?: EvidenceSummaryData | null;
}

export interface StructuredResults {
    jobs: JobResult[];
    organizations: OrganizationResult[];
}

export interface ChatMessage {
    id: number;
    role: 'user' | 'assistant';
    content: string;
    preferences_changed: boolean;
    created: string | null;
    results: StructuredResults | null;
    // Turn delivery state (issue #458): present on user messages only.
    idempotency_key?: string;
    delivery_state?: 'pending' | 'completed' | 'failed';
    // Whether the per-turn retry cap still allows a retry (server-driven).
    retry_available?: boolean;
}

/** Canonical availability payload from /api/job-matches/status/ (issue #476). */
export interface AvailabilityPayload {
    state: string;
    title: string;
    message: string;
    refreshing?: boolean;
}

export interface Conversation {
    id: number;
    active: boolean;
    created: string | null;
    modified: string | null;
    messages: ChatMessage[];
    preferences_changed: boolean;
}

/** Field-level preference diff entry (issue #466): one changed path with
 * its previous and current values, as returned by the turn endpoint. */
export interface PreferenceChange {
    path: string;
    old: unknown;
    new: unknown;
}

/** Opaque undo token (issue #466 review): the full pre-apply document plus
 * the post-apply revision it may be restored against. Client-held only; the
 * server re-validates it owner-scoped under the revision precondition. */
export interface PreferenceUndoToken {
    expected_revision: number;
    document: Record<string, unknown>;
}

/** Client-held proposal token (issue #466 review): the model-proposed patch,
 * its scope, and the base revision the apply must be preconditioned on. */
export interface PreferenceProposalToken {
    patch: Record<string, unknown>;
    scope: 'account' | 'search';
    base_revision: number;
}

/** Read-only preference proposal (issue #466 review): the chat turn never
 * persists a model-proposed patch; the user applies or dismisses it. */
export interface PreferenceProposal {
    id: string;
    scope: 'account' | 'search';
    changes: PreferenceChange[];
    change_count: number;
    base_revision: number;
    unsupported_criteria: string[];
    // The saved currency the proposal's money values are shown in.
    currency?: string | null;
    token: PreferenceProposalToken;
}

export interface SubmitResponse {
    message: ChatMessage;
    preferences_changed: boolean;
    // Additive (issue #466 review): present only when the turn produced a
    // read-only preference proposal through an orchestrator-backed provider.
    preference_proposal?: PreferenceProposal | null;
}

/** Undo lifecycle for the preference-change notice (issue #466). */
export type PreferenceUndoState = 'idle' | 'pending' | 'done' | 'error';

export interface ApiError {
    error?: {type?: string; message?: string; request_id?: string};
}

export type AssistantState =
    | 'signed_out'
    | 'replies_disabled'
    | 'temporarily_unavailable'
    | 'inventory_unavailable'
    | 'refreshing'
    | 'ready';

export interface AssistantStatus {
    state: AssistantState;
    actions: string[];
    checked_at: string;
}
