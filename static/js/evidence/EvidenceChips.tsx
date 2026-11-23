// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {EVIDENCE_STATUS_META, EvidenceStatusKey, EvidenceSummaryData} from '../labels';

interface EvidenceChipsProps {
    evidence?: EvidenceSummaryData | null;
    /** The company's first vesting comes in under a year (a profile fact, not evidence). */
    acceleratedVesting?: boolean;
}

interface ChipSpec {
    key: string;
    status: EvidenceStatusKey | 'vesting';
    marker: string;
    text: string;
}

// The compact evidence read-out of a ranked row (issue #617): one chip per
// non-empty status count, each a marker plus a word so no chip is colour only.
// A company with nothing verified or stale shows the single muted
// "Not yet verified" chip, never three zero counts. Counts come from the
// rankings payload only; per-fact detail stays in the company view.
export const evidenceChipSpecs = (evidence?: EvidenceSummaryData | null, acceleratedVesting = false): ChipSpec[] => {
    const chips: ChipSpec[] = [];
    if (!evidence || evidence.verified + evidence.stale === 0) {
        chips.push({key: 'none', status: 'unknown', marker: EVIDENCE_STATUS_META.unknown.marker, text: 'Not yet verified'});
    } else {
        if (evidence.verified > 0) {
            chips.push({key: 'verified', status: 'verified', marker: EVIDENCE_STATUS_META.verified.marker, text: `${evidence.verified} verified`});
        }
        if (evidence.stale > 0) {
            chips.push({key: 'stale', status: 'stale', marker: EVIDENCE_STATUS_META.stale.marker, text: `${evidence.stale} stale`});
        }
        if (evidence.unknown > 0) {
            chips.push({key: 'unknown', status: 'unknown', marker: EVIDENCE_STATUS_META.unknown.marker, text: `${evidence.unknown} unknown`});
        }
    }
    if (evidence && evidence.pending_review > 0) {
        chips.push({key: 'pending', status: 'pending', marker: EVIDENCE_STATUS_META.pending.marker, text: EVIDENCE_STATUS_META.pending.label});
    }
    if (acceleratedVesting) {
        chips.push({key: 'vesting', status: 'vesting', marker: '✓', text: 'First vesting < 1 yr'});
    }
    return chips;
};

const EvidenceChips: React.FC<EvidenceChipsProps> = ({evidence, acceleratedVesting}) => (
    <span className="crank-chips" data-testid="evidence-chips">
        {evidenceChipSpecs(evidence, acceleratedVesting).map(chip => (
            <span key={chip.key} className={`crank-chip crank-chip--${chip.status}`} data-status={chip.status}
                  title={chip.status === 'vesting' ? 'First vesting comes in under one year.' : EVIDENCE_STATUS_META[chip.status].meaning}>
                <span className="crank-chip-marker" aria-hidden="true">{chip.marker}</span>{chip.text}
            </span>
        ))}
    </span>
);

export default EvidenceChips;
