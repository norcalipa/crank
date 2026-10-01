// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Reusable, presentational review step (issue #480): a field-level diff of a
// proposed priorities change with Apply / Edit / This search only / Cancel.
// It owns no network state; callers (the inline editor, the chat proposal
// notice) pass the proposal and handlers. Focus moves to the heading when it
// mounts because it only mounts as the result of a user action.

import * as React from 'react';
import {PreferenceChange, preferencePathLabel, preferenceValueLabel} from './format';

export interface ReviewChangesProps {
    changes: PreferenceChange[];
    scope?: 'account' | 'search';
    pending?: boolean;
    error?: string | null;
    // A stale error (revision conflict) swaps the primary action for "Review latest".
    stale?: boolean;
    heading?: string;
    onApply: () => void;
    onCancel: () => void;
    onEdit?: () => void;
    onApplySearchOnly?: () => void;
    onReviewLatest?: () => void;
    // Editor field labels by path, so the diff names a field as the editor does.
    labels?: Record<string, string>;
    testId?: string;
}

const isEmptyValue = (v: unknown) => v === null || v === undefined || v === '' || (Array.isArray(v) && v.length === 0);

export function ChangeList({changes, label, labels}: {
    changes: PreferenceChange[];
    label: string;
    labels?: Record<string, string>;
}) {
    return (
        <ul className="pref-change-list" aria-label={label}>
            {changes.map((change) => (
                <li key={change.path} className="pref-change-item">
                    <span className="pref-change-path">{labels?.[change.path] || preferencePathLabel(change.path)}</span>
                    <span className="pref-change-values">
                        <span className={`pref-change-old${isEmptyValue(change.old) ? ' is-empty' : ''}`}>{preferenceValueLabel(change.old, change.path)}</span>
                        <i className="fa-solid fa-arrow-right pref-change-arrow" aria-hidden="true"></i>
                        <span className="visually-hidden">changed to</span>
                        <span className="pref-change-new">{preferenceValueLabel(change.new, change.path)}</span>
                    </span>
                </li>
            ))}
        </ul>
    );
}

export default function ReviewChanges({
    changes, scope = 'account', pending = false, error = null, stale = false,
    heading = 'Review your changes', onApply, onCancel, onEdit, onApplySearchOnly, onReviewLatest,
    labels, testId = 'priorities-review',
}: ReviewChangesProps) {
    const headingId = `priorities-review-${React.useId()}`;
    const headingRef = React.useRef<HTMLHeadingElement>(null);
    React.useEffect(() => {
        headingRef.current?.focus();
    }, []);
    const isSearch = scope === 'search';
    return (
        <div className="priorities-card priorities-review" data-testid={testId}
             role="group" aria-labelledby={headingId}>
            <h3 id={headingId} className="h6 priorities-heading" tabIndex={-1} ref={headingRef}>
                {heading}
            </h3>
            <span className="visually-hidden" role="status">
                {changes.length === 1 ? '1 change to review' : `${changes.length} changes to review`}
            </span>
            <p className="priorities-scope-note">
                {isSearch
                    ? 'These changes apply to this search only. They are not saved.'
                    : onApplySearchOnly
                        ? 'Save these to your account, or use them for this search only.'
                        : 'These changes will be saved to your account and used for matching.'}
            </p>
            {changes.length > 0 ? (
                <ChangeList changes={changes} label="Proposed changes" labels={labels}/>
            ) : (
                <p className="pref-change-empty" data-testid="priorities-review-empty">
                    Nothing would change. Edit a priority to continue.
                </p>
            )}
            {error && (
                <div className="pref-change-error" role="alert" data-testid="priorities-review-error">
                    <i className="fa-solid fa-triangle-exclamation me-1" aria-hidden="true"></i>
                    {error}
                </div>
            )}
            <div className="chat-actions" role="group" aria-label="Review actions">
                {stale && onReviewLatest ? (
                    <button type="button" className="btn btn-sm btn-primary"
                            onClick={onReviewLatest}>Review latest</button>
                ) : (
                    <button type="button" className="btn btn-sm btn-primary"
                            onClick={onApply} disabled={pending || changes.length === 0} aria-busy={pending}>
                        {pending ? (
                            <>
                                <span className="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
                                Applying…
                            </>
                        ) : (isSearch ? 'Apply to this search' : 'Apply to account')}
                    </button>
                )}
                {onEdit && (
                    <button type="button" className="btn btn-sm btn-link text-light"
                            onClick={onEdit} disabled={pending}>Edit</button>
                )}
                {onApplySearchOnly && !isSearch && (
                    <button type="button" className="btn btn-sm btn-outline-light"
                            onClick={onApplySearchOnly} disabled={pending || changes.length === 0}>
                        This search only
                    </button>
                )}
                <button type="button" className="btn btn-sm btn-link text-light"
                        onClick={onCancel} disabled={pending}>Cancel</button>
            </div>
        </div>
    );
}
