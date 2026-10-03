// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

import {preferencePathLabel, preferenceValueLabel} from './priorities/format';
import {MATCH_CAP} from './priorities/api';
import {patchFitsEditor} from './priorities/patch';
import {prioritiesSurface} from './priorities/surface';
import {
    describeWorkspaceContext,
    getWorkspaceSnapshot,
    setWorkspaceConversation,
    setPrioritiesEditorOpen,
    setPrioritiesRevision,
} from './workspace/store';
import {Transcript} from './chat/Transcript';
import {Composer} from './chat/Composer';
import {JumpToLatest} from './chat/JumpToLatest';
import {useTranscriptScroll} from './chat/useTranscriptScroll';
import {useAccountGate} from './chat/useAccountGate';
import {ConversationMenu} from './chat/ConversationMenu';
import {
    csrfFetch,
    newId,
    PRE_PERSISTENCE_ERROR_TYPES,
    POST_PERSISTENCE_ERROR_TYPES,
} from './chat/transport';
import {
    clearInflightTurn,
    clearInflightTurns,
    clearPendingDraft,
    LAST_ACCOUNT_KEY,
    readComposerDraft,
    readComposerDraftTs,
    readInflightTurns,
    readPendingDraft,
    reconcileAccountKey,
    writeComposerDraft,
    writeInflightTurn,
    writePendingDraft,
} from './chat/storage';
import type {InFlightTurn} from './chat/storage';
import {hasResults} from './chat/ResultCards';
import {
    AssistantStatusNotice,
    isGatedState,
    PreferenceChangeNotice,
    PreferenceProposalNotice,
} from './chat/notices';
import type {
    ApiError,
    AssistantState,
    AssistantStatus,
    AvailabilityPayload,
    ChatMessage,
    Conversation,
    JobResult,
    OrganizationResult,
    PreferenceChange,
    PreferenceProposal,
    PreferenceProposalToken,
    PreferenceUndoState,
    PreferenceUndoToken,
    StructuredResults,
    SubmitResponse,
} from './chat/types';


export {preferencePathLabel, preferenceValueLabel};
export {PreferenceChangeNotice, PreferenceProposalNotice, reconcileAccountKey, LAST_ACCOUNT_KEY};
export type {
    AssistantState,
    AssistantStatus,
    AvailabilityPayload,
    ChatMessage,
    JobResult,
    OrganizationResult,
    PreferenceChange,
    PreferenceProposal,
    PreferenceProposalToken,
    PreferenceUndoState,
    PreferenceUndoToken,
    StructuredResults,
};


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
    // Workspace only: true when the user opened the panel on this page. A
    // panel restored on load or Back must not take focus from the page
    // (issue #479). Ignored outside the workspace, which always autofocuses.
    autoFocusComposer?: boolean;
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
    const {accountPending, effectiveAuthenticated, setEffectiveAuthenticated, workspaceAccount} = useAccountGate({
        isAuthenticated,
        accountKey,
        workspaceMode: props.workspaceMode,
        onPurge: () => resetForPurge(),
    });

    const [conversationId, setConversationId] = React.useState<number | null>(null);
    const [messages, setMessages] = React.useState<ChatMessage[]>([]);
    const [input, setInput] = React.useState('');
    // "Answered about …" notes keyed by assistant message id (issue #479).
    const [staleNotes, setStaleNotes] = React.useState<Record<number, string>>({});
    const [pending, setPending] = React.useState(false);
    const [loading, setLoading] = React.useState(true);
    const [initError, setInitErrorState] = React.useState<string | null>(null);
    const [error, setError] = React.useState<string | null>(null);
    const [errorType, setErrorType] = React.useState<string | null>(null);
    const [retrying, setRetrying] = React.useState(false);
    // Idempotency key of the turn currently being retried (issue #458 round 2):
    // flips that message's failure treatment to the in-progress amber state.
    const [retryKey, setRetryKey] = React.useState<string | null>(null);
    // Data note is collapsed by default so the conversation owns the viewport;
    // the details stay available to sighted users via the toggle and to screen
    // readers via the visually-hidden fallback.
    const [preferencesChanged, setPreferencesChanged] = React.useState(false);
    const [prefDismissed, setPrefDismissed] = React.useState(false);
    // Issue #466: the latest applied field-level diff and its undo token.
    // Client-held only; the server re-validates the token on undo.
    const [prefChanges, setPrefChanges] = React.useState<PreferenceChange[] | null>(null);
    const [prefUndoToken, setPrefUndoToken] = React.useState<PreferenceUndoToken | null>(null);
    const [prefUndoState, setPrefUndoState] = React.useState<PreferenceUndoState>('idle');
    const [prefUndoError, setPrefUndoError] = React.useState<string | null>(null);
    // Server error type of the failed undo (issue #466 review): tracked
    // separately from the message so only `preference_stale` renders the
    // stale-only review action; network/5xx failures stay retry-oriented.
    const [prefUndoErrorType, setPrefUndoErrorType] = React.useState<string | null>(null);
    // Issue #466 review: the latest read-only preference proposal awaiting
    // the user's Apply/Dismiss decision. Client-held token; the server
    // re-validates and revision-guards it on apply.
    const [prefProposal, setPrefProposal] = React.useState<PreferenceProposal | null>(null);
    const [prefProposalState, setPrefProposalState] = React.useState<'idle' | 'pending' | 'error'>('idle');
    const [prefProposalError, setPrefProposalError] = React.useState<string | null>(null);
    const [prefProposalErrorType, setPrefProposalErrorType] = React.useState<string | null>(null);
    // Confirmation for an applied this-search-only proposal (never saved).
    const [prefSearchApplied, setPrefSearchApplied] = React.useState<number | null>(null);
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

    const composerRef = React.useRef<HTMLTextAreaElement>(null);
    const initErrorActionRef = React.useRef<HTMLButtonElement>(null);
    const refocusInitErrorRef = React.useRef(false);
    // The error state unmounts the composer; if it held focus, hand focus to
    // the alert's action once that commits so it never falls back to <body>.
    const setInitError = (message: string | null) => {
        if (message && composerRef.current && document.activeElement === composerRef.current) {
            refocusInitErrorRef.current = true;
        }
        setInitErrorState(message);
    };
    React.useEffect(() => {
        if (initError && refocusInitErrorRef.current) {
            refocusInitErrorRef.current = false;
            initErrorActionRef.current?.focus();
        }
    }, [initError]);
    const headingRef = React.useRef<HTMLHeadingElement>(null);
    // After load the focus belongs in the chat. The sheet keeps the keyboard
    // down, so it lands on the Conversation heading instead of the composer.
    // Focus that the reader already moved inside the chat (or into a dialog
    // opened from it) while the timer waited is theirs; the load never takes it.
    const focusAfterLoad = () => {
        if (!autoFocusRef.current) return;
        window.setTimeout(() => {
            const active = document.activeElement;
            const claimed = active instanceof HTMLElement && active !== document.body
                && active !== headingRef.current && active !== composerRef.current
                && (!!cardRef.current?.contains(active) || !!active.closest('[role="dialog"]'));
            if (claimed) return;
            if (props.workspaceMode === 'sheet') headingRef.current?.focus({preventScroll: true});
            else composerRef.current?.focus();
        }, 0);
    };
    // After New conversation or Delete, the sheet keeps the keyboard down by
    // focusing the empty state's call to action; elsewhere the composer takes
    // focus, falling back to that call to action while the composer is disabled.
    const focusAfterHistoryAction = () => {
        window.setTimeout(() => {
            const composer = composerRef.current;
            if (props.workspaceMode !== 'sheet' && composer && !composer.disabled) {
                composer.focus();
                return;
            }
            const cta = cardRef.current?.querySelector<HTMLElement>('[data-testid="empty-history-cta"]');
            (cta ?? headingRef.current)?.focus({preventScroll: true});
        }, 0);
    };
    const autoFocusRef = React.useRef(true);
    autoFocusRef.current = props.workspaceMode === undefined || !!props.autoFocusComposer;
    // Shared floor for the measured card height; must stay in sync with the
    // `20rem` inline minHeight below (16px rem * 20) so the two cannot drift.
    const MIN_CARD_PX = 320;
    const MIN_TRANSCRIPT_PX = 128;
    const MIN_STACK_PX = 96;
    const MIN_NOTICES_PX = 64;
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

    const cardRef = React.useRef<HTMLElement>(null);
    const [panelScroll, setPanelScroll] = React.useState(false);
    const captureAnchorRef = React.useRef<() => void>(() => undefined);
    const [cardHeight, setCardHeight] = React.useState<number | null>(null);
    // Room between the header and the composer band: inline panels scroll
    // inside it instead of covering the composer.
    const [stackMax, setStackMax] = React.useState<number | null>(null);
    // rAF bookkeeping so resize/orientation/keyboard bursts coalesce into at most
    // one measure per frame instead of thrashing layout on every event.
    // Footer notices never take more than 40% of the card (or, when the panel
    // scrolls, of the panel's visible height), not of the viewport.
    const [noticesMax, setNoticesMax] = React.useState<number | null>(null);
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
        // Inside the assistant panel add back the panel's own scroll offset so the
        // height does not depend on how far the reader has scrolled it (which would
        // oversize the card and nest a second scrollbar, issue #483).
        const panelBody = card.closest<HTMLElement>('.assistant-panel-body');
        const top = Math.max(0, card.getBoundingClientRect().top + (panelBody ? panelBody.scrollTop : 0));
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
        // Inside the assistant panel, keep the transcript at least 8rem tall: the
        // card grows and the panel scrolls rather than the composer painting over
        // the history. On the page the history alone yields (no page scroll).
        const log = historyRef.current;
        const floor = log && card.closest('.assistant-panel-body') ? card.offsetHeight - log.offsetHeight + MIN_TRANSCRIPT_PX : 0;
        // When the panel is too short to give the transcript a usable height the
        // panel body becomes the one scroller and the transcript grows to its
        // content, instead of nesting two scrollbars (issue #483).
        const headerBottom = card.querySelector('.card-header')?.getBoundingClientRect().bottom ?? 0;
        const formTop = card.querySelector('form')?.getBoundingClientRect().top ?? 0;
        setStackMax(Math.max(MIN_STACK_PX, formTop - headerBottom - 12));
        const scrollsPanel = floor > 0 && computed < floor;
        // Record where the reader is before the scroll owner changes under them.
        if (scrollsPanel !== (card.getAttribute('data-scroll-owner') === 'panel')) captureAnchorRef.current();
        setPanelScroll(scrollsPanel);
        // Keep keyboard/programmatic scroll-into-view clear of the pinned header
        // and composer band while the panel body is the scroller.
        if (panelBody) {
            // The footer is not rendered while the init error shows.
            const headerHeight = card.querySelector('.card-header')?.getBoundingClientRect().height ?? 0;
            const footerHeight = card.querySelector<HTMLElement>('.chat-footer')?.offsetHeight ?? 0;
            panelBody.style.scrollPaddingTop = scrollsPanel ? `${Math.round(headerHeight)}px` : '';
            panelBody.style.scrollPaddingBottom = scrollsPanel ? `${footerHeight + 8}px` : '';
        }
        const resolvedHeight = Math.max(computed, MIN_CARD_PX, floor);
        const noticesBase = scrollsPanel && panelBody && panelBody.clientHeight > 0 ? panelBody.clientHeight : resolvedHeight;
        setNoticesMax(Math.max(MIN_NOTICES_PX, Math.round(noticesBase * 0.4)));
        setCardHeight(resolvedHeight);
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
        let mutationObserver: MutationObserver | null = null;
        if (typeof ResizeObserver !== 'undefined' && cardRef.current?.parentElement) {
            observer = new ResizeObserver(scheduleMeasure);
            observer.observe(cardRef.current.parentElement);
            // Inline confirmations grow the header; re-measure so the
            // transcript floor / single-scroller decision follows (issue #483).
            const header = cardRef.current.querySelector('.card-header');
            if (header) observer.observe(header);
            // The priorities block above the chat (assistant panel) resizes as
            // its steps change; the card height depends on its offset.
            // The block can mount after the chat (it waits for sign-in
            // confirmation), so keep looking until it appears.
            const body = cardRef.current.closest('.assistant-panel-body');
            let watched: Element | null = null;
            const watch = () => {
                const priorities = body?.querySelector('[data-testid="priorities-sidebar"]') ?? null;
                if (priorities && priorities !== watched) {
                    if (watched) observer?.unobserve(watched);
                    watched = priorities;
                    observer?.observe(priorities);
                }
            };
            watch();
            if (body && typeof MutationObserver !== 'undefined') {
                mutationObserver = new MutationObserver(watch);
                mutationObserver.observe(body, {childList: true, subtree: true});
            }
        }
        return () => {
            mutationObserver?.disconnect();
            if (rafIdRef.current !== null) {
                window.cancelAnimationFrame(rafIdRef.current);
            }
            window.removeEventListener('resize', scheduleMeasure);
            window.visualViewport?.removeEventListener('resize', scheduleMeasure);
            observer?.disconnect();
        };
    }, [scheduleMeasure]);

    // The footer unmounts while the init error shows and a new one mounts when
    // the conversation starts, so it is observed per mount, not once.
    React.useEffect(() => {
        const footer = cardRef.current?.querySelector('.chat-footer');
        if (!footer || typeof ResizeObserver === 'undefined') return undefined;
        const footerObserver = new ResizeObserver(scheduleMeasure);
        footerObserver.observe(footer);
        scheduleMeasure();
        return () => footerObserver.disconnect();
    }, [initError, scheduleMeasure]);

    const chatCardStyle = React.useMemo<React.CSSProperties>(() => {
        const vars = {
            ...(stackMax === null ? {} : {'--chat-stack-max': `${stackMax}px`}),
            ...(noticesMax === null ? {} : {'--chat-notices-max': `${noticesMax}px`}),
        } as React.CSSProperties;
        if (panelScroll) return {...vars, height: 'auto'};
        if (cardHeight !== null) {
            return {...vars, height: `${cardHeight}px`, minHeight: '20rem'};
        }
        return {...vars, minHeight: '20rem'};
    }, [cardHeight, panelScroll, stackMax, noticesMax]);

    const {historyRef, showJumpToLatest, unreadCount, scrollToLatest, followNextAppend, captureAnchor} = useTranscriptScroll({
        messagesLength: messages.length,
        assistantCount: messages.filter((m) => m.role === 'assistant').length,
        scrollOwner: panelScroll ? 'panel' : 'transcript',
        pending,
        loading,
    });
    captureAnchorRef.current = captureAnchor;

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
        if (accountPending) {
            return undefined;
        }
        if (!accountKey && props.workspaceMode !== undefined) {
            // Reconcile the now-known account before any stored draft is read.
            const authenticated = workspaceAccount.status === 'authenticated';
            if (authenticated) {
                reconcileAccountKey(workspaceAccount.key);
            }
            if (authenticated !== effectiveAuthenticated) {
                setEffectiveAuthenticated(authenticated);
                return undefined;
            }
        }
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
                        focusAfterLoad();
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
                focusAfterLoad();
            })
            .catch(() => {
                if (stale()) return;
                setInitError('We couldn’t restore your previous conversation.');
                setLoading(false);
            });
        return () => {
            cancelled = true;
            if (resumeAbortRef.current === controller) {
                resumeAbortRef.current = null;
            }
        };
    }, [effectiveAuthenticated, purgeGeneration, accountPending, workspaceAccount.status]);

    // Mirror the active conversation id into the shared workspace store
    // (issue #479) — one effect covers resume, create, reset and delete.
    React.useEffect(() => {
        setWorkspaceConversation(conversationId);
    }, [conversationId]);

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
        setStaleNotes({});
        setConversationId(null);
        conversationIdRef.current = null;
        setInput('');
        setError(null);
        setErrorType(null);
        setRetrying(false);
        setInitError(null);
        setPreferencesChanged(false);
        setPrefDismissed(false);
        setPrefChanges(null);
        setPrefUndoToken(null);
        setPrefUndoState('idle');
        setPrefUndoError(null);
        setPrefUndoErrorType(null);
        setPrefProposal(null);
        setPrefProposalState('idle');
        setPrefProposalError(null);
        setPrefProposalErrorType(null);
        setPrefSearchApplied(null);
        setPrioritiesEditorOpen(null);
        if (effectiveAuthenticated) {
            // Force the resume effect to re-run so the (possibly different)
            // account's own conversation loads fresh — never the stale
            // conversation just cleared above.
            setPurgeGeneration((g) => g + 1);
        }
    };

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
        // Context this turn was sent under (issue #479): compared with the
        // live context when the reply lands.
        const sentContextLabel = describeWorkspaceContext(getWorkspaceSnapshot().context);
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
            followNextAppend();
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
            // The reply is server history and is always appended; if the page
            // context moved on meanwhile it is labelled with the page the
            // question was asked from, without touching the context strip or
            // the store. Page context is not sent to the model yet (#484), so
            // the note must not claim the reply was about that entity.
            if (sentContextLabel
                && sentContextLabel !== describeWorkspaceContext(getWorkspaceSnapshot().context)) {
                const answered = sentContextLabel.replace(/^(About|Comparing) /, '');
                setStaleNotes((prev) => ({...prev, [data.message.id]: `Asked while viewing ${answered}`}));
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
            // Issue #466 review: adopt the read-only proposal when the server
            // surfaced one; the user applies or dismisses it explicitly.
            if (data.preference_proposal) {
                setPrefProposal(data.preference_proposal);
                setPrefProposalState('idle');
                setPrefProposalError(null);
                setPrefProposalErrorType(null);
                setPrefSearchApplied(null);
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

    const handleJumpToLatest = () => {
        scrollToLatest('auto');
        // Focus the newest turn, not the composer: that would raise the phone
        // keyboard, and the composer is disabled while a reply is pending.
        const articles = historyRef.current?.querySelectorAll<HTMLElement>('article');
        (articles && articles.length ? articles[articles.length - 1] : historyRef.current)?.focus({preventScroll: true});
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

    const handleComposerChange = (value: string) => {
        setInput(value);
        if (effectiveAuthenticated) {
            writeComposerDraft(conversationId, value);
        } else {
            // Sensitive draft text (issue #465 AC-8): kept client-side only,
            // in a pre-conversation slot since a signed-out visitor has no
            // conversation id to key it against. Never sent anywhere,
            // including the sign-in `next`.
            writePendingDraft(value);
        }
        // Explicit discard (issue #458 r2): emptying the composer throws the
        // surfaced recovery draft away for good — other unsent turns stay
        // recoverable in storage.
        if (value === '') {
            clearSurfacedDraft();
        }
    };

    const handleSend = () => {
        const content = input.trim();
        if (content && isReady && !composerGated) {
            // Explicit send resolves the surfaced recovery draft (see
            // submitTurn).
            void submitTurn(content);
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

    const handleExport = async (): Promise<string | null> => {
        if (!conversationId) return null;
        try {
            const res = await csrfFetch(`/api/agent/conversations/${conversationId}/export/`);
            if (!res.ok) throw new Error('export-failed');
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `job-search-${conversationId}.json`;
            a.click();
            URL.revokeObjectURL(a.href);
            return null;
        } catch {
            return 'Could not export your conversation.';
        }
    };

    const handleReset = async (): Promise<string | null> => {
        if (!conversationId) return null;  // # pragma: no cover - menu item is disabled without a conversation
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
            focusAfterHistoryAction();
            return null;
        } catch {
            return 'Could not reset the conversation.';
        }
    };

    const handleDelete = async (): Promise<string | null> => {
        if (!conversationId) return null;
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
            focusAfterHistoryAction();
            return null;
        } catch {
            return 'Could not delete the conversation.';
        }
    };

    // Issue #466: apply the client-held undo token. The server re-validates
    // it as an ordinary owner-scoped patch under its revision precondition;
    // a 409 preference_stale means an intervening edit and undoes nothing.
    const handleUndoPreferenceChange = async () => {
        if (!prefUndoToken || prefUndoState === 'pending') return;
        setPrefUndoState('pending');
        setPrefUndoError(null);
        setPrefUndoErrorType(null);
        const epoch = purgeEpochRef.current;
        const purged = () => purgeEpochRef.current !== epoch;
        try {
            const res = await csrfFetch('/api/agent/preferences/undo/', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({undo: prefUndoToken}),
            });
            if (purged()) return;
            if (res.ok) {
                const undone = await res.json().catch(() => null);
                if (purged()) return;
                if (typeof undone?.revision === 'number') setPrioritiesRevision(undone.revision);
                setPrefUndoState('done');
                setPrefChanges(null);
                setPrefUndoToken(null);
                setPreferencesChanged(false);
                return;
            }
            let serverType: string | null = null;
            let serverMsg = '';
            try {
                const parsed = (await res.json()) as ApiError;
                serverType = parsed?.error?.type || null;
                serverMsg = parsed?.error?.message || '';
            } catch { /* non-JSON body: fall through to generic copy */ }
            if (purged()) return;
            setPrefUndoState('error');
            setPrefUndoErrorType(serverType);
            setPrefUndoError(
                serverType === 'preference_stale'
                    ? (serverMsg || 'Your preferences changed since this update. Please review the current preferences before undoing.')
                    : (serverMsg || 'The undo request is no longer valid. Please try again.'),
            );
        } catch {
            if (purged()) return;
            setPrefUndoState('error');
            setPrefUndoErrorType(null);
            setPrefUndoError('Could not undo the preference update. Please check your connection and try again.');
        }
    };

    // Issue #466 review: apply or dismiss the read-only proposal. Only an
    // explicit Apply persists (scope=account) or runs one unsaved search
    // (scope=search); Dismiss discards the client-held token.
    const handlePreferenceProposalDecision = async (
        decision: 'apply' | 'dismiss', scopeOverride?: 'search',
    ) => {
        if (!prefProposal || prefProposalState === 'pending') return;
        if (decision === 'dismiss') {
            setPrefProposal(null);
            setPrefProposalState('idle');
            setPrefProposalError(null);
            setPrefProposalErrorType(null);
            return;
        }
        setPrefProposalState('pending');
        setPrefProposalError(null);
        setPrefProposalErrorType(null);
        const epoch = purgeEpochRef.current;
        const purged = () => purgeEpochRef.current !== epoch;
        try {
            const res = await csrfFetch('/api/agent/preferences/apply/', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    proposal: scopeOverride ? {...prefProposal.token, scope: scopeOverride} : prefProposal.token,
                    decision,
                }),
            });
            if (purged()) return;
            if (res.ok) {
                const data = await res.json();
                if (purged()) return;
                if (data.scope === 'search') {
                    // This-search-only: never saved; show the confirmation
                    // with the number of matches the temporary filter found.
                    const matchCount = data.matches?.job_matches?.length || 0;
                    setPrefSearchApplied(matchCount);
                } else {
                    setPrefChanges(data.changes || []);
                    setPrefUndoToken(data.undo || null);
                    setPrefUndoState('idle');
                    setPrefUndoError(null);
                    setPrefUndoErrorType(null);
                    setPreferencesChanged(true);
                    if (typeof data.revision === 'number') setPrioritiesRevision(data.revision);
                }
                setPrefProposal(null);
                setPrefProposalState('idle');
                setPrefDismissed(false);
                return;
            }
            let serverType: string | null = null;
            let serverMsg = '';
            try {
                const parsed = (await res.json()) as ApiError;
                serverType = parsed?.error?.type || null;
                serverMsg = parsed?.error?.message || '';
            } catch { /* non-JSON body: fall through to generic copy */ }
            if (purged()) return;
            setPrefProposalState('error');
            setPrefProposalErrorType(serverType);
            setPrefProposalError(
                serverType === 'preference_stale'
                    ? (serverMsg || 'Your preferences changed since this proposal was prepared. Please review the current preferences before applying.')
                    : (serverMsg || 'The proposal could not be applied. Please try again.'),
            );
        } catch {
            if (purged()) return;
            setPrefProposalState('error');
            setPrefProposalErrorType(null);
            setPrefProposalError('Could not apply the preference proposal. Please check your connection and try again.');
        }
    };

    const revealingPrefs = preferencesChanged && !prefDismissed;
    // Issue #466: the detailed diff notice supersedes the plain banner when
    // the turn surfaced a field-level diff; the plain banner remains the
    // fallback for legacy providers (and post-reload turns, where the diff
    // is not persisted in the message history).
    const revealingPrefChanges = !prefDismissed && (prefChanges !== null || prefUndoState === 'done');

    return (
        <section className="card bg-dark d-flex flex-column" data-testid="job-search-chat"
                 aria-labelledby="job-search-chat-title"
                 ref={cardRef}
                 data-scroll-owner={panelScroll ? 'panel' : 'transcript'}
                 style={chatCardStyle}>
            <div className="card-header d-flex flex-wrap align-items-center">
                <ConversationMenu
                    title={<h2 id="job-search-chat-title" className="h6 mb-0" tabIndex={-1} ref={headingRef}>Conversation</h2>}
                    hasConversation={!!conversationId}
                    hasMessages={messages.length > 0}
                    pending={pending}
                    onExport={handleExport}
                    onNewConversation={handleReset}
                    onDeleteConversation={handleDelete}
                />
            </div>

            <div className="card-body d-flex flex-column" style={{minHeight: 0}}>
                {prefProposal && !prefDismissed && (
                    <PreferenceProposalNotice
                        proposal={prefProposal}
                        state={prefProposalState}
                        error={prefProposalError}
                        errorType={prefProposalErrorType}
                        onDecision={handlePreferenceProposalDecision}
                        onReview={() => {
                            // Stale-conflict recovery: reopen the proposal in the
                            // inline editor, to re-review against the latest (issue #480).
                            setPrioritiesEditorOpen(prioritiesSurface(), prefProposal.token.patch);
                        }}
                        onEdit={() => {
                            setPrioritiesEditorOpen(prioritiesSurface(), prefProposal.token.patch);
                            // Keep the proposal while part of it cannot be shown in the editor.
                            if (patchFitsEditor(prefProposal.token.patch)) setPrefProposal(null);
                        }}
                        onSearchOnly={() => handlePreferenceProposalDecision('apply', 'search')}
                    />
                )}

                {prefSearchApplied !== null && !prefDismissed && !prefProposal && (
                    <div className="alert alert-info pref-change-notice" role="status"
                         aria-label="Temporary search filter applied" data-testid="preference-search-applied">
                        <div className="pref-change-header">
                            <span className="pref-change-summary">
                                <i className="fa-solid fa-filter me-1" aria-hidden="true"></i>
                                Applied to this search only — not saved to your preferences
                                {prefSearchApplied > 0 ? ` (${prefSearchApplied}${prefSearchApplied >= MATCH_CAP ? '+' : ''} matches)` : ''}.
                            </span>
                            <button type="button" className="pref-change-dismiss" aria-label="Dismiss search filter notice"
                                    onClick={() => {
                                        setPrefSearchApplied(null);
                                        setPrefDismissed(true);
                                    }}>
                                <i className="fa-solid fa-xmark" aria-hidden="true"></i>
                            </button>
                        </div>
                    </div>
                )}

                {revealingPrefChanges && (
                    <PreferenceChangeNotice
                        changes={prefChanges || []}
                        undoState={prefUndoState}
                        undoError={prefUndoError}
                        undoErrorType={prefUndoErrorType}
                        onUndo={handleUndoPreferenceChange}
                        onDismiss={() => {
                            setPrefDismissed(true);
                            setPrefChanges(null);
                            setPrefUndoToken(null);
                            setPrefUndoState('idle');
                            setPrefUndoError(null);
                            setPrefUndoErrorType(null);
                        }}
                        onReview={() => {
                            // Stale-conflict recovery: review the current
                            // priorities in the inline editor (issue #480).
                            setPrioritiesEditorOpen(prioritiesSurface());
                        }}
                    />
                )}

                {!revealingPrefChanges && revealingPrefs && (
                    <div className="alert alert-success chat-pref-alert d-flex justify-content-between align-items-center"
                         role="status" aria-label="Preference update" aria-describedby="preference-update-help">
                        <span>
                            <i className="fa-solid fa-circle-check me-1"></i>
                            Preferences updated.
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
                            <button type="button" ref={initErrorActionRef} className="btn btn-sm btn-primary"
                                    onClick={handleCreateConversation}>
                                Start a conversation
                            </button>
                        </div>
                    </div>
                )}

                {!initError && (
                <div className="chat-transcript-wrap">
                <Transcript
                    historyRef={historyRef}
                    messages={messages}
                    staleNotes={staleNotes}
                    lastAssistantId={lastAssistantId}
                    availability={availability}
                    retryKey={retryKey}
                    pending={pending}
                    loading={loading}
                    authenticated={effectiveAuthenticated}
                    workspaceMode={props.workspaceMode}
                    scrollOwner={panelScroll ? 'panel' : 'transcript'}
                    onAskFirstQuestion={() => composerRef.current?.focus()}
                    onCheckResponse={(m) => void handleCheckResponse(m)}
                    onRetryMessage={(m) => handleRetryMessage(m)}
                    onEditAsNew={handleEditAsNew}
                />
                </div>
                )}

                {!initError && (
                <div className="chat-footer flex-shrink-0">
                    {showJumpToLatest && (
                        <JumpToLatest unreadCount={unreadCount} onJump={handleJumpToLatest}/>
                    )}
                    <div className="chat-footer-notices">
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
                    </div>

                    <Composer
                        textareaRef={composerRef}
                        value={input}
                        pending={pending}
                        authenticated={effectiveAuthenticated}
                        textareaDisabled={effectiveAuthenticated ? ((createOnMount && !conversationId) || pending || composerGated) : pending}
                        sendDisabled={!effectiveAuthenticated || (createOnMount && !conversationId) || pending || composerGated || !input.trim()}
                        onChange={handleComposerChange}
                        onSubmit={handleSubmit}
                        onEnter={handleSend}
                        onStop={handleStopWaiting}
                    />
                </div>
                )}

                <div className="visually-hidden" role="status" aria-live="polite" aria-atomic="true"
                     data-testid="new-messages-status">
                    {unreadCount > 0 ? `${unreadCount} new ${unreadCount === 1 ? 'message' : 'messages'}` : ''}
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
