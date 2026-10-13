// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {
    EVIDENCE_CHANGED_MARKER, EVIDENCE_STATUS_META, MATCH_TERMS, REQUIREMENT_MARKS, formatScopeWords, fundingRoundLabel,
    partialScopeWords, requirementLabel, rtoPolicyLabel,
} from '../labels';
import EvidenceBadge from '../evidence/EvidenceBadge';
import {useOwnCompanyRead} from './useOwnCompanyRead';

export interface FitRequirement {
    path: string;
    status: 'match' | 'mismatch' | 'unknown';
    hard: boolean;
    observed?: unknown;
    scope_ok?: boolean;
    scope?: Record<string, unknown> | null;
    evidence_status?: {state?: string; last_verified_at?: string | null} | null;
    source_kind?: string | null;
}

export interface CompanyFit {
    organization_id: number;
    has_preferences: boolean;
    fit_score: number | null;
    company_score: number | null;
    coverage: number | null;
    requirements: FitRequirement[];
    unsupported?: string[];
}

const isCompanyFit = (data: unknown): data is CompanyFit => {
    const fit = data as CompanyFit | null;
    return !!fit && typeof fit === 'object' && typeof fit.has_preferences === 'boolean'
        && Array.isArray(fit.requirements);
};

export type FitGroupKey = 'meets' | 'tradeoffs' | 'does-not-meet' | 'unknown';

interface FitGroupMeta {
    key: FitGroupKey;
    title: string;
    empty: string;
}

export const FIT_GROUPS: FitGroupMeta[] = [
    {key: 'meets', title: 'Meets', empty: 'No priority is confirmed as met.'},
    {key: 'tradeoffs', title: 'Tradeoffs', empty: 'No preference is unmet.'},
    {key: 'does-not-meet', title: 'Does not meet', empty: 'No requirement is unmet.'},
    {key: 'unknown', title: 'Unknown', empty: 'Nothing is undecided.'},
];

/**
 * The group one requirement belongs to. A fact whose scope does not cover the
 * reader is never a match, whatever its status says.
 */
export function fitGroupOf(requirement: FitRequirement): FitGroupKey {
    if (requirement.status === 'match' && requirement.scope_ok !== false) return 'meets';
    if (requirement.status === 'mismatch') return requirement.hard ? 'does-not-meet' : 'tradeoffs';
    return 'unknown';
}

// What the matching engine sends as `observed`, per requirement path (see the
// table in the PR body). The engine's own vocabulary is not a reader's: a
// funding stage is one lower-case letter, a work mode is "remote" / "hybrid" /
// "in-office", the in-office requirement is a number of days. A value this does
// not know is printed unchanged.
const MODE_WORDS: Record<string, string> = {remote: 'Remote', hybrid: 'Hybrid', 'in-office': 'In-Office'};
const FUNDING_CODE = /^[a-z]$/i;

const countText = (count: number, unit: string, suffix = ''): string =>
    `${count} ${unit}${count === 1 ? '' : 's'}${suffix}`;

export function observedText(requirement: FitRequirement): string {
    const {observed, path} = requirement;
    if (observed === null || observed === undefined || observed === '') return 'Unknown';
    if (typeof observed === 'boolean') return observed ? 'Yes' : 'No';
    if (Array.isArray(observed)) return observed.length > 0 ? observed.map(String).join(', ') : 'Unknown';
    if (typeof observed === 'object') return 'Unknown';
    const text = String(observed);
    const numeric = typeof observed === 'number';
    if ((path === 'funding_stage' || path === 'compensation.require_public_company') && FUNDING_CODE.test(text)) {
        return fundingRoundLabel(text.toUpperCase());
    }
    if (path === 'work_location.modes') {
        const key = text.trim().toLowerCase();
        if (Object.prototype.hasOwnProperty.call(MODE_WORDS, key)) return MODE_WORDS[key];
        return rtoPolicyLabel(text);
    }
    if (path === 'work_location.max_in_office_days' && numeric) {
        return countText(observed as number, 'day', ' a week in the office');
    }
    if ((path === 'vesting.max_cliff_months' || path === 'vesting.max_vesting_months') && numeric) {
        return countText(observed as number, 'month');
    }
    if (path === 'compensation.equity_minimum_percent' && numeric) return `${observed}%`;
    return text;
}

function evidenceText(requirement: FitRequirement): string | null {
    const state = requirement.evidence_status?.state;
    if (!state) return null;
    if (state === 'verified' || state === 'stale' || state === 'profile' || state === 'unknown'
        || state === 'pending' || state === 'conflicted') {
        return EVIDENCE_STATUS_META[state].label;
    }
    if (state === 'sourced') return MATCH_TERMS.sourced.label;
    if (state === 'superseded' || state === 'missing') return MATCH_TERMS.changed.label;
    return null;
}

const figure = (value: number | null, scale: string, digits = 1): string =>
    value === null || value === undefined ? '—' : `${value.toFixed(digits)}${scale}`;

function Figures({fit}: {fit: CompanyFit}) {
    const coverage = fit.coverage === null || fit.coverage === undefined
        ? '—' : `${Math.round(fit.coverage * 100)}%`;
    return (
        <div className="job-match-figures company-fit-figures mb-3" role="list" aria-label="Match figures">
            <span role="listitem" className="job-match-figure">
                <span className="job-match-figure-label" title={MATCH_TERMS.fit.meaning}>{MATCH_TERMS.fit.label}</span>
                <strong className="job-match-figure-value" data-testid="fit-score">{figure(fit.fit_score, ' / 100')}</strong>
            </span>
            <span role="listitem" className="job-match-figure">
                <span className="job-match-figure-label" title={MATCH_TERMS.companyScore.meaning}>{MATCH_TERMS.companyScore.label}</span>
                <strong className="job-match-figure-value" data-testid="fit-company-score">{figure(fit.company_score, ' / 5')}</strong>
            </span>
            <span role="listitem" className="job-match-figure">
                <span className="job-match-figure-label" title={MATCH_TERMS.requirementCoverage.meaning}>{MATCH_TERMS.requirementCoverage.label}</span>
                <strong className="job-match-figure-value" data-testid="fit-coverage">{coverage}</strong>
            </span>
        </div>
    );
}

function FitRow({requirement}: {requirement: FitRequirement}) {
    const group = fitGroupOf(requirement);
    const mark = REQUIREMENT_MARKS[requirement.status] || REQUIREMENT_MARKS.unknown;
    const label = (
        <span className="company-fit-label">
            <span aria-hidden="true">{mark.marker}</span> {requirementLabel(requirement.path)}
        </span>
    );
    const rowProps = {
        className: 'company-fit-row', 'data-testid': `fit-row-${requirement.path}`, 'data-group': group,
        'data-status': requirement.status,
    };
    if (requirement.scope_ok === false) {
        // One sentence: no value, no status word, and the scope is said once.
        return (
            <li {...rowProps}>
                {label}
                <span className="company-fit-out-of-scope small" data-testid={`fit-out-of-scope-${requirement.path}`}>
                    Not counted for you: the fact on record covers {partialScopeWords(requirement.scope) ?? 'part of the company'} only.
                </span>
            </li>
        );
    }
    const evidence = evidenceText(requirement);
    const noFact = !requirement.evidence_status && !requirement.source_kind;
    return (
        <li {...rowProps}>
            {label}
            <span className="company-fit-value">{observedText(requirement)}</span>
            <span className="company-fit-meta small text-muted">
                {noFact ? (
                    <span data-testid={`fit-scope-${requirement.path}`}>No fact on record</span>
                ) : (
                    <>
                        {evidence && <span data-testid={`fit-evidence-${requirement.path}`}>{evidence}</span>}
                        {evidence && ' · '}
                        <span data-testid={`fit-scope-${requirement.path}`}>
                            <span className="visually-hidden">Covers: </span>{formatScopeWords(requirement.scope)}
                        </span>
                    </>
                )}
            </span>
        </li>
    );
}

const LEGEND_STATUSES = ['verified', 'stale', 'profile'] as const;
const LEGEND_MARKS = ['match', 'mismatch', 'unknown'] as const;

/**
 * The words on the three numbers, the marks and the status words, in the
 * visible page and not only in `title` attributes. Built from the constants the
 * job-match panel's legend reads, so the two cannot drift.
 */
function FitLegend() {
    return (
        <details className="how-ranking-works company-fit-legend" data-testid="fit-legend">
            <summary className="how-ranking-works-summary">What these labels mean</summary>
            <div className="how-ranking-works-content">
                <dl className="ranking-definitions mb-0">
                    {[MATCH_TERMS.fit, MATCH_TERMS.companyScore, MATCH_TERMS.requirementCoverage].map(term => (
                        <React.Fragment key={term.label}>
                            <dt>{term.label}</dt>
                            <dd>{term.meaning}</dd>
                        </React.Fragment>
                    ))}
                    <dt>Why every priority can be met and the fit still be low</dt>
                    <dd>
                        Fit is a weighted score out of 100, not the share of your priorities that are met. Coverage
                        only says how many of your priorities could be decided, so it can be 100% while the fit is low.
                    </dd>
                    <dt>Requirement and preference</dt>
                    <dd>
                        A requirement is a priority you marked as a requirement: when it is not met it is listed under
                        Does not meet. A preference is one you marked as a preference: when it is not met it is listed
                        under Tradeoffs.
                    </dd>
                    <dt>Marks</dt>
                    <dd>
                        <ul className="evidence-legend list-unstyled mb-0" data-testid="fit-mark-legend">
                            {LEGEND_MARKS.map(key => (
                                <li key={key}>
                                    <span className="evidence-badge">
                                        <span className="evidence-badge-marker" aria-hidden="true">{REQUIREMENT_MARKS[key].marker}</span>
                                        {' '}{REQUIREMENT_MARKS[key].label}
                                    </span>: {REQUIREMENT_MARKS[key].meaning}
                                </li>
                            ))}
                        </ul>
                    </dd>
                    <dt>Evidence behind a row</dt>
                    <dd>
                        <ul className="evidence-legend list-unstyled mb-0">
                            {LEGEND_STATUSES.map(status => (
                                <li key={status}><EvidenceBadge status={status}/>: {EVIDENCE_STATUS_META[status].meaning}</li>
                            ))}
                            <li><span className="evidence-badge">{MATCH_TERMS.sourced.label}</span>: {MATCH_TERMS.sourced.meaning}</li>
                            <li>
                                <span className="evidence-badge">
                                    <span className="evidence-badge-marker" aria-hidden="true">{EVIDENCE_CHANGED_MARKER}</span>
                                    {' '}{MATCH_TERMS.changed.label}
                                </span>: {MATCH_TERMS.changed.meaning}
                            </li>
                        </ul>
                    </dd>
                    <dt>Why a fact is {EVIDENCE_STATUS_META.verified.label} below and {MATCH_TERMS.sourced.label} here</dt>
                    <dd>
                        "Data Freshness &amp; Sources" says whether the fact itself was accepted and re-checked. A row
                        here says how this priority was decided from it: read from a source's wording, inferred, or
                        taken from a source that covers only part of the company, a priority is {MATCH_TERMS.sourced.label}
                        even when the fact is {EVIDENCE_STATUS_META.verified.label}.
                    </dd>
                </dl>
            </div>
        </details>
    );
}

export interface FitSectionProps {
    organizationId: number;
    name: string;
    isAuthenticated: boolean;
    // Server-built sign-in link back to this company; null when there is none.
    signInUrl: string | null;
    // Opens the priorities editor.
    onEditPriorities: () => void;
    // The read finished (or a re-read replaced what is shown). The view uses it
    // to keep a jump heading on screen when the content above it changed height.
    onSettled?: () => void;
}

/**
 * How this company fits the signed-in reader's saved priorities (issue #486):
 * every requirement the reader set, in four groups, with the fit, company score
 * and coverage as three separately labelled numbers. It reads the reader's own
 * evaluation, so nothing here is kept outside the component.
 */
const FitSection = React.forwardRef<HTMLHeadingElement, FitSectionProps>(({
    organizationId, name, isAuthenticated, signInUrl, onEditPriorities, onSettled,
}, headingRef) => {
    const {state, retry, refreshing} = useOwnCompanyRead<CompanyFit>('fit', organizationId, isAuthenticated, isCompanyFit, true);
    const target = name.length > 40 ? 'this company' : name;
    const titleId = 'company-fit-title';

    React.useLayoutEffect(() => {
        if (state.kind !== 'loading') onSettled?.();
    }, [state, onSettled]);

    let body: React.ReactNode;
    if (!isAuthenticated || state.kind === 'signed-out') {
        body = (
            <p className="mb-0" data-testid="fit-sign-in">
                {signInUrl
                    ? <a href={signInUrl}>Sign in to see how {target} fits your priorities.</a>
                    : `Sign in to see how ${target} fits your priorities.`}
            </p>
        );
    } else if (state.kind === 'loading') {
        body = (
            <div aria-busy="true" role="status" data-testid="fit-loading">
                <p className="text-body-secondary mb-2">Checking {target} against your priorities…</p>
                <div className="company-fit-skeleton" aria-hidden="true"></div>
            </div>
        );
    } else if (state.kind === 'error') {
        body = (
            <div className="alert alert-warning mb-0" role="alert" data-testid="fit-error">
                <p className="mb-2">Fit could not be loaded.</p>
                <button type="button" className="btn btn-outline-light" data-testid="fit-retry" onClick={retry}>
                    Retry<span className="visually-hidden"> loading fit</span>
                </button>
            </div>
        );
    } else if (!state.data.has_preferences) {
        body = (
            <div data-testid="fit-no-priorities" aria-busy={refreshing || undefined}>
                <p className="mb-2">Set your priorities to see how {target} fits.</p>
                <button type="button" className="btn btn-outline-light" data-testid="fit-set-priorities"
                        onClick={onEditPriorities}>
                    Set your priorities
                </button>
            </div>
        );
    } else {
        const fit = state.data;
        body = (
            <div data-testid="fit-ready" aria-busy={refreshing || undefined}>
                <Figures fit={fit}/>
                <FitLegend/>
                {FIT_GROUPS.map(group => {
                    const rows = fit.requirements.filter(item => fitGroupOf(item) === group.key);
                    const headingId = `company-fit-${group.key}`;
                    return (
                        <div key={group.key} className="company-fit-group" data-testid={`fit-group-${group.key}`}>
                            <h4 id={headingId} className="h6 fw-semibold mb-1">
                                {group.title} <span className="text-body-secondary fw-normal">({rows.length})</span>
                            </h4>
                            {rows.length > 0 ? (
                                <ul className="list-unstyled company-fit-list mb-2" aria-labelledby={headingId}>
                                    {rows.map(row => <FitRow key={row.path} requirement={row}/>)}
                                </ul>
                            ) : (
                                <p className="small text-body-secondary mb-2" data-testid={`fit-empty-${group.key}`}>{group.empty}</p>
                            )}
                        </div>
                    );
                })}
            </div>
        );
    }

    return (
        <section className="company-fit mt-3" data-testid="company-fit" aria-labelledby={titleId}>
            <h3 id={titleId} ref={headingRef} tabIndex={-1} className="h5 mb-2 company-jump-heading">
                Fit with your priorities
            </h3>
            {body}
        </section>
    );
});
FitSection.displayName = 'FitSection';

export default FitSection;
