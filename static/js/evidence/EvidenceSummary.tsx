// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {EVIDENCE_STATUS_META, EvidenceStatusKey, EvidenceSummaryData, formatEvidenceDate} from '../labels';
import EvidenceBadge from './EvidenceBadge';

// One sentence for assistive tech. Rows and cards are role="button" with an
// aria-label, so their text is skipped in focus mode; aria-describedby points
// here so the coverage, counts and freshness are announced with the name.
export const evidenceDescription = (coverage: string, evidence?: EvidenceSummaryData | null): string => {
    if (!evidence) return `Rating coverage ${coverage}. Facts: ${EVIDENCE_STATUS_META.unknown.label}.`;
    const freshness = evidence.last_verified_at
        ? `Last verified ${formatEvidenceDate(evidence.last_verified_at)}`
        : 'Never verified';
    const pending = evidence.pending_review > 0 ? ` ${EVIDENCE_STATUS_META.pending.label}.` : '';
    return `Rating coverage ${coverage}. Facts: ${evidence.verified} verified, ${evidence.stale} stale, `
        + `${evidence.unknown} unknown. ${freshness}.${pending}`;
};

interface EvidenceSummaryProps {
    evidence?: EvidenceSummaryData | null;
    variant?: 'row' | 'card';
}

// Facts, freshness and review state for one organization, shown separately
// from rating coverage. Rows without a summary (old cache entries, fixtures)
// read as Unknown rather than as verified.
const EvidenceSummary: React.FC<EvidenceSummaryProps> = ({evidence, variant = 'row'}) => {
    if (!evidence) {
        return (
            <div className={`evidence-summary evidence-summary-${variant}`} data-testid="evidence-summary">
                <EvidenceBadge status="unknown"/>
            </div>
        );
    }
    // "Verified" only when every tracked fact is verified; a partly unknown
    // profile leads with its counts instead of a verdict.
    const headline: EvidenceStatusKey | null = evidence.stale > 0
        ? 'stale'
        : evidence.verified + evidence.stale === 0
            ? 'unknown'
            : evidence.verified === evidence.total ? 'verified' : null;
    return (
        <div className={`evidence-summary evidence-summary-${variant}`} data-testid="evidence-summary">
            {headline && <EvidenceBadge status={headline}/>}
            <span className="evidence-summary-facts fw-semibold" data-testid="evidence-facts">
                <span className="text-nowrap">{evidence.verified} verified</span>{' · '}
                <span className="text-nowrap">{evidence.stale} stale</span>{' · '}
                <span className="text-nowrap">{evidence.unknown} unknown</span>
            </span>
            <span className="evidence-summary-fresh" data-testid="evidence-last-verified">
                {evidence.last_verified_at
                    ? <>Last verified <span className="text-nowrap">{formatEvidenceDate(evidence.last_verified_at)}</span></>
                    : 'Never verified'}
            </span>
            {evidence.pending_review > 0 && (
                <EvidenceBadge status="pending" testId="evidence-pending"/>
            )}
        </div>
    );
};

export default EvidenceSummary;
