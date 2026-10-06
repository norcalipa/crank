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
