// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Reusable, presentational review step (issue #480): a field-level diff of a
// proposed priorities change with Apply / Edit / This search only / Cancel.
// It owns no network state; callers (the inline editor, the chat proposal
// notice) pass the proposal and handlers. Focus moves to the heading when it
// mounts because it only mounts as the result of a user action.

import * as React from 'react';
import {PreferenceChange, expandChanges, humanizeToken, importanceLabel, preferenceValueLabel} from './format';

export interface ReviewChangesProps {
    changes: PreferenceChange[];
    scope?: 'account' | 'search';
    pending?: boolean;
    error?: string | null;
    // A stale error (revision conflict) swaps the primary action for "Review latest".
    stale?: boolean;
    heading?: string;
    applyLabel?: string;
    onApply: () => void;
    onCancel: () => void;
    onEdit?: () => void;
    onApplySearchOnly?: () => void;
    onReviewLatest?: () => void;
    // Criteria the draft edited that also changed elsewhere since it was loaded (labels).
    conflicts?: string[];
    currency?: unknown;
    choicePaths?: ReadonlySet<string>;
    // Editor field labels by path, so the diff names a field as the editor does.
    labels?: Record<string, string>;
    // The bounded sidebar block: the change comes before the note, and Cancel sits beside the primary action.
    compact?: boolean;
    testId?: string;
}

const isEmptyValue = (v: unknown) => v === null || v === undefined || v === '' || (Array.isArray(v) && v.length === 0);

// A list is shown one token per saved entry, so exactly what will be saved is visible.
function ChangeValue({value, path, currency, importance, choice}: {
    value: unknown; path: string; currency?: unknown; importance?: boolean; choice?: boolean;
}) {
    if (importance) return <>{importanceLabel(value)}</>;
    if (Array.isArray(value) && value.length > 0) {
        return (
            <>
                {value.map((item, i) => (
                    <span key={`${i}-${String(item)}`} className="pref-change-token">
                        {typeof item !== 'string' ? preferenceValueLabel(item, path, currency) : choice ? humanizeToken(item) : item}
                    </span>
                ))}
            </>
        );
    }
    return <>{preferenceValueLabel(value, path, currency)}</>;
}

const CURRENCY_PATH = 'compensation.currency';

export function ChangeList({changes, label, labels, currency, choicePaths}: {
    changes: PreferenceChange[];
    label: string;
    labels?: Record<string, string>;
    currency?: unknown;
    choicePaths?: ReadonlySet<string>;
}) {
    // A currency change in the same edit re-labels the money on both sides: old values in the old currency, new in the new.
    const currencyChange = changes.find((change) => change.path === CURRENCY_PATH);
    const oldCurrency = currencyChange ? currencyChange.old : currency;
    const newCurrency = currencyChange ? currencyChange.new : currency;
    return (
        <ul className="pref-change-list" aria-label={label}>
            {expandChanges(changes, labels).map((change) => (
                <li key={change.key} className="pref-change-item">
                    <span className="pref-change-path">{change.label}</span>
                    <span className="pref-change-values">
                        <span className={`pref-change-old${isEmptyValue(change.old) ? ' is-empty' : ''}`}>
                            <ChangeValue value={change.old} path={change.path} currency={oldCurrency} importance={change.importance}
                                         choice={choicePaths?.has(change.path)}/>
                        </span>
                        <i className="fa-solid fa-arrow-right pref-change-arrow" aria-hidden="true"></i>
                        <span className="visually-hidden">changed to</span>
                        <span className="pref-change-new">
                            <ChangeValue value={change.new} path={change.path} currency={newCurrency} importance={change.importance}
                                         choice={choicePaths?.has(change.path)}/>
                        </span>
                    </span>
                </li>
            ))}
        </ul>
    );
}

export default function ReviewChanges({
    changes, scope = 'account', pending = false, error = null, stale = false,
    heading = 'Review your changes', applyLabel, onApply, onCancel, onEdit, onApplySearchOnly, onReviewLatest,
    labels, currency, choicePaths, conflicts = [], compact = false, testId = 'priorities-review',
}: ReviewChangesProps) {
    const headingId = `priorities-review-${React.useId()}`;
    const headingRef = React.useRef<HTMLHeadingElement>(null);
    React.useEffect(() => {
        headingRef.current?.focus();
    }, []);
    const isSearch = scope === 'search';
    const note = (
        <p className="priorities-scope-note">
            {isSearch
                ? 'These changes apply to this search only. They are not saved.'
                : onApplySearchOnly
                    ? 'Save these to your account, or use them for this search only.'
                    : 'These changes will be saved to your account and used for matching.'}
        </p>
    );
    const cancel = (
        <button type="button" className="btn btn-sm btn-link text-light priorities-review-cancel"
                onClick={onCancel} disabled={pending}>Cancel</button>
    );
    return (
        <div className={`priorities-card priorities-review${compact ? ' priorities-review-compact' : ''}`} data-testid={testId}
             role="group" aria-labelledby={headingId}>
            <h3 id={headingId} className="h6 priorities-heading" tabIndex={-1} ref={headingRef}>
                {heading}
            </h3>
            <span className="visually-hidden" role="status">
                {changes.length === 1 ? '1 change to review' : `${changes.length} changes to review`}
            </span>
            {!compact && note}
            {conflicts.length > 0 && (
                <p className="pref-change-conflict" role="status" data-testid="priorities-review-conflicts">
                    <i className="fa-solid fa-code-merge me-1" aria-hidden="true"></i>
                    Also changed elsewhere: {conflicts.join(', ')}. Your edit replaces the newer value.
                </p>
            )}
            {changes.length > 0 ? (
                <ChangeList changes={changes} label="Proposed changes" labels={labels} currency={currency} choicePaths={choicePaths}/>
            ) : (
                <p className="pref-change-empty" data-testid="priorities-review-empty">
                    Nothing would change. Edit a priority to continue.
                </p>
            )}
            {compact && note}
            {error && (
                <div className="pref-change-error" role="alert" data-testid="priorities-review-error">
                    <i className="fa-solid fa-triangle-exclamation me-1" aria-hidden="true"></i>
                    {error}
                </div>
            )}
            <div className="chat-actions" role="group" aria-label="Review actions">
                {stale && onReviewLatest ? (
                    <button type="button" className="btn btn-sm btn-primary" aria-disabled={pending} aria-busy={pending}
                            onClick={() => { if (!pending) onReviewLatest(); }}>
                        {pending ? 'Checking…' : 'Review latest'}
                    </button>
                ) : (
                    <button type="button" className="btn btn-sm btn-primary"
                            onClick={onApply} disabled={pending || changes.length === 0} aria-busy={pending}>
                        {pending ? (
                            <>
                                <span className="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
                                Applying…
                            </>
                        ) : (isSearch ? 'Apply to this search' : (applyLabel ?? 'Apply to account'))}
                    </button>
                )}
                {compact && cancel}
                {onEdit && (
                    <button type="button" className="btn btn-sm btn-link text-light priorities-review-edit"
                            onClick={onEdit} disabled={pending}>Edit</button>
                )}
                {onApplySearchOnly && !isSearch && (
                    <button type="button" className="btn btn-sm btn-outline-light priorities-review-search"
                            onClick={onApplySearchOnly} disabled={pending || changes.length === 0}>
                        This search only
                    </button>
                )}
                {!compact && cancel}
            </div>
        </div>
    );
}
