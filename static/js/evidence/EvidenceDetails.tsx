// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {EvidenceStatusKey, EvidenceSummaryData, fieldKeyLabel, formatEvidenceDate} from '../labels';
import EvidenceBadge from './EvidenceBadge';

export interface EvidenceField {
    field_key: string;
    value: string;
    source_domain: string;
    source_url?: string | null;
    observed_at: string;
    scope: Record<string, unknown>;
    last_checked_at: string | null;
    last_successful_fetch_at: string | null;
    last_changed_at: string | null;
    last_verified_at: string | null;
    stale: boolean;
    status?: 'verified' | 'stale';
    review?: 'none' | 'pending' | 'conflicted';
    policy_days?: number;
}

export interface PendingReviewItem {
    field_key: string;
    review: 'pending' | 'conflicted';
    observed_at: string;
    source_domain: string;
    observed_value: string;
}

export interface EvidenceData {
    fields?: EvidenceField[];
    unverified_fields?: string[];
    pending_review?: PendingReviewItem[];
    summary?: EvidenceSummaryData;
}

interface EvidenceDetailsProps {
    evidence: EvidenceData;
    ratingCoverage?: {covered: number; total: number} | null;
    renderFieldAction?: (fieldKey: string, verb?: string, joiner?: string) => React.ReactNode;
    emptyAction?: React.ReactNode;
}

const formatScope = (scope: Record<string, unknown>): string => {
    const parts: string[] = [];
    for (const [key, value] of Object.entries(scope || {})) {
        if (Array.isArray(value) && value.length > 0) {
            parts.push(`${key}: ${value.join(', ')}`);
        }
    }
    return parts.length > 0 ? `Scope — ${parts.join('; ')}` : '';
};

const SourceText: React.FC<{field: EvidenceField}> = ({field}) => {
    const domain = field.source_domain || 'unknown source';
    if (field.source_url) {
        return (
            <a href={field.source_url} target="_blank" rel="noopener noreferrer nofollow"
               data-testid={`field-source-link-${field.field_key}`}>
                {domain} <span className="evidence-external-note">(opens in a new tab)</span>
            </a>
        );
    }
    return <span>{domain}</span>;
};

export const EvidenceSkeleton: React.FC = () => (
    <div className="evidence-skeleton" role="status" aria-busy="true" data-testid="provenance-loading">
        <span className="visually-hidden">Loading evidence…</span>
        {[0, 1, 2].map(index => (
            <div className="evidence-skeleton-row" key={index} aria-hidden="true">
                <span className="evidence-skeleton-bar evidence-skeleton-label"></span>
                <span className="evidence-skeleton-bar evidence-skeleton-value"></span>
            </div>
        ))}
    </div>
);

const EvidenceDetails: React.FC<EvidenceDetailsProps> = ({
    evidence, ratingCoverage, renderFieldAction, emptyAction
}) => {
    const fields = evidence.fields || [];
    const unverified = evidence.unverified_fields || [];
    const pending = evidence.pending_review || [];
    const summary = evidence.summary;
    const action = (key: string, verb?: string, joiner?: string) =>
        renderFieldAction ? renderFieldAction(key, verb, joiner) : null;
    return (
        <div data-testid="evidence-details">
            <div className="evidence-coverage mt-3" data-testid="coverage-summary">
                {ratingCoverage && (
                    <div className="row mb-2" data-testid="coverage-rating">
                        <div className="col-5 text-end fw-bold">Rating coverage:</div>
                        <div className="col-7">{ratingCoverage.covered} of {ratingCoverage.total} rating dimensions</div>
                    </div>
                )}
                {summary && (
                    <div className="row mb-2" data-testid="coverage-facts">
                        <div className="col-5 text-end fw-bold">Fact coverage:</div>
                        <div className="col-7">
                            {summary.fact_coverage} of {summary.total} tracked facts
                            <span className="d-block small text-muted">
                                {summary.verified} verified · {summary.stale} stale · {summary.unknown} unknown
                            </span>
                        </div>
                    </div>
                )}
            </div>
            {fields.length === 0 && (
                <div className="evidence-empty mt-2" data-testid="evidence-empty">
                    <p className="mb-2">No accepted evidence for any tracked fact. Until a source is accepted, these
                        facts stay unknown.</p>
                    {emptyAction}
                </div>
            )}
            {fields.length > 0 && (
                <div className="mt-2" data-testid="field-evidence">
                    <h4 className="h6 fw-semibold mt-3 mb-2">Field evidence</h4>
                    {fields.map(field => {
                        const key = field.field_key;
                        const status: EvidenceStatusKey = field.status || (field.stale ? 'stale' : 'verified');
                        const review = field.review && field.review !== 'none' ? field.review : null;
                        const scope = formatScope(field.scope);
                        const badge = (
                            <EvidenceBadge status={status} fieldLabel={fieldKeyLabel(key)}
                                           lastVerifiedAt={field.last_verified_at}
                                           testId={`field-status-${key}`}/>
                        );
                        return (
                            <div className="row mb-2 align-items-baseline evidence-row" key={key}
                                 data-testid={`field-evidence-${key}`}>
                                <div className="col-5 text-end fw-bold">{fieldKeyLabel(key)}:</div>
                                <div className="col-7 evidence-row-value">
                                    <span>
                                        <span className="evidence-value" data-testid={`field-value-${key}`}>
                                            {field.value}
                                        </span>{' '}
                                        {status === 'stale'
                                            ? <span data-testid={`field-stale-${key}`}>{badge}</span>
                                            : badge}
                                        {review && (
                                            <>
                                                {' '}
                                                <EvidenceBadge status={review} testId={`field-review-${key}`}/>
                                            </>
                                        )}
                                        <span className="text-muted small evidence-meta">
                                            Source: <SourceText field={field}/>
                                        </span>
                                        <span className="text-muted small evidence-meta">
                                            Observed {formatEvidenceDate(field.observed_at)}
                                        </span>
                                        <span className="text-muted small evidence-meta">
                                            <span className="text-nowrap">Last verified {field.last_verified_at
                                                ? formatEvidenceDate(field.last_verified_at)
                                                : 'never'}</span>
                                        </span>
                                        {field.policy_days ? (
                                            <span className="text-muted small evidence-meta">Re-check every {field.policy_days} days</span>
                                        ) : null}
                                        {scope && <span className="text-muted small evidence-meta">{scope}</span>}
                                        <details className="evidence-timestamps small">
                                            <summary>
                                                Check history<span className="visually-hidden"> for {fieldKeyLabel(key)}</span>
                                            </summary>
                                            <dl className="mb-0">
                                                <dt>Last checked</dt>
                                                <dd>{formatEvidenceDate(field.last_checked_at)}</dd>
                                                <dt>Last successful fetch</dt>
                                                <dd>{formatEvidenceDate(field.last_successful_fetch_at)}</dd>
                                                <dt>Last changed</dt>
                                                <dd>{formatEvidenceDate(field.last_changed_at)}</dd>
                                                <dt>Last verified</dt>
                                                <dd>{formatEvidenceDate(field.last_verified_at)}</dd>
                                            </dl>
                                        </details>
                                    </span>
                                    {action(key)}
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}
            {unverified.length > 0 && (
                <div className="mt-2" data-testid="unverified-fields">
                    {unverified.map(key => (
                        <div className="row mb-1 align-items-baseline evidence-row" key={key}
                             data-testid={`field-unverified-${key}`}>
                            <div className="col-5 text-end fw-bold">{fieldKeyLabel(key)}:</div>
                            <div className="col-7 evidence-row-value">
                                <span>
                                    <EvidenceBadge status="unknown" fieldLabel={fieldKeyLabel(key)}/>{' '}
                                    <span className="text-muted">No accepted evidence</span>
                                </span>
                                {action(key, 'Suggest a value', 'for')}
                            </div>
                        </div>
                    ))}
                </div>
            )}
            {pending.length > 0 && (
                <div className="mt-3" data-testid="pending-review">
                    <h4 className="h6 fw-semibold mb-2">Pending review</h4>
                    <p className="text-muted small mb-2">
                        Observed but not accepted. These are not verified and do not change the facts above.
                    </p>
                    {pending.map(item => (
                        <div className="row mb-2 evidence-pending-item" key={item.field_key}
                             data-testid={`pending-review-${item.field_key}`}>
                            <div className="col-5 text-end fw-bold">{fieldKeyLabel(item.field_key)}:</div>
                            <div className="col-7">
                                <EvidenceBadge status={item.review}/>
                                <span className="d-block small text-muted">
                                    Observed {formatEvidenceDate(item.observed_at)} ·{' '}
                                    {item.source_domain || 'unknown source'} · Not yet reviewed
                                </span>
                                {item.observed_value && (
                                    <span className="d-block small evidence-observed-value">
                                        Observed, not verified: “{item.observed_value}”
                                    </span>
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
};

export default EvidenceDetails;
