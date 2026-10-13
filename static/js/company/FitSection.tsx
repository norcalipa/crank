// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {
    COMPANY_WIDE_LABEL, EVIDENCE_STATUS_META, MATCH_TERMS, REQUIREMENT_MARKS, formatScopeWords, fundingRoundLabel,
    requirementLabel, rtoPolicyLabel,
} from '../labels';
import {useOwnCompanyRead} from './useOwnCompanyRead';

export interface FitRequirement {
    path: string;
    status: 'match' | 'mismatch' | 'unknown';
    hard: boolean;
    observed?: unknown;
    scope_ok?: boolean;
    scope?: Record<string, unknown> | null;
    evidence_status?: {state?: string; last_verified_at?: string | null} | null;
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
    {key: 'meets', title: 'Meets', empty: 'None of your priorities are confirmed as met.'},
    {key: 'tradeoffs', title: 'Tradeoffs', empty: 'No preferences are missed.'},
    {key: 'does-not-meet', title: 'Does not meet', empty: 'No hard requirements are missed.'},
    {key: 'unknown', title: 'Unknown', empty: 'Every priority could be decided.'},
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
    const evidence = evidenceText(requirement);
    const outOfScope = requirement.scope_ok === false;
    const scope = requirement.scope ? formatScopeWords(requirement.scope) : COMPANY_WIDE_LABEL;
    return (
        <li className="company-fit-row" data-testid={`fit-row-${requirement.path}`} data-group={group}
            data-status={requirement.status}>
            <span className="company-fit-label">
                <span aria-hidden="true">{mark.marker}</span> {requirementLabel(requirement.path)}
            </span>
            <span className="company-fit-value">{observedText(requirement)}</span>
            <span className="company-fit-meta small text-muted">
                {evidence && <span data-testid={`fit-evidence-${requirement.path}`}>{evidence}</span>}
                {evidence && ' · '}
                <span data-testid={`fit-scope-${requirement.path}`}><span className="visually-hidden">Covers: </span>{scope}</span>
            </span>
            {outOfScope && (
                <span className="company-fit-out-of-scope small" data-testid={`fit-out-of-scope-${requirement.path}`}>
                    Not counted: this fact covers {scope}, which does not include you.
                </span>
            )}
        </li>
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
}

/**
 * How this company fits the signed-in reader's saved priorities (issue #486):
 * every requirement the reader set, in four groups, with the fit, company score
 * and coverage as three separately labelled numbers. It reads the reader's own
 * evaluation, so nothing here is kept outside the component.
 */
const FitSection: React.FC<FitSectionProps> = ({
    organizationId, name, isAuthenticated, signInUrl, onEditPriorities,
}) => {
    const {state, retry} = useOwnCompanyRead<CompanyFit>('fit', organizationId, isAuthenticated, isCompanyFit);
    const target = name.length > 40 ? 'this company' : name;
    const titleId = 'company-fit-title';

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
                <button type="button" className="btn btn-outline-light" data-testid="fit-retry" onClick={retry}>Retry</button>
            </div>
        );
    } else if (!state.data.has_preferences) {
        body = (
            <div data-testid="fit-no-priorities">
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
            <div data-testid="fit-ready">
                <Figures fit={fit}/>
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
            <h3 id={titleId} className="h5 mb-2">Fit with your priorities</h3>
            {body}
        </section>
    );
};

export default FitSection;
