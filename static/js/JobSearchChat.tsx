// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createRoot} from 'react-dom/client';

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

function getCookie(name: string): string {
    const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match[2]) : '';
}

// Durable in-flight turn marker (issue #458). Written before a submission
// resolves so a request that never received a response survives reload and
// navigation, and reconciled against the server on the next load: the server
// is the single source of truth, the marker only covers the window it cannot
// see. Keyed by conversation id so a late reply can never attach to a
// different conversation.
interface InFlightTurn {conversationId: number; content: string; key: string}

const INFLIGHT_STORAGE_KEY = 'crank:jobsearch:inflight';

function draftKey(conversationId: number): string {
    return `crank:jobsearch:draft:${conversationId}`;
}

function readInflightTurn(): InFlightTurn | null {
    try {
        const raw = window.localStorage.getItem(INFLIGHT_STORAGE_KEY);
        return raw ? (JSON.parse(raw) as InFlightTurn) : null;
    } catch {
        return null;
    }
}

function writeInflightTurn(turn: InFlightTurn): void {
    try {
        window.localStorage.setItem(INFLIGHT_STORAGE_KEY, JSON.stringify(turn));
    } catch {
        // Storage unavailable (private mode/quota); the turn still works,
        // it just is not durable across reloads.
    }
}

function clearInflightTurn(): void {
    try {
        window.localStorage.removeItem(INFLIGHT_STORAGE_KEY);
    } catch {
        // Storage unavailable; nothing durable to clear.
    }
}

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
        } else {
            window.localStorage.removeItem(draftKey(conversationId));
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

const JobSearchChat: React.FC = () => {
    const [conversationId, setConversationId] = React.useState<number | null>(null);
    const [messages, setMessages] = React.useState<ChatMessage[]>([]);
    const [input, setInput] = React.useState('');
    const [pending, setPending] = React.useState(false);
    const [loading, setLoading] = React.useState(true);
    const [initError, setInitError] = React.useState<string | null>(null);
    const [error, setError] = React.useState<string | null>(null);
    const [errorType, setErrorType] = React.useState<string | null>(null);
    const [retrying, setRetrying] = React.useState(false);
    const [preferencesChanged, setPreferencesChanged] = React.useState(false);
    const [prefDismissed, setPrefDismissed] = React.useState(false);

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

    // Resume the user's most recent conversation on load.
    React.useEffect(() => {
        let cancelled = false;
        csrfFetch('/api/agent/conversations/')
            .then(async (res) => {
                if (cancelled) return;
                if (res.status === 404) {
                    // No existing conversation — create one so the user can start chatting.
                    try {
                        const createRes = await csrfFetch('/api/agent/conversations/', {
                            method: 'POST',
                            body: JSON.stringify({create_new: true}),
                        });
                        if (cancelled) return;
                        if (!createRes.ok) throw new Error('create-failed');
                        const createData = (await createRes.json()) as Conversation;
                        if (cancelled) return;
                        setConversationId(createData.id);
                        setMessages(createData.messages);
                        reconcileDurableState(createData);
                        setLoading(false);
                        composerRef.current?.focus();
                    } catch {
                        if (cancelled) return;
                        setInitError('Could not start a conversation. Please try again.');
                        setLoading(false);
                    }
                    return;
                }
                if (!res.ok) {
                    throw new Error(`Resume failed (${res.status})`);
                }
                const data = (await res.json()) as Conversation;
                if (cancelled) return;
                setConversationId(data.id);
                setMessages(data.messages);
                setPreferencesChanged(data.preferences_changed);
                reconcileDurableState(data);
                setLoading(false);
                composerRef.current?.focus();
            })
            .catch(() => {
                if (cancelled) return;
                setInitError('Could not load your conversation. Please refresh.');
                setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    // Announce new assistant content to assistive tech.
    React.useEffect(() => {
        if (historyRef.current) {
            const last = historyRef.current.lastElementChild;
            if (last instanceof HTMLElement) {
                last.setAttribute('aria-live', 'polite');
            }
        }
    }, [messages.length]);

    const isReady = conversationId !== null && !pending && !loading;

    // Reconcile durable client state against the server after a (re)load:
    // the server is the single source of truth. A recorded in-flight turn
    // that the server knows adopts the server state (the reply may even have
    // arrived late); one the server never received is restored as an unsent
    // draft. Never overwrites a newer composer draft. Markers are keyed per
    // conversation, so they are dropped on 404 instead of resurrecting a
    // deleted/reset conversation.
    const reconcileDurableState = (conversation: Conversation) => {
        const marker = readInflightTurn();
        if (marker && marker.conversationId === conversation.id) {
            const known = conversation.messages.some(
                (m) => m.role === 'user' && m.idempotency_key === marker.key,
            );
            clearInflightTurn();
            if (!known && !input) {
                // The server never received the request: keep the text as an
                // unsent draft instead of a fake sent message.
                setInput(marker.content);
                writeComposerDraft(conversation.id, marker.content);
            }
        }
        const draft = readComposerDraft(conversation.id);
        if (draft && !input) {
            setInput(draft);
        }
    };

    // Re-fetch the conversation and adopt the server state: used after a 409
    // (another tab is running the turn) and after a client-side stop, so a
    // late completion or failure lands without waiting for a reload.
    const reconcileWithServer = async (targetId: number) => {
        try {
            const res = await csrfFetch(`/api/agent/conversations/${targetId}/`);
            if (res.status === 404) {
                // The conversation was deleted/reset elsewhere; drop any
                // marker for it rather than resurrecting it.
                clearInflightTurn();
                return;
            }
            if (!res.ok) return;
            const data = (await res.json()) as Conversation;
            setMessages(data.messages);
            setPreferencesChanged(data.preferences_changed);
            clearInflightTurn();
        } catch {
            // Network hiccup: the marker stays and the next load reconciles.
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

    const sendTurn = async (content: string, key: string) => {
        if (!conversationId || pendingRef.current) return;
        pendingRef.current = true;
        setPending(true);
        setError(null);
        setErrorType(null);
        setRetrying(false);

        // Record the in-flight turn durably before the request resolves, so a
        // reload/navigation during the wait cannot lose it (issue #458).
        writeInflightTurn({conversationId, content, key});

        // Optimistically reflect the user's turn: appended when this is a new
        // turn; for a retry the persisted user message is already in history
        // and is flipped back to pending in place instead.
        const existingUser = messages.find(
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
            // Retry of a persisted turn: it stays in place (still rendered as
            // failed until the outcome lands); the global pending indicator
            // shows the retry in flight.
        } else {
            setMessages((prev) => [...prev, optimisticUser]);
        }
        const shownUser = existingUser || optimisticUser;

        const controller = new AbortController();
        abortRef.current = controller;

        try {
            const res = await csrfFetch(`/api/agent/conversations/${conversationId}/`, {
                method: 'POST',
                body: JSON.stringify({content, idempotency_key: key}),
                signal: controller.signal,
            });
            if (!res.ok) {
                let serverMsg = `Request failed (${res.status})`;
                let serverType: string | undefined;
                try {
                    const body = (await res.json()) as ApiError;
                    if (body.error) {
                        if (body.error.message) serverMsg = body.error.message;
                        if (body.error.type) serverType = body.error.type;
                    }
                } catch {
                    // non-JSON error; keep the generic message
                }
                if (serverType === 'turn_in_progress') {
                    // Another request is already running this turn. Surface
                    // the honest status and adopt the server state.
                    setError(serverMsg);
                    setErrorType(serverType);
                    await reconcileWithServer(conversationId);
                    return;
                }
                // The server persisted the user turn before failing (stable
                // typed envelope): keep the question visible as a failed turn
                // instead of rolling it back, so retry survives reload.
                setErrorType(serverType || null);
                setError(serverMsg);
                lastSent.current = {content, key};
                setRetrying(true);
                setMessages((prev) => prev.map(
                    (m) => (m === shownUser
                        ? {...m, delivery_state: 'failed'}
                        : m),
                ));
                return;
            }
            const data = (await res.json()) as SubmitResponse;
            // Keep the (optimistic or retried) user turn, mark it completed,
            // and append the persisted assistant reply. Filter by key so the
            // retried turn is replaced exactly once wherever it lives.
            setMessages((prev) => {
                const base = prev.filter(
                    (m) => !(m.role === 'user' && m.idempotency_key === key),
                );
                return [...base, {...shownUser, delivery_state: 'completed'}, data.message];
            });
            if (data.preferences_changed) {
                setPreferencesChanged(true);
                setPrefDismissed(false);
            }
            clearInflightTurn();
            writeComposerDraft(conversationId, '');
            lastSent.current = null;
        } catch (e) {
            if (controller.signal.aborted) {
                // Honest cancel semantics: stopping only stops the client's
                // wait. The server may still complete; the durable marker
                // stands and the state is reconciled on the next GET/reload.
                setError('Stopped waiting. The response may still arrive; retry or reload to check.');
                lastSent.current = {content, key};
                setRetrying(true);
                return;
            }
            // Network-level failure: unknown whether the server received the
            // request. Keep the turn visible and the marker; reconciliation
            // on the next load resolves which state is true.
            setError(e instanceof Error ? e.message : 'Something went wrong.');
            lastSent.current = {content, key};
            setRetrying(true);
            setMessages((prev) => prev.map(
                (m) => (m === shownUser ? {...m, delivery_state: 'failed'} : m),
            ));
        } finally {
            abortRef.current = null;
            pendingRef.current = false;
            setPending(false);
            setInput('');
            // Defer refocus until React flushes the re-render (the submit button
            // becoming disabled would otherwise steal focus back to <body>).
            window.setTimeout(() => composerRef.current?.focus(), 0);
        }
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        const content = input.trim();
        if (!content || !isReady) return;
        await sendTurn(content, newId());
    };

    const handleRetryMessage = async (message: ChatMessage) => {
        if (!message.idempotency_key) return;
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

    const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            const content = input.trim();
            if (content && isReady) {
                sendTurn(content, newId());
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
            composerRef.current?.focus();
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
            setConversationId(data.id);
            setMessages([]);
            setPreferencesChanged(false);
            setPrefDismissed(false);
            setError(null);
            clearInflightTurn();
            writeComposerDraft(conversationId, '');
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
            clearInflightTurn();
            writeComposerDraft(conversationId, '');
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
                            disabled={!conversationId || !messages.length} aria-label="Export conversation">Export</button>
                    <button type="button" className="btn btn-outline-light" onClick={handleReset}
                            disabled={!conversationId} aria-label="Reset conversation">Reset</button>
                    <button type="button" className="btn btn-outline-danger" onClick={handleDelete}
                            disabled={!conversationId} aria-label="Delete conversation">Delete</button>
                </div>
            </div>

            <div className="card-body d-flex flex-column" style={{minHeight: 0}}>
                <div id="job-search-data-note" className="alert alert-info" role="note">
                    The assistant is automated and can be wrong. Check important details yourself. Your messages
                    and preference updates are saved to your account; use Export, Reset, or Delete above to manage them.
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

                <div className="position-relative d-flex flex-column flex-grow-1" style={{minHeight: 0}}>
                    <div className="bg-dark border rounded p-3 mb-3 flex-grow-1" style={{minHeight: 0, overflowY: 'auto'}}
                         ref={historyRef} role="log" aria-live="polite" aria-label="Message history" aria-busy={pending}>
                        {messages.length === 0 && !loading && (
                            <div data-testid="empty-history">
                                <p className="text-muted mb-2">
                                    Ask about compensation, work location, funding, or culture to get started.
                                </p>
                                <p className="text-muted small mb-0">
                                    <i className="fa-solid fa-circle-info me-1"></i>
                                    Your matches are shown in the panel above. The assistant searches
                                    across real organizations and job listings to find the best fit.
                                </p>
                            </div>
                        )}
                        {messages.map((m) => (
                            <article key={m.id} aria-label={m.role === 'user' ? 'Your message' : 'Assistant message'}
                                     className={`d-flex ${m.role === 'user' ? 'justify-content-end' : 'justify-content-start'} mb-2`}>
                                <div className={`rounded p-2 ${m.role === 'user' ? 'bg-primary' : 'bg-secondary'}`}
                                     style={{maxWidth: '80%', whiteSpace: 'pre-wrap', wordBreak: 'break-word'}}>
                                    {m.content}
                                    {m.role === 'assistant' && m.results && (
                                        <ResultCards results={m.results} />
                                    )}
                                    {m.role === 'user' && m.delivery_state === 'failed' && (
                                        <div className="mt-1 pt-1 border-top border-light-subtle" data-testid="failed-turn">
                                            <div className="small" role="status">
                                                <i className="fa-solid fa-triangle-exclamation me-1" aria-hidden="true"></i>
                                                Message saved; response unavailable.
                                            </div>
                                            <div className="btn-group btn-group-sm mt-1" role="group" aria-label="Failed turn actions">
                                                <button type="button" className="btn btn-outline-light"
                                                        onClick={() => handleRetryMessage(m)}
                                                        aria-label="Retry response" data-testid="retry-response-button">
                                                    Retry response
                                                </button>
                                                <button type="button" className="btn btn-outline-light"
                                                        onClick={() => handleEditAsNew(m)}
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
                        <button type="button" className="btn btn-sm btn-primary position-absolute bottom-0 end-0 mb-4 me-2"
                                onClick={() => scrollToLatest('auto')} aria-label="Jump to latest message"
                                data-testid="jump-to-latest">
                            New messages · Jump to latest
                        </button>
                    )}
                </div>

                <div className="flex-shrink-0" style={{paddingBottom: 'calc(0.25rem + env(safe-area-inset-bottom))'}}>
                    {error && (
                        <div className="alert alert-danger d-flex justify-content-between align-items-center"
                             role="alert" data-testid="chat-error" data-error-type={errorType || undefined}>
                            <span className="flex-grow-1 me-2">{error}</span>
                            {retrying && (
                                <button type="button" className="btn btn-sm btn-outline-danger ms-2 flex-shrink-0 text-nowrap"
                                        onClick={handleRetry} data-testid="retry-button">
                                    Retry
                                </button>
                            )}
                        </div>
                    )}

                    <form onSubmit={handleSubmit} aria-busy={pending}>
                        <div className="input-group">
                            <textarea
                                ref={composerRef}
                                className="form-control"
                                placeholder="Type your message…"
                                aria-label="Message"
                                value={input}
                                onChange={(e) => {
                                    setInput(e.target.value);
                                    writeComposerDraft(conversationId, e.target.value);
                                }}
                                onKeyDown={handleKeyDown}
                                disabled={!conversationId || pending}
                                autoComplete="off"
                                rows={1}
                                style={{resize: 'none', overflowY: 'hidden'}}
                            />
                            <button type="submit" className="btn btn-primary" disabled={!conversationId || pending || !input.trim()}
                                    aria-label="Send message">
                                <i className="fa-solid fa-paper-plane"></i>
                            </button>
                            {pending && (
                                <button type="button" className="btn btn-outline-secondary" onClick={handleStopWaiting}
                                        aria-label="Stop waiting for response" data-testid="stop-button">
                                    Stop
                                </button>
                            )}
                        </div>
                    </form>
                </div>

                {/* Screen-reader-only live region for pending/error transitions. */}
                <div ref={statusRef} className="visually-hidden" role="status" aria-live="assertive">
                    {pending ? 'Sending message.' : ''}
                    {!pending && error ? 'Your message could not be sent.' : ''}
                </div>
            </div>
        </section>
    );
};

export default JobSearchChat;

document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('job-search-chat');
    if (container) {
        const root = createRoot(container);
        root.render(<JobSearchChat/>);
    }
});
