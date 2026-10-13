// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.

// Human labels for the company fields that carry evidence and can be
// corrected (mirrors CompanyFieldEvidence.FieldKey on the server).
export const CORRECTABLE_FIELD_LABELS: Record<string, string> = {
    rto_policy: 'RTO Policy',
    funding_round: 'Funding Round',
    public_status: 'Public Status',
    accelerated_vesting: 'Accelerated Vesting',
    locations: 'Locations',
    company_name: 'Company Name',
    company_domain: 'Company Domain',
};

export const isCorrectableFieldKey = (key: unknown): key is string =>
    typeof key === 'string' && Object.prototype.hasOwnProperty.call(CORRECTABLE_FIELD_LABELS, key);

export const fieldKeyLabel = (key: string): string => CORRECTABLE_FIELD_LABELS[key] || key;

// Funding-round and RTO-policy words, equal to Organization.FundingRound /
// RTOPolicy on the server (pinned by crank/tests/test_frontend_label_parity.py).
// Every surface reads these; no component keeps its own map (#473).
export const FUNDING_ROUND_LABELS: Record<string, string> = {
    S: 'Seed',
    A: 'Series A',
    B: 'Series B',
    C: 'Series C',
    D: 'Series D',
    E: 'Series E',
    F: 'Series F',
    X: 'Series G or Later',
    O: 'Other Private',
    P: 'Public',
};

export const RTO_POLICY_LABELS: Record<string, string> = {
    R: 'Remote',
    H: 'Hybrid',
    O: 'In-Office',
};

const codeLabel = (labels: Record<string, string>, code: string | null | undefined, fallback: string): string =>
    (code && Object.prototype.hasOwnProperty.call(labels, code) ? labels[code] : code) || fallback;

// An unrecognized code is shown as-is; an empty one reads as `fallback`.
export const fundingRoundLabel = (code: string | null | undefined, fallback = ''): string =>
    codeLabel(FUNDING_ROUND_LABELS, code, fallback);

export const rtoPolicyLabel = (code: string | null | undefined, fallback = ''): string =>
    codeLabel(RTO_POLICY_LABELS, code, fallback);

export type EvidenceStatusKey = 'verified' | 'stale' | 'unknown' | 'profile' | 'pending' | 'conflicted';

interface EvidenceStatusMeta {
    label: string;
    marker: string;
    meaning: string;
}

// The one vocabulary for fact status. Every surface renders it through
// EvidenceBadge: a word plus a non-color marker, never color alone (#473).
export const EVIDENCE_STATUS_META: Record<EvidenceStatusKey, EvidenceStatusMeta> = {
    verified: {label: 'Verified', marker: '✓', meaning: 'Accepted and re-checked within its freshness window.'},
    stale: {label: 'Stale', marker: '!', meaning: 'Accepted, but not re-verified within its freshness window.'},
    unknown: {label: 'Unknown', marker: '?', meaning: 'No accepted evidence for this fact.'},
    profile: {label: 'Profile data', marker: '•', meaning: 'Curated profile value without field-level evidence.'},
    pending: {label: 'Pending review', marker: '…', meaning: 'An observation is waiting for review. Not verified.'},
    conflicted: {label: 'Conflicting observation', marker: '\u26A0\uFE0E', meaning: 'A later observation disagrees. Not verified.'},
};

// The first mark on a requirement chip: how the requirement came out. The
// word is the chip's spoken outcome, so a mark is never the only cue.
export const REQUIREMENT_MARKS: Record<'match' | 'mismatch' | 'unknown', EvidenceStatusMeta> = {
    match: {label: 'Match', marker: '✓', meaning: 'The requirement is met.'},
    mismatch: {label: 'Mismatch', marker: '✗', meaning: 'The requirement is not met.'},
    unknown: {label: 'Unknown', marker: '?', meaning: 'The requirement could not be decided either way, so it does not count toward requirement coverage.'},
};

// Leads a chip whose stored outcome rests on evidence that has since changed.
export const EVIDENCE_CHANGED_MARKER = '↻';

// Job-card terms that are not a fact status of their own: what a requirement
// chip rests on, and the three figures. The cards, their legend and "How
// ranking works" all read these (pinned by test_frontend_label_parity.py).
export const MATCH_TERMS: Record<string, {label: string; meaning: string}> = {
    sourced: {
        label: 'Sourced, not confirmed',
        meaning: 'An accepted, current source backs this without stating it outright: matching read it from the wording of the source or inferred it (for example, a day count from "Hybrid"), or the source covers only some locations or roles. Not confirmed.',
    },
    changed: {
        label: 'Evidence changed',
        meaning: 'The fact behind this result was replaced or removed after matches were computed. Refresh matches to re-check it.',
    },
    unqualified: {
        label: 'No qualifier',
        meaning: 'A match or mismatch with no qualifier was decided from the job listing itself.',
    },
    companyScore: {
        label: 'Company score (preset)',
        meaning: 'The preset-weighted average of the rating dimensions. It is the same for everyone.',
    },
    fit: {
        label: 'Fit',
        meaning: 'Computed from your saved priorities and never mixed into the company score.',
    },
    requirementCoverage: {
        label: 'Requirement coverage',
        meaning: 'The share of your requirements that could be decided either way. It does not mean the facts behind them are verified.',
    },
};

export interface EvidenceSummaryData {
    verified: number;
    stale: number;
    unknown: number;
    total: number;
    fact_coverage: number;
    last_verified_at: string | null;
    pending_review: number;
}

export const formatEvidenceDate = (iso: string | null | undefined, long = false): string => {
    if (!iso) return 'Unknown';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return 'Unknown';
    return date.toLocaleDateString(undefined, {
        year: 'numeric', month: long ? 'long' : 'short', day: 'numeric',
    });
};

// What a fact covers, in words (issue #486). Evidence stores a scope as raw
// keys (`countries`, `role_families`); no surface prints those keys. A fact
// with no declared scope covers the whole company.
export const COMPANY_WIDE_LABEL = 'Company-wide';

const SCOPE_WORDS: Record<string, string> = {
    countries: 'Location',
    role_families: 'Roles',
};

export const formatScopeWords = (scope: Record<string, unknown> | null | undefined): string => {
    const parts: string[] = [];
    let other = false;
    for (const [key, value] of Object.entries(scope || {})) {
        // Attribution (which domain a crawler claimed), not a limit on what the fact covers.
        if (key === 'claimed_domain') continue;
        const values = (Array.isArray(value) ? value : [value])
            .filter(item => typeof item === 'string' || typeof item === 'number')
            .map(String)
            .filter(item => item !== '');
        if (values.length === 0) continue;
        if (Object.prototype.hasOwnProperty.call(SCOPE_WORDS, key)) {
            parts.push(`${SCOPE_WORDS[key]}: ${values.join(', ')}`);
        } else {
            other = true;
        }
    }
    if (other) parts.push('Limited scope');
    return parts.length > 0 ? parts.join('; ') : COMPANY_WIDE_LABEL;
};

/** The limit of a fact that covers only part of the company, in words; null for a company-wide one. */
export const partialScopeWords = (scope: Record<string, unknown> | null | undefined): string | null => {
    const words = formatScopeWords(scope);
    return words === COMPANY_WIDE_LABEL ? null : words;
};

// The reader's own requirements, by canonical path (the labels the match
// panel's chips use). An unknown path reads as its last segment.
const REQUIREMENT_LABELS: Record<string, string> = {
    'compensation.minimum_salary': 'Minimum salary',
    'compensation.equity_minimum_percent': 'Equity',
    'compensation.require_public_company': 'Public company',
    'work_location.modes': 'Work mode',
    'work_location.countries': 'Country',
    'work_location.max_in_office_days': 'In-office days',
    'geography.regions': 'Region',
    'geography.remote_friendly': 'Remote-friendly',
    'industry': 'Industry',
    'funding_stage': 'Funding stage',
    'culture': 'Culture',
    'vesting.max_cliff_months': 'Cliff',
    'vesting.max_vesting_months': 'Vesting length',
    'vesting.prefer_accelerated': 'Accelerated vesting',
};

export const requirementLabel = (path: string): string =>
    (Object.prototype.hasOwnProperty.call(REQUIREMENT_LABELS, path) ? REQUIREMENT_LABELS[path] : '')
    || path.split('.').pop() || path;
