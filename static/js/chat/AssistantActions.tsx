// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Explicit, user-confirmed assistant actions (issue #484). The server returns
// a small allowlist of typed actions (ids and enums only); this module renders
// each as a button and builds every URL itself from those enums and ids. A
// click re-checks that the page is still the one the reply answered, so a
// late reply can never change a view the user has since left. Nothing here
// moves keyboard focus: the buttons keep it, and an applied action only
// changes its own label.

import * as React from 'react';
import {COMPANY_OPEN_EVENT} from '../suggestCompany/controller';
import {
    ApiFailure, GENERIC_ERROR_MESSAGE, Proposal, applyProposal, proposePriorities,
} from '../priorities/api';
import ReviewChanges from '../priorities/ReviewChanges';
import {
    applyWorkspaceFilters, getWorkspaceSnapshot, setPrioritiesRevision, subscribeWorkspace,
} from '../workspace/store';
import {RtoPolicyCode, WorkspaceFilters} from '../workspace/types';

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
}

const RTO_CODES: readonly string[] = ['R', 'H', 'O'];
const RTO_WORDS: Record<RtoPolicyCode, string> = {R: 'remote', H: 'hybrid', O: 'in-office'};
const RTO_PATCH_MODE: Record<RtoPolicyCode, string> = {R: 'remote', H: 'hybrid', O: 'in-office'};
export const STALE_MESSAGE = 'This suggestion was for an earlier view.';

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

type RequirementState =
    | {status: 'idle'}
    | {status: 'proposing'}
    | {status: 'review'; proposal: Proposal; applying: boolean; error: string | null; stale: boolean}
    | {status: 'saved'}
    | {status: 'error'; message: string};

interface AssistantActionsProps {
    turn: TurnActions | undefined;
}

export default function AssistantActions({turn}: AssistantActionsProps) {
    const liveRevision = React.useSyncExternalStore(
        subscribeWorkspace,
        () => getWorkspaceSnapshot().contextRevision,
    );
    const [applied, setApplied] = React.useState<Record<number, boolean>>({});
    const [requirement, setRequirement] = React.useState<Record<number, RequirementState>>({});
    const controllers = React.useRef(new Set<AbortController>());
    React.useEffect(() => () => {
        controllers.current.forEach((controller) => controller.abort());
    }, []);
    if (!turn || turn.actions.length === 0) {
        return null;
    }
    const names = turn.names ?? {};
    const stale = turn.revision !== liveRevision;
    const anyPending = turn.actions.some((_, index) => !applied[index]);

    const setReq = (index: number, state: RequirementState) =>
        setRequirement((prev) => ({...prev, [index]: state}));

    const run = (index: number, action: AssistantAction) => {
        // Click-time check: the context may have moved since render.
        if (applied[index] || turn.revision !== getWorkspaceSnapshot().contextRevision) {
            return;
        }
        if (action.type === 'open_company') {
            if (document.getElementById('organization-list')) {
                window.dispatchEvent(new CustomEvent(COMPANY_OPEN_EVENT, {
                    detail: {organizationId: action.organization_id},
                }));
                setApplied((prev) => ({...prev, [index]: true}));
            } else {
                window.location.assign(companyUrl(action.organization_id));
            }
            return;
        }
        if (applyWorkspaceFilters(toWorkspaceFilters(action))) {
            setApplied((prev) => ({...prev, [index]: true}));
            return;
        }
        // No rankings list is mounted (/chat/, jobs): go to the rankings page.
        window.location.assign(`/?${filterQuery(action)}`);
    };

    const propose = async (index: number, action: AssistantAction & {type: 'propose_filters'}) => {
        const controller = new AbortController();
        controllers.current.add(controller);
        setReq(index, {status: 'proposing'});
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
            });
        } finally {
            controllers.current.delete(controller);
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
            setReq(index, {status: 'saved'});
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

    return (
        <div className="assistant-actions mt-2" data-testid="assistant-actions" role="group" aria-label="Suggested actions">
            {turn.actions.map((action, index) => {
                const done = applied[index] === true;
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
                        {action.type === 'propose_filters' && done && state.status === 'idle' && (
                            <button type="button" className="chat-btn chat-btn-secondary chat-focus assistant-action-save"
                                    data-testid="assistant-action-save"
                                    onClick={() => void propose(index, action)}>
                                Also save as a lasting requirement
                            </button>
                        )}
                        {state.status === 'proposing' && (
                            <div className="small assistant-action-note" role="status">Preparing the change for review…</div>
                        )}
                        {state.status === 'error' && (
                            <div className="small assistant-action-note assistant-action-error" role="alert" data-testid="assistant-action-error">
                                <i className="fa-solid fa-triangle-exclamation me-1" aria-hidden="true"></i>
                                {state.message}{' '}
                                {action.type === 'propose_filters' && (
                                    <button type="button" className="btn btn-link btn-sm p-0 align-baseline chat-focus"
                                            onClick={() => void propose(index, action)}>Try again</button>
                                )}
                            </div>
                        )}
                        {action.type === 'propose_filters' && state.status === 'review' && (
                            <ReviewChanges
                                changes={state.proposal.changes}
                                scope="account"
                                pending={state.applying}
                                error={state.error}
                                stale={state.stale}
                                heading="Save as a lasting requirement"
                                testId="assistant-action-review"
                                onApply={() => void save(index, state)}
                                onCancel={() => setReq(index, {status: 'idle'})}
                                onReviewLatest={() => void propose(index, action)}
                            />
                        )}
                        {state.status === 'saved' && (
                            <div className="small assistant-action-note" role="status" data-testid="assistant-action-saved">
                                <i className="fa-solid fa-circle-check me-1" aria-hidden="true"></i>
                                Saved to your account.
                            </div>
                        )}
                    </div>
                );
            })}
            <div className="assistant-actions-status small" role="status" data-testid="assistant-actions-stale">
                {stale && anyPending ? STALE_MESSAGE : ''}
            </div>
        </div>
    );
}
