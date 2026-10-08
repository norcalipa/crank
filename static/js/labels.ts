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
        meaning: 'An accepted, current source says this in its own words. Matching read it automatically and the reading has not been confirmed.',
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
