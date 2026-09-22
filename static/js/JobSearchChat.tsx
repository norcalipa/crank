// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createRoot} from 'react-dom/client';

import {purgePrivateClientState} from './authIntent';

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

interface Conversation {
    id: number;
    active: boolean;
    created: string | null;
    modified: string | null;
    messages: ChatMessage[];
    preferences_changed: boolean;
}

interface SubmitResponse {
    message: ChatMessage;
    preferences_changed: boolean;
}

interface ApiError {
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

function getCookie(name: string): string {
    const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match[2]) : '';
}

// Durable in-flight turn markers (issue #458). Written before a submission
// resolves so a request that never received a response survives reload and
// navigation, and reconciled against the server on the next load: the server
// is the single source of truth, the markers only cover the window it cannot
// see. Each turn gets its own storage key (conversation id + turn key) so
// concurrent turns or tabs never overwrite each other's recovery state and
// resolving one turn never clears another turn's marker.
interface InFlightTurn {conversationId: number; content: string; key: string; ts: number}

const INFLIGHT_PREFIX = 'crank:jobsearch:inflight:';
// Markers older than a day cannot still be in flight; prune them so storage
// cannot grow without bound.
const INFLIGHT_MAX_AGE_MS = 24 * 60 * 60 * 1000;

function inflightStorageKey(conversationId: number, key: string): string {
    return `${INFLIGHT_PREFIX}${conversationId}:${key}`;
}

function draftKey(conversationId: number): string {
    return `crank:jobsearch:draft:${conversationId}`;
}

// Draft typed before the first conversation exists (issue #465): a
// signed-out visitor has no conversation id to key a draft against, so this
// single slot holds their in-progress text until the first conversation
// created after sign-in adopts it. Never sent anywhere — draft text stays
// out of every URL, including the sign-in `next`.
const PENDING_DRAFT_KEY = 'crank:jobsearch:draft:pending';

function readPendingDraft(): string {
    try {
        return window.localStorage.getItem(PENDING_DRAFT_KEY) || '';
    } catch {
        return '';
    }
}

function writePendingDraft(text: string): void {
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

function clearPendingDraft(): void {
    try {
        window.localStorage.removeItem(PENDING_DRAFT_KEY);
    } catch {
        // Storage unavailable; nothing durable to clear.
    }
}

// Name of the account that last wrote private artefacts into this browser's
// storage. Written by both the synchronous server-rendered reconciliation
// below and the asynchronous whoami hydration, which agree on the value
// (Django's username) so either can detect a switch the other missed.
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
    let lastAccount: string | null = null;
    try {
        lastAccount = window.localStorage.getItem(LAST_ACCOUNT_KEY);
    } catch {
        // Storage unavailable: nothing durable was stored for any account,
        // so there is nothing to leak and nothing to record.
        return false;
    }
    const switched = !!lastAccount && lastAccount !== accountKey;
    if (switched) {
        purgePrivateClientState();
    }
    try {
        window.localStorage.setItem(LAST_ACCOUNT_KEY, accountKey);
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
function draftTsKey(conversationId: number): string {
    return `crank:jobsearch:draftts:${conversationId}`;
}

function readComposerDraftTs(conversationId: number): number {
    try {
        return Number(window.localStorage.getItem(draftTsKey(conversationId))) || 0;
    } catch {
        return 0;
    }
}

function writeInflightTurn(turn: InFlightTurn): void {
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

function clearInflightTurn(conversationId: number, key: string): void {
    try {
        window.localStorage.removeItem(inflightStorageKey(conversationId, key));
    } catch {
        // Storage unavailable; nothing durable to clear.
    }
}

// Clear every marker belonging to one conversation (reset/delete/gone).
// Markers are keyed per conversation, so this can never destroy another
// conversation's recovery state.
function clearInflightTurns(conversationId: number): void {
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
function readInflightTurns(conversationId: number): InFlightTurn[] {
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

// Typed error envelopes that mean the request never persisted a turn: the
// server has no trace of it, so the UI must never claim "your message is
// saved" for these — the text stays an unsent draft instead (issue #458).
const PRE_PERSISTENCE_ERROR_TYPES = new Set([
    'rate_limited',
    'invalid_message',
    'malformed_json',
    'payload_too_large',
    'invalid_request',
    'not_found',
]);
// Typed envelopes the server returns only AFTER the user turn is persisted;
// for these the failed-turn UI ("your message is saved; retry") is honest.
const POST_PERSISTENCE_ERROR_TYPES = new Set([
    'assistant_unavailable',
    'provider_timeout',
    'cost_limit',
    'invalid_output',
    'service_error',
    'unexpected_error',
]);

function readComposerDraft(conversationId: number): string {
    try {
        return window.localStorage.getItem(draftKey(conversationId)) || '';
    } catch {
        return '';
    }
}

function writeComposerDraft(conversationId: number | null, text: string): void {
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

function newId(): string {
    const cryptoObj = typeof crypto !== 'undefined' ? crypto : null;
    if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
        return cryptoObj.randomUUID();
    }
    // Fallback for older runtimes/tests without crypto.randomUUID.
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        const v = c === 'x' ? r : (r & 0x3) | 0x8;
        return v.toString(16);
    });
}

async function csrfFetch(url: string, init: RequestInit = {}): Promise<Response> {
    const method = (init.method || 'GET').toUpperCase();
    const headers: Record<string, string> = {...(init.headers as Record<string, string> || {})};
    if (method !== 'GET' && method !== 'HEAD') {
        const token = getCookie('csrftoken');
        if (token) {
            headers['X-CSRFToken'] = token;
        }
        headers['Content-Type'] = 'application/json';
    }
    return fetch(url, {...init, headers});
}

function formatCompensation(comp: JobResult['compensation']): string {
    if (!comp) return '';
    const parts: string[] = [];
    const fmt = (v: number | null) => v !== null ? v.toLocaleString() : '';
    if (comp.min !== null && comp.max !== null) {
        parts.push(`${fmt(comp.min)}-${fmt(comp.max)}`);
    } else if (comp.min !== null) {
        parts.push(`${fmt(comp.min)}+`);
    } else if (comp.max !== null) {
        parts.push(`up to ${fmt(comp.max)}`);
    }
    if (comp.currency) parts.push(comp.currency);
    if (comp.interval) parts.push(comp.interval);
    return parts.join(' ');
}

function fundingRoundLabel(code: string): string {
    const map: Record<string, string> = {
        S: 'Seed', A: 'Series A', B: 'Series B', C: 'Series C',
        D: 'Series D', E: 'Series E', F: 'Series F',
        X: 'Late Stage', O: 'IPO', P: 'Pre-IPO',
    };
    return map[code] || code || '';
}

function rtoPolicyLabel(code: string): string {
    const map: Record<string, string> = {
        R: 'Remote', H: 'Hybrid', O: 'On-site',
    };
    return map[code] || code || '';
}

// States in which submitting a message would be futile: the assistant cannot
// answer at all (administratively disabled) or has nothing to search.
// Gating is advisory-only: the POST path remains authoritative and a failed
// status fetch never gates the composer (issue #457).
const GATED_STATES: AssistantState[] = ['replies_disabled', 'inventory_unavailable'];

function isGatedState(state: AssistantState | undefined): boolean {
    return state !== undefined && GATED_STATES.includes(state);
}

// Re-checking is only meaningful for transient conditions; a fixed policy
// state (replies_disabled) is not expected to flip by re-fetching.
const RETRYABLE_STATES: AssistantState[] = ['temporarily_unavailable', 'refreshing'];

function AssistantStatusNotice({status, onRetry, checking}: {
    status: AssistantStatus;
    onRetry: () => void;
    checking: boolean;
}) {
    // No notice for the healthy baseline. `signed_out` is now reachable on
    // /chat/ (issue #465 made the page public) but adds nothing actionable
    // here: the dedicated signed-out introduction below already explains the
    // state and offers the sign-in CTA.
    if (status.state === 'ready' || status.state === 'signed_out') return null;

    // Short scannable state label plus one supporting sentence: the state and
    // the next action should be readable at a glance, especially on mobile.
    const copy: Record<string, {title: string; body: string}> = {
        replies_disabled: {
            title: 'Assistant unavailable',
            body: 'Replies are paused right now. Saved preferences remain ' +
                'available — update them here once replies resume.',
        },
        inventory_unavailable: {
            title: 'Assistant unavailable',
            body: 'No active job listings to search right now. Saved preferences ' +
                'are still available — update them here once listings return.',
        },
        temporarily_unavailable: {
            title: 'Assistant temporarily unavailable',
            body: 'Check again in a moment.',
        },
        refreshing: {
            title: 'Assistant refreshing',
            body: 'Job listings are being refreshed; the assistant will be back shortly.',
        },
    };
    const text = copy[status.state];
    if (!text) return null;

    const canRetry = RETRYABLE_STATES.includes(status.state);
    const browseRankings = status.actions.includes('browse_rankings');

    return (
        <div
            className="alert alert-danger assistant-status-notice py-2 px-3"
            role="alert"
            data-testid="assistant-status-notice"
            data-status-state={status.state}
            aria-label="Assistant availability"
        >
            <div className="d-flex align-items-start gap-2">
                <i className="fa-solid fa-circle-exclamation mt-1" aria-hidden="true"></i>
                <div>
                    <strong className="d-block">{text.title}</strong>
                    <span className="d-block small">{text.body}</span>
                </div>
            </div>
            {(browseRankings || canRetry) && (
                <div className="assistant-status-notice-actions d-flex flex-wrap gap-2 mt-1">
                    {browseRankings && (
                        <a href="/" className="alert-link assistant-status-notice-action">
                            Browse company rankings
                        </a>
                    )}
                    {canRetry && (
                        <button
                            type="button"
                            // btn-danger is the semantic recovery action: solid,
                            // high-emphasis, and clearly the way out of the
                            // unavailable state (visual review #472 round 1).
                            className="btn btn-danger assistant-status-notice-action"
                            onClick={onRetry}
                            disabled={checking}
                            data-testid="assistant-status-retry"
                        >
                            Check again
                        </button>
                    )}
                </div>
            )}
        </div>
    );
}

function JobCard({job}: {job: JobResult}) {
    const comp = formatCompensation(job.compensation);
    const freshness = job.observed_at
        ? new Date(job.observed_at).toLocaleDateString(undefined, {month: 'short', day: 'numeric'})
        : '';
    return (
        <article
            className="job-card border rounded p-2 mb-2"
            tabIndex={0}
            role="article"
            aria-label={`Job: ${job.title} at ${job.organization_name}`}
            style={{maxWidth: '100%', overflow: 'hidden'}}
        >
            <div className="d-flex justify-content-between align-items-start flex-wrap">
                <strong className="text-break" style={{maxWidth: '100%'}}>{job.title}</strong>
                {freshness && <small className="text-muted text-nowrap ms-2">{freshness}</small>}
            </div>
            <div className="text-muted small">
                {job.organization_name}{job.location ? ` · ${job.location}` : ''}
                {job.remote ? ' · Remote' : ''}
            </div>
            {comp && <div className="small">{comp}</div>}
            {job.canonical_url && (
                <a
                    href={job.canonical_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="small d-inline-block mt-1"
                    aria-label={`Open listing for ${job.title} (opens in a new tab)`}
                >
                    View listing ↗
                </a>
            )}
        </article>
    );
}

function OrgCard({org}: {org: OrganizationResult}) {
    const funding = fundingRoundLabel(org.funding_round);
    const rto = rtoPolicyLabel(org.rto_policy);
    return (
        <article
            className="org-card border rounded p-2 mb-2"
            tabIndex={0}
            role="article"
            aria-label={`Organization: ${org.name}`}
            style={{maxWidth: '100%', overflow: 'hidden'}}
        >
            <strong className="text-break" style={{maxWidth: '100%'}}>{org.name}</strong>
            <div className="text-muted small">
                {funding && <span>{funding}</span>}
                {funding && rto && ' · '}
                {rto && <span>{rto}</span>}
            </div>
            {org.url && (
                <a
                    href={org.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="small d-inline-block mt-1"
                    aria-label={`View details for ${org.name} (opens in a new tab)`}
                >
                    Details ↗
                </a>
            )}
        </article>
    );
}

function ResultCards({results}: {results: StructuredResults}) {
    const hasJobs = results.jobs && results.jobs.length > 0;
    const hasOrgs = results.organizations && results.organizations.length > 0;
    if (!hasJobs && !hasOrgs) return null;
    return (
        <div className="mt-2" data-testid="result-cards">
            {hasJobs && (
                <div>
                    <h3 className="h6 small text-muted mb-1">Job Listings</h3>
                    {results.jobs.map((job) => (
                        <JobCard key={`job-${job.id}`} job={job} />
                    ))}
                </div>
            )}
            {hasOrgs && (
                <div>
                    <h3 className="h6 small text-muted mb-1">Organizations</h3>
                    {results.organizations.map((org) => (
                        <OrgCard key={`org-${org.id}`} org={org} />
                    ))}
                </div>
            )}
        </div>
    );
}

function hasResults(results: StructuredResults | null): boolean {
    if (!results) return false;
    return (results.jobs?.length || 0) + (results.organizations?.length || 0) > 0;
}

/** Compact availability notice so an empty reply is never silently unexplained. */
function AvailabilityNotice({availability}: {availability: AvailabilityPayload}) {
    return (
        <div className="availability-notice border rounded p-2 mt-2 small"
             role="status" aria-live="polite" data-testid="availability-notice">
            <i className="fa-solid fa-circle-info me-1" aria-hidden="true"></i>
            <strong>{availability.title}</strong> — {availability.message}
        </div>
    );
}

export interface JobSearchChatProps {
    // Hydrated server-side from `crank.auth.visitor_state` (issue #465):
    // false for both anonymous states below. Gates the conversation
    // resume/create fetch entirely — an anonymous GET must never create a
    // JobSearchConversation row.
    isAuthenticated?: boolean;
    // 'authenticated' | 'session_expired' | 'anonymous_first_visit'. Not
    // read by this component (the server already resolves it into
    // signedOutMessage below); accepted so callers can pass the same
    // dataset-derived props object used elsewhere without filtering it.
    visitorState?: string;
    signInUrl?: string;
    // Pre-selected by the server for the current visitorState (issue #465):
    // the first-visit intro or the expiry explanation, never both at once —
    // an anonymous-first-visit response must never leak the expiry copy (or
    // vice versa) into the DOM via an unused prop.
    signedOutMessage?: string;
    // Trusted server-rendered account discriminator for this response
    // (issue #465 AC-9/10). `/chat/` is @never_cache and
    // server-authenticated, so this names the account the page was rendered
    // for — available synchronously, unlike the whoami hydration. Empty for
    // a signed-out visitor.
    accountKey?: string;
    // Shared workspace contract: opening the assistant must not create a
    // conversation; the first send creates it.
    createOnMount?: boolean;
    // Workspace placement (issue #472): 'sheet' means the full-screen mobile
    // surface where the Job Matches panel is behind a "Back to results"
    // control, so directional microcopy must not claim the panel is beside
    // the chat. Undefined outside the workspace (legacy mount).
    workspaceMode?: 'docked' | 'drawer' | 'sheet';
}

const JobSearchChat: React.FC<JobSearchChatProps> = (props) => {
    const {
        isAuthenticated = true,
        signInUrl = '/accounts/login/',
        signedOutMessage = '',
        accountKey = '',
    } = props;    // The shared workspace opts out explicitly. Direct authenticated mounts
    // retain the legacy create-on-mount contract for compatibility; the
    // prop-less workspace/test mount uses the issue #472 default of false.
    const createOnMount = props.createOnMount ?? props.isAuthenticated !== undefined;
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
        reconcileAccountKey(accountKey);
    }

    const [effectiveAuthenticated, setEffectiveAuthenticated] = React.useState(isAuthenticated);

    const [conversationId, setConversationId] = React.useState<number | null>(null);
    const [messages, setMessages] = React.useState<ChatMessage[]>([]);
    const [input, setInput] = React.useState('');
    const [pending, setPending] = React.useState(false);
    const [loading, setLoading] = React.useState(true);
    const [initError, setInitError] = React.useState<string | null>(null);
    const [error, setError] = React.useState<string | null>(null);
    const [errorType, setErrorType] = React.useState<string | null>(null);
    const [retrying, setRetrying] = React.useState(false);
    // Idempotency key of the turn currently being retried (issue #458 round 2):
    // flips that message's failure treatment to the in-progress amber state.
    const [retryKey, setRetryKey] = React.useState<string | null>(null);
    // Data note is collapsed by default so the conversation owns the viewport;
    // the details stay available to sighted users via the toggle and to screen
    // readers via the visually-hidden fallback.
    const [dataNoteOpen, setDataNoteOpen] = React.useState(false);
    const [preferencesChanged, setPreferencesChanged] = React.useState(false);
    const [prefDismissed, setPrefDismissed] = React.useState(false);
    // Advisory assistant availability (issue #457). null means the status is
    // unknown — e.g. the fetch failed — and the chat stays fully usable.
    const [assistantStatus, setAssistantStatus] = React.useState<AssistantStatus | null>(null);
    const [statusChecking, setStatusChecking] = React.useState(false);
    // Availability state (issue #476): fetched lazily, best-effort, so an
    // assistant reply without results can carry a compact availability notice
    // from the same canonical contract as the job-match panel.
    const [availability, setAvailability] = React.useState<AvailabilityPayload | null>(null);
    const availabilityRequested = React.useRef(false);

    // Mirror of conversationId for async continuations: a response that
    // arrives after the active conversation changed (reset/delete in another
    // tab) is stale and must be discarded, never attached to whichever
    // conversation the UI is showing now (issue #458).
    const conversationIdRef = React.useRef<number | null>(null);
    React.useEffect(() => {
        conversationIdRef.current = conversationId;
    }, [conversationId]);

    // Set when honest pre-persistence handling restores the composer draft:
    // the finally block must not wipe it again (issue #458).
    const keepDraftRef = React.useRef(false);

    // The recovery draft currently surfaced in the composer (issue #458 r2):
    // a marker reconciled into the composer stays durable in storage until
    // the user explicitly sends or discards it — a scan or another turn's
    // reconciliation never silently deletes unsent content.
    const surfacedDraftRef = React.useRef<{conversationId: number; key: string} | null>(null);

    // Resolve the surfaced recovery draft (explicit send/discard only).
    const clearSurfacedDraft = () => {
        const surfaced = surfacedDraftRef.current;
        if (!surfaced || surfaced.conversationId !== conversationId) return;
        clearInflightTurn(surfaced.conversationId, surfaced.key);
        surfacedDraftRef.current = null;
    };

    // Ref to the last submitted turn so Retry replays the same content + idempotency key.
    const lastSent = React.useRef<{content: string; key: string} | null>(null);
    // Synchronous pending mirror so double clicks and concurrent retries cannot
    // start a second in-flight turn (apply-once, client side; the server's
    // 409 turn_in_progress is the backstop).
    const pendingRef = React.useRef(false);
    // Abort controller for the in-flight submission: stopping only stops the
    // client's wait; the server may still complete and is reconciled via GET.
    const abortRef = React.useRef<AbortController | null>(null);
    const statusRef = React.useRef<HTMLDivElement>(null);
    const historyRef = React.useRef<HTMLDivElement>(null);

    // Auto-resize the composer textarea up to a bounded max rows.
    const MAX_COMPOSER_ROWS = 6;
    const composerRef = React.useRef<HTMLTextAreaElement>(null);
    // Shared floor for the measured card height; must stay in sync with the
    // `20rem` inline minHeight below (16px rem * 20) so the two cannot drift.
    const MIN_CARD_PX = 320;
    const adjustComposerHeight = React.useCallback(() => {
        const ta = composerRef.current;
        if (!ta) return;
        ta.style.height = 'auto';
        if (!ta.value) {
            ta.style.overflowY = 'hidden';
            return;
        }
        const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 24;
        const maxHeight = lineHeight * MAX_COMPOSER_ROWS;
        const desired = Math.min(ta.scrollHeight, maxHeight);
        ta.style.height = `${desired}px`;
        ta.style.overflowY = ta.scrollHeight > maxHeight ? 'auto' : 'hidden';
    }, []);
    // Adjust the composer on mount and on every keystroke/content reset. This
    // only invokes the (stable) adjuster; it does *not* (re)register the passive
    // window/font listeners below, so typing does not recreate them each key.
    React.useEffect(() => {
        adjustComposerHeight();
    }, [adjustComposerHeight, input]);

    const loadAvailability = React.useCallback(async () => {
        if (availabilityRequested.current) return;
        availabilityRequested.current = true;
        try {
            const res = await fetch('/api/job-matches/status/');
            if (!res || !res.ok) return;
            const data = await res.json();
            if (data && typeof data.state === 'string' && typeof data.title === 'string') {
                setAvailability(data as AvailabilityPayload);
            }
        } catch {
            // Best-effort notice only; never blocks or breaks the chat.
        }
    }, []);

    // Fetch availability once when the latest assistant reply carries no
    // results — exactly the situation the notice exists to explain.
    React.useEffect(() => {
        const last = messages[messages.length - 1];
        if (last && last.role === 'assistant' && !hasResults(last.results)) {
            loadAvailability();
        }
    }, [messages, loadAvailability]);

    // The notice explains the most recent reply only; historical messages
    // without results stay quiet.
    const lastAssistantId = React.useMemo(() => {
        for (let i = messages.length - 1; i >= 0; i--) {
            if (messages[i].role === 'assistant') return messages[i].id;
        }
        return null;
    }, [messages]);

    // Register the long-lived listeners exactly once: window/viewport resize plus
    // a one-shot document.fonts.ready hook so the height is re-measured once web
    // fonts finish loading (the initial measure uses a fallback line-height before
    // the real face paints). Because adjustComposerHeight is stable and the only
    // dependency, these listeners are never re-registered per keystroke.
    React.useEffect(() => {
        // Defensive/idempotent mount-time measure: Effect 1 already runs
        // adjustComposerHeight when the input state settles, but this guarantees
        // the height is correct before the font/resize listeners are registered
        // — the two effects are intentionally order-independent.
        adjustComposerHeight();
        let cancelled = false;
        if (document.fonts && typeof document.fonts.ready?.then === 'function') {
            const reflow = () => { if (!cancelled) adjustComposerHeight(); };
            void document.fonts.ready.then(reflow, reflow);
        }
        window.addEventListener('resize', adjustComposerHeight);
        window.visualViewport?.addEventListener('resize', adjustComposerHeight);
        return () => {
            cancelled = true;
            window.removeEventListener('resize', adjustComposerHeight);
            window.visualViewport?.removeEventListener('resize', adjustComposerHeight);
        };
    }, [adjustComposerHeight]);
    const cardRef = React.useRef<HTMLElement>(null);
    const nearBottomRef = React.useRef(true);
    const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);
    const [cardHeight, setCardHeight] = React.useState<number | null>(null);
    // rAF bookkeeping so resize/orientation/keyboard bursts coalesce into at most
    // one measure per frame instead of thrashing layout on every event.
    const rafIdRef = React.useRef<number | null>(null);
    const rafPendingRef = React.useRef(false);

    // Measure the actual vertical space left after the page header, match panel,
    // and margins so the chat card fits the viewport instead of assuming a fixed
    // 7rem header offset. This keeps the composer visible and makes history the
    // single intentional scroll region.
    const measureCardHeight = React.useCallback(() => {
        const card = cardRef.current;
        if (!card) return;
        const viewport = window.visualViewport;
        const viewportHeight = viewport ? viewport.height : window.innerHeight;
        // Clamp so a negative offset when the page is scrolled cannot inflate the
        // card past the viewport (which would bury the composer below the fold).
        const top = Math.max(0, card.getBoundingClientRect().top);
        // Respect the device home-indicator inset (iPhone X+). env() is exposed as
        // a CSS custom property (popup.css) since it isn't directly readable.
        let safeAreaBottom = 0;
        try {
            const raw = getComputedStyle(document.documentElement)
                .getPropertyValue('--safe-area-inset-bottom').trim();
            const parsed = parseFloat(raw);
            safeAreaBottom = Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
        } catch {
            safeAreaBottom = 0;
        }
        const bottomGap = safeAreaBottom || 16; // breathing room above the page bottom
        const computed = viewportHeight - top - bottomGap;
        setCardHeight(Math.max(computed, MIN_CARD_PX));
    }, []);

    // Coalesce high-frequency resize/viewport events (fired many times per second
    // on mobile for orientation changes and keyboard show/hide) into one measure
    // per animation frame. The guard flag guarantees only a single rAF is ever
    // outstanding, so bursts do not thrash layout or re-render on every event.
    const scheduleMeasure = React.useCallback(() => {
        if (rafPendingRef.current) return;
        rafPendingRef.current = true;
        rafIdRef.current = window.requestAnimationFrame(() => {
            rafPendingRef.current = false;
            rafIdRef.current = null;
            measureCardHeight();
        });
    }, [measureCardHeight]);

    React.useEffect(() => {
        scheduleMeasure();
        window.addEventListener('resize', scheduleMeasure);
        window.visualViewport?.addEventListener('resize', scheduleMeasure);
        // Watch the card's offset parent so a match-panel resize above the chat
        // (e.g. empty -> results) re-measures the available height.
        let observer: ResizeObserver | null = null;
        if (typeof ResizeObserver !== 'undefined' && cardRef.current?.parentElement) {
            observer = new ResizeObserver(scheduleMeasure);
            observer.observe(cardRef.current.parentElement);
        }
        return () => {
            if (rafIdRef.current !== null) {
                window.cancelAnimationFrame(rafIdRef.current);
            }
            window.removeEventListener('resize', scheduleMeasure);
            window.visualViewport?.removeEventListener('resize', scheduleMeasure);
            observer?.disconnect();
        };
    }, [scheduleMeasure]);

    const chatCardStyle = React.useMemo<React.CSSProperties>(() => {
        if (cardHeight !== null) {
            return {height: `${cardHeight}px`, minHeight: '20rem'};
        }
        return {minHeight: '20rem'};
    }, [cardHeight]);

    const prefersReducedMotion = (): boolean => (
        typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );

    const isNearBottom = (element: HTMLDivElement): boolean => (
        element.scrollHeight - element.scrollTop - element.clientHeight <= 48
    );

    const scrollToLatest = (behavior: ScrollBehavior = prefersReducedMotion() ? 'auto' : 'smooth') => {
        const history = historyRef.current;
        if (!history) return;
        if (typeof history.scrollTo === 'function') {
            history.scrollTo({top: history.scrollHeight, behavior});
        } else {
            history.scrollTop = history.scrollHeight;
        }
        nearBottomRef.current = true;
        setShowJumpToLatest(false);
    };

    // Keep the latest content visible only while the reader is already at the bottom.
    React.useEffect(() => {
        const history = historyRef.current;
        if (!history) return;
        const handleScroll = () => {
            const nearBottom = isNearBottom(history);
            nearBottomRef.current = nearBottom;
            setShowJumpToLatest(!nearBottom);
        };
        history.addEventListener('scroll', handleScroll, {passive: true});
        return () => history.removeEventListener('scroll', handleScroll);
    }, []);

    // Initial history, optimistic turns, replies, and the pending indicator all append
    // content to the same viewport. Do not interrupt someone reading older messages.
    React.useEffect(() => {
        // Empty history (visual review #472 round 5): never auto-scroll — the
        // empty state stays anchored at the top of the log so its lead is
        // visible on first open, even on the shortest sheet viewports.
        // Auto-scroll resumes once a conversation exists or content is added.
        if (messages.length === 0) {
            return;
        }
        if (loading || !nearBottomRef.current) {
            if (!nearBottomRef.current) setShowJumpToLatest(true);
            return;
        }
        scrollToLatest();
    }, [messages.length, pending, loading]);

    // Visual viewport changes cover mobile keyboards and orientation changes. Preserve
    // the reader's position when they are browsing older messages.
    React.useEffect(() => {
        const handleViewportResize = () => {
            const history = historyRef.current;
            if (!history) return;
            const nearBottom = isNearBottom(history);
            nearBottomRef.current = nearBottom;
            setShowJumpToLatest(!nearBottom);
            if (nearBottom) scrollToLatest('auto');
        };
        window.addEventListener('resize', handleViewportResize);
        window.visualViewport?.addEventListener('resize', handleViewportResize);
        return () => {
            window.removeEventListener('resize', handleViewportResize);
            window.visualViewport?.removeEventListener('resize', handleViewportResize);
        };
    }, []);

    // Advisory availability check (issue #457). Runs on mount and is re-run
    // before each send and from the notice's retry affordance. A failed check
    // never blocks the chat: the POST path remains authoritative.
    const refreshStatus = React.useCallback(async (): Promise<AssistantStatus | null> => {
        setStatusChecking(true);
        try {
            const res = await csrfFetch('/api/agent/assistant-status/');
            if (!res.ok) throw new Error('status-failed');
            const data = (await res.json()) as AssistantStatus;
            setAssistantStatus(data);
            return data;
        } catch {
            // Advisory only: leave any previous status in place and keep the
            // chat usable (status never gates or ungate the send path itself).
            return null;
        } finally {
            setStatusChecking(false);
        }
    }, []);

    // Fetch the status on mount. Declared before the conversation-resume
    // effect so the status request is issued first (a deterministic order the
    // tests rely on). Advisory-only and read-only, so it is safe to run for
    // signed-out visitors too — the server reports `signed_out` for them and
    // the notice already renders nothing for that state.
    React.useEffect(() => {
        void refreshStatus();
    }, [refreshStatus]);

    // Adopt the pending pre-conversation draft (issue #465 AC-8) into
    // whichever conversation just became active for this authenticated
    // session, then clear the pending slot. A draft already typed into the
    // composer for this load wins — adoption only fills an empty composer.
    const adoptPendingDraft = (targetConversationId: number) => {
        const pending = readPendingDraft();
        if (!pending) return;
        clearPendingDraft();
        setInput((current) => {
            if (current) return current;
            writeComposerDraft(targetConversationId, pending);
            return pending;
        });
    };

    // Bumped by the purge listener below to force a fresh resume of the
    // (possibly different) account's conversation after an account-switch
    // purge (issue #465 AC-9). A sign-out purge is followed by a full
    // navigation, so it never reaches this effect.
    const [purgeGeneration, setPurgeGeneration] = React.useState(0);

    // Abort controller for the conversation resume/create requests, and a
    // monotonic purge epoch (issue #465 review round 2). A purge aborts the
    // in-flight resume and bumps the epoch, so a response that was already
    // decoding when the account switched is discarded instead of being
    // rendered — or having its pending draft adopted — into the new
    // account's view. The epoch is the backstop for the window where the
    // fetch has already resolved and `abort()` no longer has any effect.
    const resumeAbortRef = React.useRef<AbortController | null>(null);
    const purgeEpochRef = React.useRef(0);

    // Resume the user's most recent conversation on load. Skipped entirely
    // for a signed-out visitor (issue #465 AC-2): an anonymous GET must
    // create no JobSearchConversation/JobSearchMessage row, and the message
    // history region must stay empty until an authenticated load succeeds
    // (AC-10).
    React.useEffect(() => {
        if (!effectiveAuthenticated) {
            setLoading(false);
            setInput((current) => current || readPendingDraft());
            return;
        }
        let cancelled = false;
        const epoch = purgeEpochRef.current;
        const controller = new AbortController();
        resumeAbortRef.current = controller;
        // Stale once this effect run was torn down *or* a purge superseded
        // it: either way nothing from this response may reach the store.
        const stale = () => cancelled || purgeEpochRef.current !== epoch;
        setLoading(true);
        csrfFetch('/api/agent/conversations/', {signal: controller.signal})
            .then(async (res) => {
                if (stale()) return;
                if (res.status === 404 && !createOnMount) {
                    setLoading(false);
                    return;
                }
                if (res.status === 404) {
                    // No existing conversation — create one so the user can start chatting.
                    try {
                        const createRes = await csrfFetch('/api/agent/conversations/', {
                            method: 'POST',
                            body: JSON.stringify({create_new: true}),
                            signal: controller.signal,
                        });
                        if (stale()) return;
                        if (!createRes.ok) throw new Error('create-failed');
                        const createData = (await createRes.json()) as Conversation;
                        if (stale()) return;
                        setConversationId(createData.id);
                        setMessages(createData.messages);
                        reconcileDurableState(createData);
                        adoptPendingDraft(createData.id);
                        setLoading(false);
                        // Focus after React commits: a synchronous focus here
                        // lands on the still-disabled textarea (disabled until
                        // conversationId/loading commit) and is silently
                        // dropped, leaving the composer unfocused (CI: 400%
                        // zoom composer-focus race).
                        window.setTimeout(() => composerRef.current?.focus(), 0);
                    } catch {
                        if (stale()) return;
                        setInitError('Could not start a conversation. Please try again.');
                        setLoading(false);
                    }
                    return;
                }
                if (!res.ok) {
                    throw new Error(`Resume failed (${res.status})`);
                }
                const data = (await res.json()) as Conversation;
                if (stale()) return;
                setConversationId(data.id);
                setMessages(data.messages);
                setPreferencesChanged(data.preferences_changed);
                reconcileDurableState(data);
                adoptPendingDraft(data.id);
                setLoading(false);
                // Defer focus past the React commit (see above): the textarea
                // is disabled until conversationId/loading land.
                window.setTimeout(() => composerRef.current?.focus(), 0);
            })
            .catch(() => {
                if (stale()) return;
                setInitError('Could not load your conversation. Please refresh.');
                setLoading(false);
            });
        return () => {
            cancelled = true;
            if (resumeAbortRef.current === controller) {
                resumeAbortRef.current = null;
            }
        };
    }, [effectiveAuthenticated, purgeGeneration]);

    // Account-switch / sign-out purge (issue #465 AC-9). Any in-flight
    // submission is aborted (stopping only the client's wait — the server
    // may still complete, but this tab must never attach that reply to the
    // wrong account's view), and every private artefact for the previous
    // account is discarded so it can never be exposed.
    const resetForPurge = () => {
        // Bump first: a resume response already past `await` must see the
        // new epoch and discard itself even though abort() came too late.
        purgeEpochRef.current += 1;
        abortRef.current?.abort();
        resumeAbortRef.current?.abort();
        resumeAbortRef.current = null;
        surfacedDraftRef.current = null;
        lastSent.current = null;
        setMessages([]);
        setConversationId(null);
        conversationIdRef.current = null;
        setInput('');
        setError(null);
        setErrorType(null);
        setRetrying(false);
        setInitError(null);
        setPreferencesChanged(false);
        setPrefDismissed(false);
        if (effectiveAuthenticated) {
            // Force the resume effect to re-run so the (possibly different)
            // account's own conversation loads fresh — never the stale
            // conversation just cleared above.
            setPurgeGeneration((g) => g + 1);
        }
    };

    React.useEffect(() => {
        // Sign-out (issue #465 AC-9): app-nav.js purges storage and
        // dispatches this directly before its redirect.
        const handlePurged = () => resetForPurge();
        // Account switch while this page is already open (issue #465 AC-9):
        // app-nav.js's whoami hydration dispatches this on every load, and
        // it is the only signal for a switch that happened after the server
        // rendered `accountKey`. The load-time case is already handled
        // synchronously by reconcileAccountKey() above — this covers the
        // rest, using the same comparison so the two cannot disagree.
        const handleHydrated = (e: Event) => {
            const detail = (e as CustomEvent).detail as {authenticated?: boolean; username?: string} | undefined;
            if (!detail) return;
            setEffectiveAuthenticated(!!detail.authenticated);
            if (!detail.authenticated || !detail.username) return;
            if (reconcileAccountKey(detail.username)) {
                resetForPurge();
            }
        };
        document.addEventListener('crank:private-state-purged', handlePurged);
        document.addEventListener('crank:auth-hydrated', handleHydrated);
        return () => {
            document.removeEventListener('crank:private-state-purged', handlePurged);
            document.removeEventListener('crank:auth-hydrated', handleHydrated);
        };
    }, [effectiveAuthenticated]);

    // Announce new assistant content to assistive tech.
    React.useEffect(() => {
        if (historyRef.current) {
            const last = historyRef.current.lastElementChild;
            if (last instanceof HTMLElement) {
                last.setAttribute('aria-live', 'polite');
            }
        }
    }, [messages.length]);

    // With create-on-mount suppressed, a null conversation is a valid
    // not-started state: the first send creates it.
    const isReady = (conversationId !== null || !createOnMount) && !pending && !loading;

    // Composer gating from the advisory status (issue #457): only states where
    // sending is known-futile disable the input; a missing/failed status never
    // gates (advisory-only contract).
    const composerGated = isGatedState(assistantStatus?.state);

    // Reconcile durable client state against the server after a (re)load:
    // the server is the single source of truth. Markers are reconciled per
    // turn, independently (issue #458 r2): a turn the server confirms is
    // resolved (its content is server-side); a turn the server never received
    // is NEVER silently deleted — eager clearing permanently lost unsent
    // content whenever more than one marker existed. Unsent markers stay
    // durable in storage: the newest surfaces as the composer draft, and
    // older ones surface on later loads of this conversation view once the
    // newer ones are explicitly sent or discarded. Markers only clear when
    // the server confirms the turn, on an explicit send/discard of the
    // surfaced draft, or when the conversation is reset/deleted — never on
    // a scan.
    const reconcileDurableState = (conversation: Conversation) => {
        const markers = readInflightTurns(conversation.id);
        const serverKeys = new Set(
            conversation.messages
                .filter((m) => m.role === 'user' && m.idempotency_key)
                .map((m) => m.idempotency_key as string),
        );
        let restoreDraft: InFlightTurn | null = null;
        for (const marker of markers) {
            if (serverKeys.has(marker.key)) {
                // The server knows this turn: the marker is resolved and its
                // content is represented in the conversation history.
                clearInflightTurn(conversation.id, marker.key);
                continue;
            }
            // The server never received this turn: keep the marker as a
            // recoverable draft candidate (the newest wins the composer).
            if (!restoreDraft || marker.ts > restoreDraft.ts) {
                restoreDraft = marker;
            }
        }
        const draft = readComposerDraft(conversation.id);
        if (restoreDraft && !input && (!draft || restoreDraft.ts > readComposerDraftTs(conversation.id))) {
            // Surface the newest unsent turn as the unsent draft instead of a
            // fake sent message. The marker stays durable until the user
            // explicitly sends or discards it. A composer draft edited after
            // the failed send is newer (timestamped) and wins instead —
            // surfacing never clobbers the user's latest typing.
            surfacedDraftRef.current = {conversationId: conversation.id, key: restoreDraft.key};
            setInput(restoreDraft.content);
            writeComposerDraft(conversation.id, restoreDraft.content);
        } else if (!input && draft) {
            setInput(draft);
        }
    };

    // Re-fetch the conversation and adopt the server state: used after a 409
    // (another tab is running the turn), after a client-side stop, and for
    // any uncertain failure — the server is the single source of truth. Only
    // the given turn's marker is resolved, so one turn's reconciliation can
    // never clear another in-flight turn's marker (issue #458). Returns where
    // the turn ended up: 'present' (the server knows it — the marker is
    // cleared), 'absent' (the server never received it — the marker stays
    // durable exactly like the load-time contract; the caller surfaces the
    // kept marker instead of dropping it), 'gone' (the conversation
    // disappeared), or 'unknown' (the check itself failed — markers stay for
    // the next load).
    const reconcileWithServer = async (
        targetId: number,
        turnKey: string,
    ): Promise<'present' | 'absent' | 'gone' | 'unknown'> => {
        try {
            const res = await csrfFetch(`/api/agent/conversations/${targetId}/`);
            if (res.status === 404) {
                // The conversation was deleted/reset elsewhere; drop its
                // markers rather than resurrecting it.
                clearInflightTurns(targetId);
                return 'gone';
            }
            if (!res.ok) return 'unknown';
            const data = (await res.json()) as Conversation;
            setMessages(data.messages);
            setPreferencesChanged(data.preferences_changed);
            const known = data.messages.some(
                (m) => m.role === 'user' && m.idempotency_key === turnKey,
            );
            if (known) {
                // The server holds this turn: its marker is resolved — its
                // content is represented in the conversation history.
                clearInflightTurn(targetId, turnKey);
            }
            // Absent does NOT clear: the marker stays durable so two tabs
            // both confirmed absent cannot silently delete each other's
            // unsent turn (issue #458 r3). The caller surfaces the kept
            // marker newest-wins, exactly like the load-time path.
            return known ? 'present' : 'absent';
        } catch {
            // Network hiccup: the marker stays and the next load reconciles.
            return 'unknown';
        }
    };

    // Surface a kept unsent-turn marker in the composer exactly like the
    // load-time path: the newest unsent content wins, and a draft the user
    // edited after the send is newer still and wins instead — surfacing
    // never clobbers the user's latest typing. The marker stays durable
    // either way until it is explicitly sent or discarded.
    const keepUnsentTurn = (targetId: number, marker: InFlightTurn) => {
        const draft = readComposerDraft(targetId);
        if (!draft || marker.ts > readComposerDraftTs(targetId)) {
            surfacedDraftRef.current = {conversationId: targetId, key: marker.key};
            setInput(marker.content);
            writeComposerDraft(targetId, marker.content);
        }
    };

    const ensureConversation = async (createNew: boolean): Promise<number> => {
        const body = createNew ? {create_new: true} : {};
        const res = await csrfFetch('/api/agent/conversations/', {
            method: 'POST',
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            throw new Error('start-failed');
        }
        const data = (await res.json()) as Conversation;
        setConversationId(data.id);
        setMessages(data.messages);
        return data.id;
    };

    const sendTurn = async (
        content: string,
        key: string,
        opts?: {conversationId?: number | null; retriedAfterClose?: boolean},
    ) => {
        const turnConversationId = opts && 'conversationId' in opts ? opts.conversationId : conversationId;
        // The conversation_closed recovery replays from inside the original
        // send, which still holds the pending guard.
        if (turnConversationId == null || (pendingRef.current && !opts?.retriedAfterClose)) return;
        pendingRef.current = true;
        keepDraftRef.current = false;
        setPending(true);
        setError(null);
        setErrorType(null);
        setRetrying(false);
        setRetryKey(null);

        // Record the in-flight turn durably before the request resolves, so a
        // reload/navigation during the wait cannot lose it (issue #458). The
        // marker is stored per turn (conversation + key): concurrent turns or
        // tabs never clobber each other's recovery state.
        const markerTs = Date.now();
        writeInflightTurn({conversationId: turnConversationId, content, key, ts: markerTs});

        // Optimistically reflect the user's turn: appended when this is a new
        // turn; for a retry the persisted user message is already in history
        // and is flipped back to pending in place instead.
        // A conversation_closed replay targets a fresh conversation: the
        // `messages` captured by this closure still belong to the closed one.
        const existingUser = opts?.retriedAfterClose ? undefined : messages.find(
            (m) => m.role === 'user' && m.idempotency_key === key,
        );
        const optimisticUser: ChatMessage = {
            id: -Date.now(),
            role: 'user',
            content,
            preferences_changed: false,
            created: new Date().toISOString(),
            results: null,
            idempotency_key: key,
            delivery_state: 'pending',
        };
        if (existingUser) {
            // Retry of a persisted turn: it stays in place and its failure
            // treatment is REPLACED by the retry-in-progress state.
            setRetryKey(key);
        } else {
            setMessages((prev) => [...prev, optimisticUser]);
        }

        const controller = new AbortController();
        abortRef.current = controller;

        // The server confirmed it never received this turn (issue #458 r3/r4):
        // this covers uncertain responses reconciled to `absent` AND the
        // definitive pre-persistence typed failures (rate_limited,
        // invalid_message, not_found, ...). The marker stays durable — only a
        // server-present confirmation, an explicit send/discard of the
        // surfaced draft, or a gone conversation clears it — and the kept
        // marker is surfaced newest-wins like the load-time path, so two
        // concurrent absent tabs can no longer collapse two unsent turns into
        // the single shared draft slot. The error copy stays honest: the
        // caller decides whether it reads the server's typed message or the
        // generic "may not have been sent" text.
        // the marker stays durable — only a server-present confirmation, an
        // explicit send/discard of the surfaced draft, or a gone conversation
        // clears it — and the kept marker is surfaced newest-wins like the
        // load-time path, so two concurrent absent reconciliations can no
        // longer collapse two unsent turns into the single shared draft slot.
        const handleConfirmedUnsent = (message: string, type: string | null) => {
            lastSent.current = null;
            keepDraftRef.current = true;
            setErrorType(type);
            setError(message);
            if (!existingUser) {
                setMessages((prev) => prev.filter((m) => m !== optimisticUser));
            }
            keepUnsentTurn(turnConversationId, {
                conversationId: turnConversationId, content, key, ts: markerTs,
            });
        };

        try {
            const res = await csrfFetch(`/api/agent/conversations/${turnConversationId}/`, {
                method: 'POST',
                body: JSON.stringify({content, idempotency_key: key}),
                signal: controller.signal,
            });
            // A reset/delete in another tab (or anything else that switched the
            // active conversation) makes this completion stale: discard it rather
            // than attaching a reply from an old conversation to the new one
            // (issue #458).
            if (conversationIdRef.current !== turnConversationId) {
                clearInflightTurn(turnConversationId, key);
                return;
            }
            if (!res.ok) {
                let serverMsg = `Request failed (${res.status})`;
                let serverType: string | undefined;
                let parsed = false;
                try {
                    const body = (await res.json()) as ApiError;
                    parsed = true;
                    if (body.error) {
                        if (body.error.message) serverMsg = body.error.message;
                        if (body.error.type) serverType = body.error.type;
                    }
                } catch {
                    // Non-JSON error (proxy/gateway): treated as uncertain below.
                }
                if (serverType === 'turn_in_progress') {
                    // Another request is already running this turn. Surface the
                    // honest status and adopt the server state.
                    setError(serverMsg);
                    setErrorType(serverType);
                    await reconcileWithServer(turnConversationId, key);
                    return;
                }
                if (serverType === 'retry_limit_reached') {
                    // The turn exhausted its retry cap. Keep the failed turn
                    // visible and adopt the server's exhausted state so the
                    // copy and the Retry affordance are honest.
                    setError(serverMsg);
                    setErrorType(serverType);
                    await reconcileWithServer(turnConversationId, key);
                    return;
                }
                if (serverType === 'conversation_closed') {
                    // conversation_closed (issue #487): the conversation was
                    // reset or deleted while the turn was in flight, but the
                    // user turn stays retryable with the SAME idempotency key.
                    // Recover by switching to the user's active conversation —
                    // the reset's fresh one, or a newly created one after
                    // delete — and replaying the retained turn there exactly
                    // once. The replay records its own marker on the new
                    // conversation, so the closed one's marker is resolved.
                    if (!existingUser) {
                        setMessages((prev) => prev.filter((m) => m !== optimisticUser));
                    }
                    if (!opts?.retriedAfterClose) {
                        try {
                            const newId = await ensureConversation(false);
                            clearInflightTurn(turnConversationId, key);
                            // Adopt the new conversation synchronously so the
                            // replay's stale-conversation guard accepts it.
                            conversationIdRef.current = newId;
                            await sendTurn(content, key, {
                                conversationId: newId,
                                retriedAfterClose: true,
                            });
                            return;
                        } catch {
                            // Recovery failed; surface the original error below.
                        }
                    }
                    setError(serverMsg);
                    setErrorType(serverType);
                    lastSent.current = {content, key};
                    setRetrying(true);
                    return;
                }
                if (parsed && serverType && PRE_PERSISTENCE_ERROR_TYPES.has(serverType)) {
                    // Validation/budget/gone-conversation failures happen
                    // before persistence: not saved, nothing to retry. The
                    // honest server copy surfaces, and the per-turn marker
                    // stays durable like every other confirmed-absent path
                    // (issue #458 r4) — the draft is only ever resolved
                    // explicitly by the user.
                    handleConfirmedUnsent(serverMsg, serverType || null);
                    return;
                }
                if (parsed && serverType && POST_PERSISTENCE_ERROR_TYPES.has(serverType)) {
                    // The server persisted the turn before failing: keep the
                    // question visible as a failed turn so retry survives reload.
                    // The server-side attempt cap is the backstop for exhausted
                    // retries (a retry_limit_reached response reconciles the
                    // honest exhausted state below).
                    setErrorType(serverType);
                    setError(serverMsg);
                    lastSent.current = {content, key};
                    setRetrying(true);
                    setMessages((prev) => prev.map(
                        (m) => (m === (existingUser || optimisticUser)
                            ? {...m, delivery_state: 'failed'}
                            : m),
                    ));
                    return;
                }
                // Unknown typed error or a non-JSON response: whether the
                // server received the request is uncertain. The server is
                // the source of truth — reconcile now instead of guessing.
                const outcome = await reconcileWithServer(turnConversationId, key);
                if (outcome === 'absent') {
                    handleConfirmedUnsent(
                        'Your message may not have been sent. It has been kept as a draft below.',
                        serverType || null,
                    );
                    return;
                }
                if (outcome === 'gone') {
                    setError('This conversation is no longer available.');
                    setErrorType('not_found');
                    return;
                }
                // The server knows the turn (pending/failed/completed): its
                // state is rendered; let the turn's own panel speak.
                setError(serverMsg);
                setErrorType(serverType || null);
                lastSent.current = {content, key};
                setRetrying(true);
                return;
            }
            const data = (await res.json()) as SubmitResponse;
            if (conversationIdRef.current !== turnConversationId) {
                clearInflightTurn(turnConversationId, key);
                return;
            }
            // Keep the turn in its ORIGINAL position and insert the reply
            // immediately after it: a retried turn must never reorder the
            // transcript, on send or after reload (issue #458).
            setMessages((prev) => {
                const idx = prev.findIndex(
                    (m) => m.role === 'user' && m.idempotency_key === key,
                );
                if (idx === -1) {
                    return [...prev, {...optimisticUser, delivery_state: 'completed'}, data.message];
                }
                const next = [...prev];
                next[idx] = {...next[idx], delivery_state: 'completed'};
                next.splice(idx + 1, 0, data.message);
                return next;
            });
            if (data.preferences_changed) {
                setPreferencesChanged(true);
                setPrefDismissed(false);
            }
            clearInflightTurn(turnConversationId, key);
            writeComposerDraft(turnConversationId, '');
            lastSent.current = null;
        } catch (e) {
            if (conversationIdRef.current !== turnConversationId) {
                clearInflightTurn(turnConversationId, key);
                return;
            }
            if (controller.signal.aborted) {
                // Honest cancel semantics: stopping only stops the client's
                // wait. Ask the server what actually happened to the turn.
                const outcome = await reconcileWithServer(turnConversationId, key);
                if (outcome === 'absent') {
                    handleConfirmedUnsent(
                        'Stopped before the message was sent. It has been kept as a draft below.',
                        null,
                    );
                    return;
                }
                if (outcome === 'gone') {
                    setError('This conversation is no longer available.');
                    setErrorType('not_found');
                    return;
                }
                // The server is (still) processing or already finished: its
                // state is now rendered; tell the user how to follow up.
                setError(
                    'Stopped waiting. The response will appear when it is ready — use "Check for response" below.',
                );
                lastSent.current = {content, key};
                setRetrying(true);
                return;
            }
            // Network-level failure: whether the server received the request
            // is unknown; reconcile against the server instead of guessing.
            const outcome = await reconcileWithServer(turnConversationId, key);
            if (outcome === 'absent') {
                handleConfirmedUnsent(
                    'Your message may not have been sent. It has been kept as a draft below.',
                    null,
                );
                return;
            }
            if (outcome === 'gone') {
                setError('This conversation is no longer available.');
                setErrorType('not_found');
                return;
            }
            setError(e instanceof Error ? e.message : 'Something went wrong.');
            lastSent.current = {content, key};
            setRetrying(true);
        } finally {
            abortRef.current = null;
            pendingRef.current = false;
            setRetryKey(null);
            setPending(false);
            // Only clear/refocus the composer if we are still on the same
            // conversation: a mid-flight switch must not wipe the new draft.
            if (conversationIdRef.current === turnConversationId && !keepDraftRef.current) {
                setInput('');
                window.setTimeout(() => composerRef.current?.focus(), 0);
            } else if (conversationIdRef.current === turnConversationId) {
                window.setTimeout(() => composerRef.current?.focus(), 0);
            }
        }
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        const content = input.trim();
        if (!content || !isReady || composerGated) return;
        await submitTurn(content);
    };

    // Pre-send advisory re-check (issue #457): fetch the status and abort the
    // turn when it now reports a futile state. The notice is already updated
    // by refreshStatus; a failed check (null) proceeds with the send.
    const submitTurn = async (content: string) => {
        const status = await refreshStatus();
        if (status && isGatedState(status.state)) return;
        let targetId = conversationId;
        if (targetId === null) {
            try {
                targetId = await ensureConversation(true);
                conversationIdRef.current = targetId;
            } catch {
                setInitError('Could not start a conversation. Please try again.');
                return;
            }
        }
        // Explicit send resolves the surfaced recovery draft (issue #458 r2):
        // its content is being dealt with now, so it is no longer an unsent
        // turn to recover. Other unsent markers stay untouched. A gated
        // (aborted) send keeps the draft recoverable.
        clearSurfacedDraft();
        await sendTurn(content, newId(), {conversationId: targetId});
    };

    const handleRetryMessage = async (message: ChatMessage) => {
        if (!message.idempotency_key) return;
        // The server's per-turn attempt cap is exhausted: no retry possible.
        if (message.retry_available === false) return;
        await sendTurn(message.content, message.idempotency_key);
    };

    const handleEditAsNew = (message: ChatMessage) => {
        setInput(message.content);
        writeComposerDraft(conversationId, message.content);
        composerRef.current?.focus();
    };

    const handleStopWaiting = () => {
        abortRef.current?.abort();
    };

    const handleCheckResponse = async (message: ChatMessage) => {
        if (!conversationId || !message.idempotency_key) return;
        const targetId = conversationId;
        const turnKey = message.idempotency_key;
        const outcome = await reconcileWithServer(targetId, turnKey);
        if (outcome === 'absent') {
            // Confirmed unsent (issue #458 r3): the marker stays durable and
            // the kept content surfaces newest-wins, like the load-time path;
            // the optimistic pending bubble stops posing as in-flight.
            setError('Your message may not have been sent. It has been kept as a draft below.');
            setErrorType(null);
            const marker = readInflightTurns(targetId).find((mk) => mk.key === turnKey);
            if (marker) keepUnsentTurn(targetId, marker);
            setMessages((prev) => prev.filter((mm) => !(mm === message && mm.id < 0)));
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            const content = input.trim();
            if (content && isReady && !composerGated) {
                // Explicit send resolves the surfaced recovery draft (see
                // submitTurn).
                void submitTurn(content);
            }
        }
    };

    const handleRetry = async () => {
        const sent = lastSent.current;
        if (!sent) return;
        setRetrying(false);
        setError(null);
        setErrorType(null);
        await sendTurn(sent.content, sent.key);
    };

    const handleCreateConversation = async () => {
        setInitError(null);
        setLoading(true);
        try {
            await ensureConversation(true);
        } catch {
            setInitError('Could not start a conversation. Please try again.');
        } finally {
            setLoading(false);
            // Defer focus past the React commit (see above).
            window.setTimeout(() => composerRef.current?.focus(), 0);
        }
    };

    const handleExport = async () => {
        if (!conversationId) return;
        try {
            const res = await csrfFetch(`/api/agent/conversations/${conversationId}/export/`);
            if (!res.ok) throw new Error('export-failed');
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `job-search-${conversationId}.json`;
            a.click();
            URL.revokeObjectURL(a.href);
        } catch {
            setError('Could not export your conversation.');
        }
    };

    const handleReset = async () => {
        if (!conversationId) return;  // # pragma: no cover - button is disabled without a conversation
        if (!window.confirm('Start a new conversation? Your current history will be archived.')) return;
        try {
            const res = await csrfFetch(`/api/agent/conversations/${conversationId}/reset/`, {method: 'POST'});
            if (!res.ok) throw new Error('reset-failed');
            const data = (await res.json()) as Conversation;
            // The old conversation is archived: clear its turn markers and
            // draft. Markers are keyed per conversation, so another
            // conversation's in-flight turns are untouched (issue #458).
            clearInflightTurns(conversationId);
            writeComposerDraft(conversationId, '');
            surfacedDraftRef.current = null;
            setConversationId(data.id);
            setMessages([]);
            setPreferencesChanged(false);
            setPrefDismissed(false);
            setError(null);
            composerRef.current?.focus();
        } catch {
            setError('Could not reset the conversation.');
        }
    };

    const handleDelete = async () => {
        if (!conversationId) return;
        if (!window.confirm('Delete this conversation permanently? This cannot be undone.')) return;
        try {
            const res = await csrfFetch(`/api/agent/conversations/${conversationId}/delete/`, {method: 'POST'});
            if (!res.ok) throw new Error('delete-failed');
            clearInflightTurns(conversationId);
            writeComposerDraft(conversationId, '');
            surfacedDraftRef.current = null;
            setConversationId(null);
            setMessages([]);
            setPreferencesChanged(false);
            setPrefDismissed(false);
            setError(null);
            composerRef.current?.focus();
        } catch {
            setError('Could not delete the conversation.');
        }
    };

    const revealingPrefs = preferencesChanged && !prefDismissed;

    return (
        <section className="card bg-dark d-flex flex-column" data-testid="job-search-chat"
                 aria-labelledby="job-search-chat-title"
                 ref={cardRef}
                 style={chatCardStyle}>
            <div className="card-header d-flex justify-content-between align-items-center">
                <h2 id="job-search-chat-title" className="h6 mb-0">Conversation</h2>
                <div className="btn-group btn-group-sm flex-wrap" role="group" aria-label="Conversation controls">
                    <button type="button" className="btn btn-outline-light" onClick={handleExport}
                            disabled={!conversationId || !messages.length}>Export chat</button>
                    <button type="button" className="btn btn-outline-light" onClick={handleReset}
                            disabled={!conversationId || pending}>Reset chat</button>
                    <button type="button" className="btn btn-outline-danger" onClick={handleDelete}
                            disabled={!conversationId || pending}>Delete conversation</button>
                </div>
            </div>

            <div className="card-body d-flex flex-column" style={{minHeight: 0}}>
                <div id="job-search-data-note" className="chat-note" role="note">
                    <div className="chat-note-row">
                        <i className="fa-solid fa-circle-info chat-status-icon" aria-hidden="true"></i>
                        <span className="chat-note-text">The assistant is automated and can be wrong. Check important details yourself.</span>
                        <button type="button" className="chat-note-toggle chat-focus" aria-expanded={dataNoteOpen}
                                aria-controls="job-search-data-note-details" onClick={() => setDataNoteOpen((o) => !o)}
                                data-testid="data-note-toggle">
                            {dataNoteOpen ? 'Hide details' : 'Details'}
                        </button>
                    </div>
                    <div id="job-search-data-note-details" className={dataNoteOpen ? 'chat-note-details' : 'visually-hidden'}>
                        Your messages and preference updates are saved to your account; use Export, Reset, or Delete
                        above to manage them.
                    </div>
                </div>

                {revealingPrefs && (
                    <div className="alert alert-success d-flex justify-content-between align-items-center"
                         role="status" aria-label="Preference update" aria-describedby="preference-update-help">
                        <span>
                            <i className="fa-solid fa-circle-check me-1"></i>
                            Your saved preferences were updated based on this conversation.
                        </span>
                        <span id="preference-update-help" className="visually-hidden">
                            You can correct or remove a preference by telling the assistant what to change.
                        </span>
                        <button type="button" className="btn-close" aria-label="Dismiss preference notice"
                                onClick={() => setPrefDismissed(true)}></button>
                    </div>
                )}

                {!effectiveAuthenticated && (
                    // Signed-out introduction (issue #465 AC-2/AC-6): mirrors
                    // the server-rendered block above the card (present
                    // before hydration and for no-JS/screen-reader-first
                    // reads) so the mounted widget stays self-contained for
                    // #472's later relocation. The message-history region
                    // below never fetches or renders content for this
                    // requester (AC-10) — an expired session must never
                    // expose the previous account's conversation.
                    <div data-testid="signed-out-introduction" className="mb-3">
                        <p className="text-muted mb-2">
                            {signedOutMessage}
                        </p>
                        <a href={signInUrl} className="btn btn-primary btn-sm" data-testid="chat-sign-in-cta">
                            Sign in to save your search
                        </a>
                        <p id="job-search-signed-out-reason" className="text-muted small mt-2 mb-0">
                            You can type a message below; sign in to send it and save your conversation.
                        </p>
                    </div>
                )}

                {initError && (
                    <div className="alert alert-danger" role="alert">
                        {initError}
                        <div className="mt-2">
                            <button type="button" className="btn btn-sm btn-primary" onClick={handleCreateConversation}>
                                Start a conversation
                            </button>
                        </div>
                    </div>
                )}

                <div className="d-flex flex-column flex-grow-1" style={{minHeight: 0}}>
                    <div className="bg-dark border rounded p-3 mb-3 flex-grow-1" style={{minHeight: 0, overflowY: 'auto'}}
                         ref={historyRef} role="log" aria-live="polite" aria-label="Message history" aria-busy={pending}>
                        {effectiveAuthenticated && messages.length === 0 && !loading && !initError && (
                            <div data-testid="empty-history">
                                <p className="empty-history-lead mb-2">
                                    Ask about compensation, work location, funding, or culture to get started.
                                </p>
                                <p className="empty-history-note small mb-3">
                                    <i className="fa-solid fa-circle-info me-1"></i>
                                    {props.workspaceMode === 'sheet'
                                        ? 'Tap Back to results to view job-match status and results.'
                                        : 'Job-match status and results appear in the Job Matches panel.'}
                                </p>
                                <button type="button" className="btn btn-primary empty-history-cta"
                                        data-testid="empty-history-cta"
                                        onClick={() => composerRef.current?.focus()}>
                                    <i className="fa-solid fa-pen-to-square me-1" aria-hidden="true"></i>
                                    Ask your first question
                                </button>
                            </div>
                        )}
                        {messages.map((m) => (
                            <article key={m.id} aria-label={m.role === 'user' ? 'Your message' : 'Assistant message'}
                                     className={`d-flex ${m.role === 'user' ? 'justify-content-end' : 'justify-content-start'} mb-2`}>
                                <div className={`chat-bubble ${m.role === 'user' ? 'chat-bubble-user' : 'chat-bubble-assistant'}`}
                                     style={{maxWidth: '80%', wordBreak: 'break-word'}}>
                                    <div style={{whiteSpace: 'pre-wrap', wordBreak: 'break-word'}}>{m.content}</div>
                                    {m.role === 'assistant' && m.results && (
                                        <ResultCards results={m.results} />
                                    )}
                                    {m.role === 'assistant' && m.id === lastAssistantId && !hasResults(m.results) && availability && availability.state !== 'ok' && (
                                        <AvailabilityNotice availability={availability} />
                                    )}
                                    {m.role === 'user' && m.delivery_state === 'failed' && retryKey !== m.idempotency_key && (
                                        <div className="chat-failure-panel mt-2" data-testid="failed-turn">
                                            <div className="chat-status-row" role="status">
                                                <i className="fa-solid fa-triangle-exclamation chat-status-icon" aria-hidden="true"></i>
                                                <div>
                                                    {m.retry_available === false ? (
                                                        <div>Response failed after several retries. Your message is saved.</div>
                                                    ) : (
                                                        <div>Response failed. Your message is saved; retries are limited.</div>
                                                    )}
                                                </div>
                                            </div>
                                            <div className="chat-actions mt-3" role="group" aria-label="Failed turn actions">
                                                {m.retry_available === false ? (
                                                    <button type="button" className="chat-btn chat-btn-primary chat-focus" disabled
                                                            aria-label="Retry limit reached" data-testid="retry-response-button">
                                                        Retry limit reached
                                                    </button>
                                                ) : (
                                                    <button type="button" className="chat-btn chat-btn-primary chat-focus"
                                                            onClick={() => handleRetryMessage(m)}
                                                            aria-label="Retry response" data-testid="retry-response-button">
                                                        Retry response
                                                    </button>
                                                )}
                                                <button type="button" className="chat-btn chat-btn-secondary chat-focus"
                                                        onClick={() => handleEditAsNew(m)}
                                                        aria-label="Edit as new message" data-testid="edit-as-new-button">
                                                    Edit as new message
                                                </button>
                                            </div>
                                        </div>
                                    )}
                                    {m.role === 'user' && m.delivery_state === 'pending' && retryKey !== m.idempotency_key && (
                                        <div className="chat-retry-panel mt-2" data-testid="pending-turn">
                                            <div className="chat-status-row" role="status">
                                                <i className="fa-solid fa-hourglass-half chat-status-icon" aria-hidden="true"></i>
                                                <div>
                                                    <div>The response has not arrived yet.</div>
                                                </div>
                                            </div>
                                            <div className="chat-actions mt-3" role="group" aria-label="Pending turn actions">
                                                <button type="button" className="chat-btn chat-btn-primary chat-focus"
                                                        onClick={() => void handleCheckResponse(m)}
                                                        aria-label="Check for response" data-testid="check-response-button">
                                                    Check for response
                                                </button>
                                            </div>
                                        </div>
                                    )}
                                    {m.role === 'user' && retryKey !== null && m.idempotency_key === retryKey && m.delivery_state !== 'completed' && (
                                        <div className="chat-retry-panel mt-2" data-testid="retrying-turn">
                                            <div className="chat-status-row" role="status">
                                                <i className="fa-solid fa-spinner fa-spin chat-status-icon" aria-hidden="true"></i>
                                                <div>
                                                    <div>Retrying response…</div>
                                                </div>
                                            </div>
                                            <div className="chat-actions mt-3" role="group" aria-label="Failed turn actions">
                                                <button type="button" className="chat-btn chat-btn-primary" disabled
                                                        aria-label="Retrying response" data-testid="retry-response-button">
                                                    Retrying…
                                                </button>
                                                <button type="button" className="chat-btn chat-btn-secondary" disabled
                                                        aria-label="Edit as new message" data-testid="edit-as-new-button">
                                                    Edit as new message
                                                </button>
                                            </div>
                                        </div>
                                    )}
                                </div>
                            </article>
                        ))}
                        {pending && (
                            <div className="text-muted" role="status" aria-live="polite" data-testid="pending-status">
                                <i className="fa-solid fa-spinner fa-spin me-1"></i>Assistant is typing…
                            </div>
                        )}
                    </div>
                    {showJumpToLatest && (
                        /* Reserved footer slot (round-2 critique): the control
                           sits in its own row below the history instead of
                           floating over message content. */
                        <div className="flex-shrink-0 text-end mb-2">
                            <button type="button" className="btn btn-sm btn-primary"
                                    onClick={() => scrollToLatest('auto')} aria-label="Jump to latest message"
                                    data-testid="jump-to-latest">
                                New messages · Jump to latest
                            </button>
                        </div>
                    )}
                </div>

                <div className="flex-shrink-0" style={{paddingBottom: 'calc(0.25rem + env(safe-area-inset-bottom))'}}>
                    {assistantStatus && (
                        <AssistantStatusNotice
                            status={assistantStatus}
                            checking={statusChecking}
                            onRetry={() => { void refreshStatus(); }}
                        />
                    )}

                    {error && (
                        <div className="alert alert-danger d-flex justify-content-between align-items-center"
                             role="alert" data-testid="chat-error" data-error-type={errorType || undefined}>
                            <span className="flex-grow-1 me-2">{error}</span>
                            {retrying && (
                                <button type="button"
                                        className="btn btn-sm btn-outline-danger ms-2 flex-shrink-0 text-nowrap chat-focus"
                                        onClick={handleRetry} disabled={pending} data-testid="retry-button">
                                    Retry
                                </button>
                            )}
                        </div>
                    )}

                    <form onSubmit={handleSubmit} aria-busy={pending}>
                        {/* chat-composer-pending (round-2 critique): while the Stop
                            control renders, compact Send to an icon-only 44px target on
                            narrow screens so the row never clips the placeholder. */}
                        <div className={`input-group${pending ? ' chat-composer-pending' : ''}`}>
                            <textarea
                                ref={composerRef}
                                className="form-control chat-focus"
                                placeholder="Type your message…"
                                aria-label="Message"
                                aria-describedby={!effectiveAuthenticated ? 'job-search-signed-out-reason' : undefined}
                                value={input}
                                onChange={(e) => {
                                    setInput(e.target.value);
                                    if (effectiveAuthenticated) {
                                        writeComposerDraft(conversationId, e.target.value);
                                    } else {
                                        // Sensitive draft text (issue #465 AC-8):
                                        // kept client-side only, in a
                                        // pre-conversation slot since a
                                        // signed-out visitor has no
                                        // conversation id to key it against.
                                        // Never sent anywhere, including the
                                        // sign-in `next`.
                                        writePendingDraft(e.target.value);
                                    }
                                    // Explicit discard (issue #458 r2): emptying the
                                    // composer throws the surfaced recovery draft
                                    // away for good — other unsent turns stay
                                    // recoverable in storage.
                                    if (e.target.value === '') {
                                        clearSurfacedDraft();
                                    }
                                }}
                                onKeyDown={handleKeyDown}
                                disabled={effectiveAuthenticated ? ((createOnMount && !conversationId) || pending || composerGated) : pending}
                                autoComplete="off"
                                rows={1}
                                style={{resize: 'none', overflowY: 'hidden'}}
                            />
                            <button type="submit" className="btn btn-primary chat-send chat-focus"
                                    disabled={!effectiveAuthenticated || (createOnMount && !conversationId) || pending || composerGated || !input.trim()}
                                    aria-label="Send message" aria-describedby={!effectiveAuthenticated ? 'job-search-signed-out-reason' : undefined}>
                                <i className="fa-solid fa-paper-plane" aria-hidden="true"></i>
                                <span>Send</span>
                            </button>
                            {pending && (
                                <button type="button" className="btn btn-outline-light chat-stop chat-focus" onClick={handleStopWaiting}
                                        aria-label="Stop waiting for response" data-testid="stop-button">
                                    <i className="fa-solid fa-circle-stop" aria-hidden="true"></i>
                                    <span>Stop</span>
                                </button>
                            )}
                        </div>
                    </form>
                </div>

                {/* Screen-reader-only live region for pending/error transitions. */}
                <div ref={statusRef} className="visually-hidden" role="status" aria-live="assertive">
                    {pending ? 'Sending message.' : ''}
                    {!pending && error ? (
                        errorType && PRE_PERSISTENCE_ERROR_TYPES.has(errorType)
                            ? 'Your message could not be sent.'
                            : errorType && POST_PERSISTENCE_ERROR_TYPES.has(errorType)
                                ? 'Your message is saved; the response is not available yet.'
                                : 'Something went wrong with this response. Check the message for the latest status.'
                    ) : ''}
                </div>
            </div>
        </section>
    );
};

export default JobSearchChat;

