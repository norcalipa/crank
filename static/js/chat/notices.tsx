// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {ChangeList} from '../priorities/ReviewChanges';
import type {AssistantState, AssistantStatus, AvailabilityPayload, PreferenceChange, PreferenceProposal, PreferenceUndoState} from './types';

/** Preference-change notice (issue #466): the field-level diff of what the
 * assistant just changed, with a one-click Undo. Four rendered states:
 * populated (diff list + Undo), loading (undo request in flight), empty
 * (update reported but no field diff), and error (undo rejected, e.g. a
 * stale-revision conflict). */
export function PreferenceChangeNotice({changes, undoState, undoError, undoErrorType, onUndo, onDismiss, onReview}: {
    changes: PreferenceChange[];
    undoState: PreferenceUndoState;
    undoError: string | null;
    // Server error type of the failed undo (issue #466 review): the review
    // action is stale-only; other failures stay retry-oriented.
    undoErrorType: string | null;
    onUndo: () => void;
    onDismiss: () => void;
    // Stale-conflict recovery (issue #466 round 2): focus the composer so the
    // user can ask the assistant for the current preferences.
    onReview?: () => void;
}) {
    if (undoState === 'done') {
        return (
            <div className="alert alert-success pref-change-notice"
                 role="status" aria-label="Preference update undone" data-testid="preference-change-undone">
                <div className="pref-change-header">
                    <span className="pref-change-summary">
                        <i className="fa-solid fa-rotate-left me-1" aria-hidden="true"></i>
                        The preference update was undone.
                    </span>
                    <button type="button" className="pref-change-dismiss" aria-label="Dismiss undo notice"
                            onClick={onDismiss}>
                        <i className="fa-solid fa-xmark" aria-hidden="true"></i>
                    </button>
                </div>
            </div>
        );
    }
    const pending = undoState === 'pending';
    // Only a server-confirmed stale revision renders the stale-only review
    // action (issue #466 review): connectivity/5xx failures keep the
    // retry-oriented Undo path instead.
    const staleConflict = undoState === 'error' && (undoErrorType === 'preference_stale' || undoErrorType === 'forbidden');
    const emptyDiff = changes.length === 0;
    return (
        <div className="alert alert-success pref-change-notice" role="status"
             aria-label="Preference update details" data-testid="preference-change-notice">
            <div className="pref-change-header">
                <span className="pref-change-summary">
                    <i className="fa-solid fa-circle-check me-1" aria-hidden="true"></i>
                    Your saved preferences were updated based on this conversation.
                </span>
                <button type="button" className="pref-change-dismiss" aria-label="Dismiss preference notice"
                        onClick={onDismiss}>
                    <i className="fa-solid fa-xmark" aria-hidden="true"></i>
                </button>
            </div>
            {!emptyDiff ? (
                <ChangeList changes={changes} label="Changed preferences"/>
            ) : (
                <p className="pref-change-empty" data-testid="preference-change-empty">
                    The update did not change any individual preference fields.
                </p>
            )}
            {undoState === 'error' && undoError && (
                <div className="pref-change-error" role="alert" data-testid="preference-undo-error">
                    <i className="fa-solid fa-triangle-exclamation me-1" aria-hidden="true"></i>
                    {undoError}
                </div>
            )}
            <div className="chat-actions mt-2" role="group" aria-label="Preference update actions">
                {staleConflict && onReview && (
                    // After a stale conflict the undo token is dead, so the
                    // primary recovery is reviewing the current preferences
                    // (focuses the composer); Undo drops to secondary.
                    <button type="button" className="chat-btn chat-btn-primary pref-review-btn"
                            onClick={onReview} data-testid="preference-review-button">
                        <i className="fa-solid fa-list-check me-1" aria-hidden="true"></i>
                        Review current preferences
                    </button>
                )}
                <button type="button" className="chat-btn chat-btn-secondary chat-focus pref-undo-btn"
                        onClick={onUndo} disabled={pending || (undoState === 'error' && undoErrorType === 'forbidden')}
                        aria-label={pending ? 'Undoing preference update' : 'Undo preference update'}
                        aria-busy={pending} data-testid="preference-undo-button">
                    {pending ? (
                        <>
                            <span className="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
                            Undoing…
                        </>
                    ) : emptyDiff ? (
                        // Empty diff: say what the undo restores.
                        <>
                            <i className="fa-solid fa-rotate-left me-1" aria-hidden="true"></i>
                            Restore previous preferences
                        </>
                    ) : (
                        <>
                            <i className="fa-solid fa-rotate-left me-1" aria-hidden="true"></i>
                            Undo this update
                        </>
                    )}
                </button>
            </div>
        </div>
    );
}

/** Preference proposal notice (issue #466 review): the read-only field-level
 * diff of a model-proposed change with Apply/Dismiss. Nothing is persisted
 * until the user explicitly applies; a this-search-only proposal is labelled
 * as never saved. */
export function PreferenceProposalNotice({proposal, state, error, errorType, onDecision, onReview, onEdit, onSearchOnly}: {
    proposal: PreferenceProposal;
    state: 'idle' | 'pending' | 'error';
    error: string | null;
    errorType: string | null;
    onDecision: (decision: 'apply' | 'dismiss') => void;
    onReview?: () => void;
    // Issue #480: open the inline priorities editor / apply to one search only.
    onEdit?: () => void;
    onSearchOnly?: () => void;
}) {
    const pending = state === 'pending';
    const isSearch = proposal.scope === 'search';
    const staleConflict = state === 'error' && (errorType === 'preference_stale' || errorType === 'forbidden');
    const tokenExpired = state === 'error' && errorType === 'forbidden';
    return (
        <div className="alert alert-warning pref-change-notice" role="status"
             aria-label="Proposed preference change" data-testid="preference-proposal-notice">
            <div className="pref-change-header">
                <span className="pref-change-summary">
                    <i className="fa-solid fa-pen-to-square me-1" aria-hidden="true"></i>
                    {isSearch
                        ? 'The assistant suggests a filter for this search only — it will not be saved.'
                        : 'The assistant suggests updating your saved preferences.'}
                </span>
                <button type="button" className="pref-change-dismiss" aria-label="Dismiss preference proposal"
                        onClick={() => onDecision('dismiss')} disabled={pending}>
                    <i className="fa-solid fa-xmark" aria-hidden="true"></i>
                </button>
            </div>
            {proposal.changes.length > 0 ? (
                <ChangeList changes={proposal.changes} currency={proposal.currency} label="Proposed preference changes"/>
            ) : (
                <p className="pref-change-empty" data-testid="preference-proposal-empty">
                    The suggestion does not change any individual preference fields.
                </p>
            )}
            {state === 'error' && error && (
                <div className="pref-change-error" role="alert" data-testid="preference-proposal-error">
                    <i className="fa-solid fa-triangle-exclamation me-1" aria-hidden="true"></i>
                    {error}
                </div>
            )}
            <div className="chat-actions mt-2" role="group" aria-label="Preference proposal actions">
                {staleConflict && onReview && (
                    <button type="button" className="chat-btn chat-btn-primary pref-review-btn"
                            onClick={onReview} data-testid="preference-proposal-review-button">
                        <i className="fa-solid fa-list-check me-1" aria-hidden="true"></i>
                        Review current preferences
                    </button>
                )}
                <button type="button" className="chat-btn chat-btn-primary chat-focus pref-apply-btn"
                        onClick={() => onDecision('apply')} disabled={pending || tokenExpired}
                        aria-label={pending ? 'Applying preference proposal' : 'Apply preference proposal'}
                        aria-busy={pending} data-testid="preference-apply-button">
                    {pending ? (
                        <>
                            <span className="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
                            Applying…
                        </>
                    ) : (
                        <>
                            <i className="fa-solid fa-check me-1" aria-hidden="true"></i>
                            {isSearch ? 'Apply to this search' : 'Apply'}
                        </>
                    )}
                </button>
                {onEdit && (
                    <button type="button" className="chat-btn chat-btn-secondary chat-focus"
                            onClick={onEdit} disabled={pending} data-testid="preference-proposal-edit-button">
                        Edit
                    </button>
                )}
                {onSearchOnly && !isSearch && (
                    <button type="button" className="chat-btn chat-btn-secondary chat-focus"
                            onClick={onSearchOnly} disabled={pending}
                            data-testid="preference-proposal-search-only-button">
                        This search only
                    </button>
                )}
                <button type="button" className="chat-btn chat-btn-secondary chat-focus pref-dismiss-btn"
                        onClick={() => onDecision('dismiss')} disabled={pending}
                        data-testid="preference-proposal-dismiss-button">
                    Dismiss
                </button>
            </div>
        </div>
    );
}

export const GATED_STATES: AssistantState[] = ['replies_disabled', 'inventory_unavailable'];

export function isGatedState(state: AssistantState | undefined): boolean {
    return state !== undefined && GATED_STATES.includes(state);
}

// Re-checking is only meaningful for transient conditions; a fixed policy
// state (replies_disabled) is not expected to flip by re-fetching.
export const RETRYABLE_STATES: AssistantState[] = ['temporarily_unavailable', 'refreshing'];

export function AssistantStatusNotice({status, onRetry, checking}: {
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

/** Compact availability notice so an empty reply is never silently unexplained. */
export function AvailabilityNotice({availability}: {availability: AvailabilityPayload}) {
    return (
        <div className="availability-notice border rounded p-2 mt-2 small"
             role="status" aria-live="polite" data-testid="availability-notice">
            <i className="fa-solid fa-circle-info me-1" aria-hidden="true"></i>
            <strong>{availability.title}</strong> — {availability.message}
        </div>
    );
}
