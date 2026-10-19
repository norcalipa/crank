// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {
    EVIDENCE_STATUS_META, MATCH_TERMS, REQUIREMENT_MARKS, formatScopeWords, fundingRoundLabel,
    partialScopeWords, requirementLabel, rtoPolicyLabel,
} from '../labels';
import EvidenceBadge from '../evidence/EvidenceBadge';
import {retryFrom, useHoldReaderBelow, useOwnCompanyRead} from './useOwnCompanyRead';

export interface FitRequirement {
    path: string;
    // Whether a fact about the company can decide this priority; false means it is checked for each job.
    counted?: boolean;
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
    // Confirmed met out of the priorities a company fact can decide; null when none can be.
    priorities_met: {met: number; counted: number} | null;
    company_score: number | null;
    coverage: number | null;
    requirements: FitRequirement[];
    unsupported?: string[];
    // The reader excluded this company by name; the assistant's company matches leave it out.
    excluded?: boolean;
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
    {key: 'tradeoffs', title: 'Tradeoffs', empty: 'No preference is known to be unmet.'},
    {key: 'does-not-meet', title: 'Does not meet', empty: 'No requirement is known to be unmet.'},
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
// "in-office", the in-office requirement is a number of days, and a country,
// industry, region or culture tag is the reader's own lower-case word. A value
// this does not know is printed unchanged.
const MODE_WORDS: Record<string, string> = {remote: 'Remote', hybrid: 'Hybrid', 'in-office': 'In-Office'};
const FUNDING_CODE = /^[a-z]$/i;
// Paths whose engine value is a lower-cased word of the reader's own.
const LOWER_CASE_PATHS = [
    'work_location.countries', 'geography.regions', 'industry', 'culture', 'funding_stage',
];
const ACRONYMS = new Set([
    'ai', 'ml', 'saas', 'hr', 'it', 'vr', 'ar', 'iot', 'b2b', 'b2c', 'api', 'ux', 'ui', 'us', 'usa', 'uk', 'eu', 'uae',
]);

// A priority the server does not count (`counted: false`) is decided from a job
// listing's own pay, place or tags: the company-level check has no listing.
const PER_JOB_TEXT = 'Checked for each job, not for the company. See Open jobs.';
// The words the evidence list uses for the same answer.
const NO_EVIDENCE_TEXT = 'No accepted evidence';

const capitalizeWords = (text: string): string =>
    text.replace(/[^\s\-/]+/g, word => (ACRONYMS.has(word) ? word.toUpperCase() : word.charAt(0).toUpperCase() + word.slice(1)));

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
    if (path === 'compensation.minimum_salary') {
        if (numeric) return `${(observed as number).toLocaleString('en-US')} per year`;
        if (/^[a-z]{3}$/i.test(text)) return `Pay is published in ${text.toUpperCase()}`;
    }
    if (path === 'work_location.countries' && /^[a-z]{2,3}$/.test(text)) return text.toUpperCase();
    if (LOWER_CASE_PATHS.includes(path) && text === text.toLowerCase()) return capitalizeWords(text);
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

// The first figure is a count of the rows below: how many of the reader's
// priorities that a fact about the company can decide are confirmed met. The
// server decides which priorities those are (`counted` on each row).
export const PRIORITIES_MET_TERM = MATCH_TERMS.prioritiesMet;
export const PRIORITIES_NONE_TEXT = 'No priority in the rows below can be decided from a fact about a company; '
    + 'each is checked for each job.';
// Beside a count, the priorities it leaves out, so "of 3" can be checked against five rows without the legend.
export const perJobCountText = (count: number): string => (count === 1
    ? 'One priority below is checked for each job and is not counted.'
    : `${count} priorities below are checked for each job and are not counted.`);

// The number the endpoint sends as `company_score` is the plain mean of the
// company's rating types (`_company_score`); no preset weights enter it. The
// job-match panel's own label (`MATCH_TERMS.companyScore`) is not true for it.
export const COMPANY_SCORE_TERM = {
    label: 'Average rating',
    meaning: 'The plain average of its rating types, 0 to 5, whatever the ranking preset.',
};

const figure = (value: number | null, scale: string, digits = 1): string =>
    value === null || value === undefined ? '—' : `${value.toFixed(digits)}${scale}`;

const prioritiesMetText = (fit: CompanyFit): string =>
    fit.priorities_met ? `${fit.priorities_met.met} of ${fit.priorities_met.counted}` : '—';

function Figures({fit}: {fit: CompanyFit}) {
    const coverage = fit.coverage === null || fit.coverage === undefined
        ? '—' : `${Math.round(fit.coverage * 100)}%`;
    return (
        <div className="job-match-figures company-fit-figures mb-3" role="list" aria-label="Match figures">
            <span role="listitem" className="job-match-figure">
                <span className="job-match-figure-label" title={PRIORITIES_MET_TERM.meaning}>{PRIORITIES_MET_TERM.label}</span>
                <strong className="job-match-figure-value" data-testid="fit-priorities-met">{prioritiesMetText(fit)}</strong>
            </span>
            <span role="listitem" className="job-match-figure">
                <span className="job-match-figure-label" title={COMPANY_SCORE_TERM.meaning}>{COMPANY_SCORE_TERM.label}</span>
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
                    Counted as not met: the fact on record covers {partialScopeWords(requirement.scope) ?? 'part of the company'} only.
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
                    <span data-testid={`fit-scope-${requirement.path}`}>
                        {requirement.counted === false ? PER_JOB_TEXT : NO_EVIDENCE_TEXT}
                    </span>
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

const LEGEND_MARKS = ['match', 'mismatch', 'unknown'] as const;

/**
 * The words on the three numbers, the marks and the two status words a row can
 * carry, in the visible page and not only in `title` attributes. Built from the
 * constants the job-match panel's legend reads where the meaning is shared, so
 * the two cannot drift.
 */
function FitLegend() {
    return (
        <details className="how-ranking-works company-fit-legend" data-testid="fit-legend">
            <summary className="how-ranking-works-summary">What these labels mean</summary>
            <div className="how-ranking-works-content">
                <dl className="ranking-definitions mb-0">
                    <dt>{PRIORITIES_MET_TERM.label}</dt>
                    <dd>{PRIORITIES_MET_TERM.meaning}</dd>
                    <dt>{COMPANY_SCORE_TERM.label}</dt>
                    <dd>{COMPANY_SCORE_TERM.meaning}</dd>
                    <dt>{MATCH_TERMS.requirementCoverage.label}</dt>
                    <dd>{MATCH_TERMS.requirementCoverage.meaning}</dd>
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
                            <li><EvidenceBadge status="verified"/>: {EVIDENCE_STATUS_META.verified.meaning}</li>
                            <li><span className="evidence-badge">{MATCH_TERMS.sourced.label}</span>: {MATCH_TERMS.sourced.meaning}</li>
                        </ul>
                        <p className="mb-0 mt-1">
                            {EVIDENCE_STATUS_META.verified.label} describes the fact in "Data Freshness &amp; Sources";
                            {' '}{MATCH_TERMS.sourced.label} describes how a row was decided from it.
                        </p>
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

/** Where an exclusion applies: the assistant's matches, not the ranked list. */
function ExcludedNote({target, hasRows}: {target: string; hasRows: boolean}) {
    return (
        <p className="company-fit-excluded mb-2" data-testid="fit-excluded">
            You excluded {target} in your priorities, so the assistant&rsquo;s company matches leave it out.
            Company rankings do not use your priorities.{hasRows && ' This page still shows how it fits the rest.'}
        </p>
    );
}

/**
 * How this company fits the signed-in reader's saved priorities (issue #486):
 * every requirement the reader set, in four groups, with the priorities met, company
 * score and coverage as three separately labelled numbers. It reads the reader's own
 * evaluation, so nothing here is kept outside the component.
 */
const FitSection = React.forwardRef<HTMLHeadingElement, FitSectionProps>(({
    organizationId, name, isAuthenticated, signInUrl, onEditPriorities, onSettled,
}, headingRef) => {
    const {state, retry, refreshing} = useOwnCompanyRead<CompanyFit>('fit', organizationId, isAuthenticated, isCompanyFit, true);
    const sectionRef = React.useRef<HTMLElement>(null);
    useHoldReaderBelow(sectionRef, state);
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
                <button type="button" className="btn btn-outline-light" data-testid="fit-retry" onClick={() => retryFrom(titleId, retry)}>
                    Retry<span className="visually-hidden"> loading fit</span>
                </button>
            </div>
        );
    } else if (!state.data.has_preferences || (state.data.requirements.length === 0 && (state.data.unsupported || []).length > 0)) {
        // Nothing here can be checked against a company: no priorities at all, or
        // only ones the engine cannot evaluate (with or without an exclusion).
        const unsupported = state.data.unsupported || [];
        body = unsupported.length > 0 ? (
            <div data-testid="fit-unsupported-only" aria-busy={refreshing || undefined}>
                <p className="mb-2">
                    Your saved priorities cannot be checked against a company yet: {unsupported.map(requirementLabel).join(', ')}.
                    {' '}Add another priority to see how {target} fits.
                </p>
                <button type="button" className="btn btn-outline-light" data-testid="fit-set-priorities"
                        onClick={onEditPriorities}>
                    Edit your priorities
                </button>
            </div>
        ) : (
            <div data-testid="fit-no-priorities" aria-busy={refreshing || undefined}>
                <p className="mb-2">Set your priorities to see how {target} fits.</p>
                <button type="button" className="btn btn-outline-light" data-testid="fit-set-priorities"
                        onClick={onEditPriorities}>
                    Set your priorities
                </button>
            </div>
        );
    } else if (state.data.requirements.length === 0) {
        // Only exclusions: there is no requirement to count, so no coverage and no groups.
        body = (
            <div data-testid="fit-exclusions-only" aria-busy={refreshing || undefined}>
                {state.data.excluded && <ExcludedNote target={target} hasRows={false}/>}
                <p className="mb-0">
                    Your saved priorities are only exclusions, so there is no requirement to check {target} against.
                    Exclusions apply to the assistant&rsquo;s company matches and to Job Search listings.
                </p>
            </div>
        );
    } else {
        const fit = state.data;
        body = (
            <div data-testid="fit-ready" aria-busy={refreshing || undefined}>
                {fit.excluded && <ExcludedNote target={target} hasRows/>}
                <Figures fit={fit}/>
                {!fit.priorities_met && (
                    <p className="small text-body-secondary mb-2" data-testid="fit-priorities-none">{PRIORITIES_NONE_TEXT}</p>
                )}
                {fit.priorities_met && fit.requirements.some(item => item.counted === false) && (
                    <p className="small text-body-secondary mb-2" data-testid="fit-priorities-per-job">
                        {perJobCountText(fit.requirements.filter(item => item.counted === false).length)}
                    </p>
                )}
                <FitLegend/>
                {FIT_GROUPS.map(group => {
                    const rows = fit.requirements.filter(item => fitGroupOf(item) === group.key)
                        .sort((a, b) => Number(a.counted === false) - Number(b.counted === false));
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
                {(fit.unsupported || []).length > 0 && (
                    <p className="small text-body-secondary mb-0" data-testid="fit-unsupported">
                        Not checked for a company: {(fit.unsupported || []).map(requirementLabel).join(', ')}.
                    </p>
                )}
            </div>
        );
    }

    return (
        <section ref={sectionRef} className="company-fit mt-3" data-testid="company-fit" aria-labelledby={titleId}>
            <h3 id={titleId} ref={headingRef} tabIndex={-1} className="h5 mb-2 company-jump-heading">
                Fit with your priorities
            </h3>
            {body}
        </section>
    );
});
FitSection.displayName = 'FitSection';

export default FitSection;
