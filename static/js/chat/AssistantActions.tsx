// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Explicit, user-confirmed assistant actions (issue #484). The server returns
// a small allowlist of typed actions (ids and enums only); this module renders
// each as a button and builds every URL itself from those enums and ids. A
// click re-checks that the page is still the one the reply answered, so a
// late reply can never change a view the user has since left. Applying an
// action never moves focus into the chat; focus is only restored (to a control
// in this reply) when the control the user was on disappears.

import * as React from 'react';
import {
    ApiFailure, GENERIC_ERROR_MESSAGE, Proposal, UndoToken, applyProposal, proposePriorities, undoApplied,
} from '../priorities/api';
import ReviewChanges from '../priorities/ReviewChanges';
import {
    applyWorkspaceFilters, getWorkspaceSnapshot, openWorkspaceCompany, sameContext, setPrioritiesRevision,
    subscribeWorkspace,
} from '../workspace/store';
import {RtoPolicyCode, WorkspaceContext, WorkspaceFilters} from '../workspace/types';

export type AssistantAction =
    | {type: 'open_company'; organization_id: number}
    | {type: 'propose_filters'; target: 'rankings'; filters: {rto_policy?: RtoPolicyCode; accelerated_vesting?: true}};

export interface TurnActions {
    actions: AssistantAction[];
    // `context.revision` the server echoed for the turn that produced them.
    revision: number;
    // Display names for ids a reply may open (its result cards and the page
    // the question was asked from); an unknown id gets a generic label.
    names?: Record<number, string>;
    // The context the question was asked under: lets the reply recognise that
    // Back undid its own change.
    context?: WorkspaceContext | null;
}

// Revision bumps that land this soon after a click are the click's own
// consequence (the list re-reports its context when React commits).
const SELF_CHANGE_WINDOW_MS = 250;
const NOT_IN_LIST_MESSAGE = "That company isn't in the ranked list you're viewing.";

const RTO_CODES: readonly string[] = ['R', 'H', 'O'];
const RTO_WORDS: Record<RtoPolicyCode, string> = {R: 'remote', H: 'hybrid', O: 'in-office'};
const RTO_PATCH_MODE: Record<RtoPolicyCode, string> = {R: 'remote', H: 'hybrid', O: 'in-office'};
export const STALE_MESSAGE = 'This suggestion was for an earlier view.';
// The server's editor labels, so the review card names a field as the chips do.
const REVIEW_LABELS: Record<string, string> = {
    'work_location.modes': 'Work arrangement',
    'vesting.prefer_accelerated': 'Prefer accelerated vesting',
};
const REVIEW_CHOICES: ReadonlySet<string> = new Set(['work_location.modes']);

const isId = (value: unknown): value is number =>
    typeof value === 'number' && Number.isInteger(value) && value > 0 && value <= 2 ** 31 - 1;

// Re-validates the wire shape. The server already drops bad actions, but this
// is the last gate before a click can navigate, so it is strict too: unknown
// types (including compare_companies, until #490), extra keys and values
// outside the enums are dropped, never rendered.
export function parseActions(raw: unknown): AssistantAction[] {
    if (!Array.isArray(raw)) {
        return [];
    }
    const actions: AssistantAction[] = [];
    for (const item of raw.slice(0, 3)) {
        if (typeof item !== 'object' || item === null) {
            continue;
        }
        const entry = item as Record<string, unknown>;
        const keys = Object.keys(entry);
        if (entry.type === 'open_company'
            && keys.length === 2 && isId(entry.organization_id)) {
            actions.push({type: 'open_company', organization_id: entry.organization_id});
        } else if (entry.type === 'propose_filters'
            && keys.every((key) => ['type', 'target', 'filters'].includes(key))
            && (entry.target === undefined || entry.target === 'rankings')
            && typeof entry.filters === 'object' && entry.filters !== null) {
            const rawFilters = entry.filters as Record<string, unknown>;
            const filterKeys = Object.keys(rawFilters);
            const filters: {rto_policy?: RtoPolicyCode; accelerated_vesting?: true} = {};
            let valid = filterKeys.length > 0;
            for (const key of filterKeys) {
                if (key === 'rto_policy' && typeof rawFilters[key] === 'string' && RTO_CODES.includes(rawFilters[key] as string)) {
                    filters.rto_policy = rawFilters[key] as RtoPolicyCode;
                } else if (key === 'accelerated_vesting' && rawFilters[key] === true) {
                    filters.accelerated_vesting = true;
                } else {
                    valid = false;
                }
            }
            if (valid) {
                actions.push({type: 'propose_filters', target: 'rankings', filters});
            }
        }
    }
    return actions;
}

// Rankings query for a filter action, from enums only. `rto` and
// `accelerated_vesting` are the only keys that can appear.
export function filterQuery(filters: AssistantAction & {type: 'propose_filters'}): string {
    const params = new URLSearchParams();
    if (filters.filters.rto_policy) {
        params.set('rto', filters.filters.rto_policy);
    }
    if (filters.filters.accelerated_vesting) {
        params.set('accelerated_vesting', '1');
    }
    return params.toString();
}

export function companyUrl(organizationId: number): string {
    return `/?company=${organizationId}`;
}

export function filterLabel(action: AssistantAction & {type: 'propose_filters'}, applied = false): string {
    const {rto_policy: rto, accelerated_vesting: vesting} = action.filters;
    if (rto && !vesting) {
        return applied ? `${capitalize(RTO_WORDS[rto])} filter applied` : `Apply ${RTO_WORDS[rto]} filter`;
    }
    if (vesting && !rto) {
        return applied ? 'Early vesting filter applied' : 'Apply early vesting filter';
    }
    return applied ? 'Filters applied' : 'Apply filters';
}

const capitalize = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

// The #480 patch for an applied filter: only the two mapped paths.
export function filtersToPatch(filters: AssistantAction & {type: 'propose_filters'}) {
    const set: Record<string, unknown> = {};
    if (filters.filters.rto_policy) {
        set['work_location.modes'] = [RTO_PATCH_MODE[filters.filters.rto_policy]];
    }
    if (filters.filters.accelerated_vesting) {
        set['vesting.prefer_accelerated'] = true;
    }
    return {set};
}

function toWorkspaceFilters(action: AssistantAction & {type: 'propose_filters'}): WorkspaceFilters {
    const filters: WorkspaceFilters = {};
    if (action.filters.rto_policy) {
        filters.rtoPolicy = action.filters.rto_policy;
    }
    if (action.filters.accelerated_vesting) {
        filters.acceleratedVesting = true;
    }
    return filters;
}


type UndoState = 'idle' | 'pending' | 'done' | 'error';

type RequirementState =
    | {status: 'idle'}
    | {status: 'proposing'}
    | {status: 'review'; proposal: Proposal; applying: boolean; error: string | null; stale: boolean}
    | {status: 'saved'; undo: UndoToken | null; undoState: UndoState; undoError: string | null}
    | {status: 'error'; message: string};

type FocusKind = 'save' | 'saved' | 'proposing' | 'retry';

interface AssistantActionsProps {
    turn: TurnActions | undefined;
    // Only the newest reply announces "earlier view"; older ones render the same text silently.
    announce?: boolean;
}

// The nearest ancestor that scrolls (the log, or the panel body when the log grows to its content).
function scrollParentOf(element: HTMLElement): HTMLElement | null {
    for (let node = element.parentElement; node; node = node.parentElement) {
        if (/(auto|scroll|overlay)/.test(getComputedStyle(node).overflowY)) {
            return node;
        }
    }
    return element.closest<HTMLElement>('[role="log"]');
}

const filtersPresent = (
    action: AssistantAction & {type: 'propose_filters'}, live: WorkspaceFilters | undefined,
): boolean => {
    if (!live) {
        return true;
    }
    return (!action.filters.rto_policy || live.rtoPolicy === action.filters.rto_policy)
        && (!action.filters.accelerated_vesting || live.acceleratedVesting === true);
};

export default function AssistantActions({turn, announce = true}: AssistantActionsProps) {
    const liveRevision = React.useSyncExternalStore(
        subscribeWorkspace,
        () => getWorkspaceSnapshot().contextRevision,
    );
    const [applied, setApplied] = React.useState<Record<number, boolean>>({});
    const [notFound, setNotFound] = React.useState<Record<number, boolean>>({});
    const [requirement, setRequirement] = React.useState<Record<number, RequirementState>>({});
    const controllers = React.useRef(new Set<AbortController>());
    React.useEffect(() => () => {
        controllers.current.forEach((controller) => controller.abort());
    }, []);
    const root = React.useRef<HTMLDivElement>(null);
    const focusNext = React.useRef<{index: number; kind: FocusKind} | null>(null);

    // Staleness is per reply: the revision it answered, plus the revisions its
    // own clicks caused. Any other change (a newer selection, Back, a refresh)
    // leaves the reply on an earlier view.
    const accepted = React.useRef<Set<number>>(new Set());
    const acceptedFor = React.useRef<TurnActions | undefined>(undefined);
    const selfChangeUntil = React.useRef(0);
    if (acceptedFor.current !== turn) {
        acceptedFor.current = turn;
        accepted.current = new Set(turn ? [turn.revision] : []);
    }
    const hasApplied = Object.values(applied).some(Boolean);
    const adoptLiveRevision = () => {
        const snapshot = getWorkspaceSnapshot();
        const live = snapshot.contextRevision;
        if (accepted.current.has(live)) {
            return true;
        }
        if (Date.now() <= selfChangeUntil.current
            || (hasApplied && turn?.context && sameContext(snapshot.context, turn.context))) {
            accepted.current.add(live);
            return true;
        }
        return false;
    };
    const fresh = turn ? adoptLiveRevision() : true;
    const stale = !fresh;
    const liveFilters = getWorkspaceSnapshot().context?.filters;
    const isDone = (index: number, action: AssistantAction) => applied[index] === true
        && (action.type !== 'propose_filters' || filtersPresent(action, liveFilters));

    // Background check (propose is read-only) so "Save as a requirement" is
    // only offered when it would change something.
    const [saveable, setSaveable] = React.useState<Record<number, Proposal | 'already'>>({});
    const prefetched = React.useRef(new Set<number>());
    React.useEffect(() => {
        turn?.actions.forEach((action, index) => {
            if (action.type !== 'propose_filters' || !applied[index] || prefetched.current.has(index)) {
                return;
            }
            prefetched.current.add(index);
            const controller = new AbortController();
            controllers.current.add(controller);
            proposePriorities(filtersToPatch(action), 'account', controller.signal)
                .then((proposal) => setSaveable((prev) => ({
                    ...prev, [index]: proposal.changes.length === 0 ? 'already' : proposal,
                })))
                .catch(() => undefined)
                .finally(() => controllers.current.delete(controller));
        });
    }, [turn, applied]);

    const staleShown = !!turn && stale
        && turn.actions.some((action, index) => !isDone(index, action));
    const prevStale = React.useRef(staleShown);
    const mounted = React.useRef(false);
    // After a state change, bring the bubble's bottom edge (the confirm row of
    // a review card included) into the scrolling ancestor. The card heading may
    // scroll away when the card is taller than the view; it stays reachable by
    // scrolling. A reader who has scrolled this reply out of view is left alone.
    React.useLayoutEffect(() => {
        const staleFlip = staleShown && !prevStale.current;
        prevStale.current = staleShown;
        const focusTarget = focusNext.current;
        focusNext.current = null;
        if (focusTarget) {
            const active = document.activeElement;
            if (!active || active === document.body) {
                root.current?.querySelector<HTMLElement>(
                    `[data-focus-key="${focusTarget.index}:${focusTarget.kind}"]`)?.focus({preventScroll: true});
            }
        }
        if (!mounted.current) {
            mounted.current = true;
            return;
        }
        const host = root.current as HTMLElement;
        const scroller = scrollParentOf(host);
        if (!scroller) {
            return;
        }
        const scrollerRect = scroller.getBoundingClientRect();
        const hostRect = host.getBoundingClientRect();
        if (hostRect.bottom <= scrollerRect.top || hostRect.top >= scrollerRect.bottom) {
            return;
        }
        const bubble = host.closest('.chat-bubble-assistant') ?? host;
        if (staleFlip) {
            // A view change makes every reply stale at once: follow it only for the newest bubble.
            const bubbles = scroller.querySelectorAll('.chat-bubble-assistant');
            const btn = host.querySelector('.assistant-action-btn') as Element;
            if (bubbles[bubbles.length - 1] !== bubble
                || btn.getBoundingClientRect().bottom > scrollerRect.bottom) {
                return;
            }
        }
        const overflow = bubble.getBoundingClientRect().bottom - scrollerRect.bottom + 8;
        if (overflow > 0) {
            scroller.scrollTop += overflow;
        }
    }, [applied, requirement, saveable, staleShown, notFound]);
    if (!turn || turn.actions.length === 0) {
        return null;
    }
    const names = turn.names ?? {};

    const setReq = (index: number, state: RequirementState, focus?: FocusKind) => {
        if (focus) {
            focusNext.current = {index, kind: focus};
        }
        setRequirement((prev) => ({...prev, [index]: state}));
    };

    const markApplied = (index: number) => {
        selfChangeUntil.current = Date.now() + SELF_CHANGE_WINDOW_MS;
        setApplied((prev) => ({...prev, [index]: true}));
    };

    const run = (index: number, action: AssistantAction) => {
        // Click-time check: the context may have moved since render.
        if (isDone(index, action) || !adoptLiveRevision()) {
            return;
        }
        selfChangeUntil.current = Date.now() + SELF_CHANGE_WINDOW_MS;
        if (action.type === 'open_company') {
            const outcome = openWorkspaceCompany(action.organization_id);
            if (outcome === 'opened') {
                setNotFound((prev) => ({...prev, [index]: false}));
                markApplied(index);
            } else if (outcome === 'not-found') {
                setNotFound((prev) => ({...prev, [index]: true}));
            } else {
                window.location.assign(companyUrl(action.organization_id));
            }
            return;
        }
        if (applyWorkspaceFilters(toWorkspaceFilters(action))) {
            markApplied(index);
            return;
        }
        // No rankings list is mounted (/chat/, jobs): go to the rankings page.
        window.location.assign(`/?${filterQuery(action)}`);
    };

    const propose = async (index: number, action: AssistantAction & {type: 'propose_filters'}) => {
        const controller = new AbortController();
        controllers.current.add(controller);
        setReq(index, {status: 'proposing'}, 'proposing');
        try {
            const proposal = await proposePriorities(filtersToPatch(action), 'account', controller.signal);
            setReq(index, {status: 'review', proposal, applying: false, error: null, stale: false});
        } catch (err) {
            if (controller.signal.aborted) {
                return;
            }
            setReq(index, {
                status: 'error',
                message: err instanceof ApiFailure ? err.message : GENERIC_ERROR_MESSAGE,
            }, 'retry');
        } finally {
            controllers.current.delete(controller);
        }
    };

    const openReview = (index: number, action: AssistantAction & {type: 'propose_filters'}) => {
        const cached = saveable[index];
        if (cached && cached !== 'already') {
            setReq(index, {status: 'review', proposal: cached, applying: false, error: null, stale: false});
        } else {
            void propose(index, action);
        }
    };

    const save = async (index: number, state: Extract<RequirementState, {status: 'review'}>) => {
        const controller = new AbortController();
        controllers.current.add(controller);
        setReq(index, {...state, applying: true, error: null});
        try {
            const result = await applyProposal(state.proposal.token, controller.signal);
            if (result.revision !== null) {
                setPrioritiesRevision(result.revision);
            }
            setReq(index, {status: 'saved', undo: result.undo, undoState: 'idle', undoError: null}, 'saved');
        } catch (err) {
            if (controller.signal.aborted) {
                return;
            }
            const failure = err instanceof ApiFailure ? err : null;
            setReq(index, {
                ...state,
                applying: false,
                stale: failure?.stale === true,
                error: failure ? failure.message : GENERIC_ERROR_MESSAGE,
            });
        } finally {
            controllers.current.delete(controller);
        }
    };

    const undo = async (index: number, state: Extract<RequirementState, {status: 'saved'}>) => {
        if (!state.undo || state.undoState === 'pending') {
            return;
        }
        const controller = new AbortController();
        controllers.current.add(controller);
        setReq(index, {...state, undoState: 'pending', undoError: null});
        try {
            const revision = await undoApplied(state.undo, controller.signal);
            if (revision !== null) {
                setPrioritiesRevision(revision);
            }
            setReq(index, {...state, undoState: 'done', undoError: null}, 'saved');
        } catch (err) {
            if (controller.signal.aborted) {
                return;
            }
            const failure = err instanceof ApiFailure ? err : null;
            setReq(index, {
                ...state,
                undoState: 'error',
                undoError: failure?.stale
                    ? 'Your priorities changed since this update, so it can no longer be undone.'
                    : (failure?.message || 'Could not undo. Try again.'),
            });
        } finally {
            controllers.current.delete(controller);
        }
    };

    const staleText = staleShown ? <><i className="fa-solid fa-clock-rotate-left" aria-hidden="true"></i><span>{STALE_MESSAGE}</span></> : '';

    return (
        <div className="assistant-actions mt-2" ref={root} data-testid="assistant-actions" role="group" aria-label="Suggested actions">
            {turn.actions.map((action, index) => {
                const done = isDone(index, action);
                const disabled = stale && !done;
                const state = requirement[index] ?? {status: 'idle'};
                const label = action.type === 'open_company'
                    ? (done ? `Opened ${names[action.organization_id] ?? 'company'}` : `Open ${names[action.organization_id] ?? 'company details'}`)
                    : filterLabel(action, done);
                return (
                    <div className="assistant-action" key={index}>
                        <button type="button"
                                className={`chat-btn chat-btn-primary chat-focus assistant-action-btn${done ? ' is-done' : ''}`}
                                data-testid={`assistant-action-${action.type}`}
                                aria-disabled={disabled || done}
                                onClick={() => run(index, action)}>
                            {done && <i className="fa-solid fa-check me-1" aria-hidden="true"></i>}
                            {label}
                        </button>
                        {action.type === 'open_company' && notFound[index] && !done && (
                            <div className="small assistant-action-note" role="status" data-testid="assistant-action-not-in-list">
                                <i className="fa-solid fa-circle-info" aria-hidden="true"></i>
                                <span>{NOT_IN_LIST_MESSAGE}</span>
                            </div>
                        )}
                        {action.type === 'propose_filters' && done && state.status === 'idle' && saveable[index] === 'already' && (
                            <div className="small assistant-action-note" role="status" data-testid="assistant-action-already">
                                <i className="fa-solid fa-circle-check" aria-hidden="true"></i>
                                <span>Already one of your requirements.</span>
                            </div>
                        )}
                        {action.type === 'propose_filters' && done && state.status === 'idle' && saveable[index] !== 'already' && (
                            <button type="button" className="chat-btn chat-btn-secondary chat-focus assistant-action-save"
                                    data-testid="assistant-action-save" data-focus-key={`${index}:save`}
                                    onClick={() => openReview(index, action)}>
                                Save as a requirement
                            </button>
                        )}
                        {state.status === 'proposing' && (
                            <div className="small assistant-action-note" role="status" tabIndex={-1} data-focus-key={`${index}:proposing`}>
                                <span className="spinner-border spinner-border-sm" aria-hidden="true"></span>
                                <span>Preparing the change for review…</span>
                            </div>
                        )}
                        {state.status === 'error' && (
                            <div className="small assistant-action-note assistant-action-error" role="alert" data-testid="assistant-action-error">
                                <i className="fa-solid fa-circle-exclamation" aria-hidden="true"></i>
                                <span>{state.message === GENERIC_ERROR_MESSAGE ? "Couldn't prepare the change for review." : state.message}</span>
                                {action.type === 'propose_filters' && (
                                    <button type="button" className="chat-btn chat-btn-secondary chat-focus assistant-action-retry"
                                            data-focus-key={`${index}:retry`}
                                            onClick={() => void propose(index, action)}>Try again</button>
                                )}
                            </div>
                        )}
                        {action.type === 'propose_filters' && state.status === 'review' && state.proposal.changes.length === 0 && (
                            <div className="small assistant-action-note" role="status" data-testid="assistant-action-already">
                                <i className="fa-solid fa-circle-check" aria-hidden="true"></i>
                                <span>Already one of your requirements.</span>
                            </div>
                        )}
                        {action.type === 'propose_filters' && state.status === 'review' && state.proposal.changes.length > 0 && (
                            <ReviewChanges
                                changes={state.proposal.changes}
                                scope="account"
                                pending={state.applying}
                                error={state.error}
                                stale={state.stale}
                                heading="Save as a requirement?"
                                labels={REVIEW_LABELS}
                                choicePaths={REVIEW_CHOICES}
                                applyLabel="Save"
                                testId="assistant-action-review"
                                onApply={() => void save(index, state)}
                                onCancel={() => setReq(index, {status: 'idle'}, 'save')}
                                onReviewLatest={() => void propose(index, action)}
                            />
                        )}
                        {state.status === 'saved' && (
                            <div className="small assistant-action-note" role="status" tabIndex={-1}
                                 data-testid="assistant-action-saved" data-focus-key={`${index}:saved`}>
                                <i className={`fa-solid ${state.undoState === 'done' ? 'fa-rotate-left' : 'fa-circle-check'}`} aria-hidden="true"></i>
                                <span>{state.undoState === 'done' ? 'Change undone.' : 'Saved to your account.'}</span>
                                {state.undo && state.undoState !== 'done' && (
                                    <button type="button" className="chat-btn chat-btn-secondary chat-focus assistant-action-undo"
                                            data-testid="assistant-action-undo"
                                            aria-disabled={state.undoState === 'pending'}
                                            onClick={() => void undo(index, state)}>
                                        {state.undoState === 'pending' ? 'Undoing…' : 'Undo'}
                                    </button>
                                )}
                            </div>
                        )}
                        {state.status === 'saved' && state.undoError && (
                            <div className="small assistant-action-note assistant-action-error" role="alert" data-testid="assistant-action-undo-error">
                                <i className="fa-solid fa-circle-exclamation" aria-hidden="true"></i>
                                <span>{state.undoError}</span>
                            </div>
                        )}
                    </div>
                );
            })}
            {announce ? (
                <div className="assistant-actions-status small" role="status" data-testid="assistant-actions-stale">{staleText}</div>
            ) : (
                <div className="assistant-actions-status small" data-testid="assistant-actions-stale">{staleText}</div>
            )}
        </div>
    );
}
