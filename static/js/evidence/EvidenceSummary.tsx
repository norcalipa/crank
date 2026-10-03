// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {EvidenceStatusKey, EvidenceSummaryData, formatEvidenceDate} from '../labels';
import EvidenceBadge from './EvidenceBadge';

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
                {evidence.verified} of {evidence.total} verified · {evidence.stale} stale · {evidence.unknown} unknown
            </span>
            <span className="evidence-summary-fresh" data-testid="evidence-last-verified">
                {evidence.last_verified_at
                    ? `Last verified ${formatEvidenceDate(evidence.last_verified_at)}`
                    : 'Never verified'}
            </span>
            {evidence.pending_review > 0 && (
                <EvidenceBadge status="pending" testId="evidence-pending"/>
            )}
        </div>
    );
};

export default EvidenceSummary;
