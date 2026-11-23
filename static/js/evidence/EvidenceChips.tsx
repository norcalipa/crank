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

export const VESTING_CHIP_TEXT = 'Vests < 1 yr';
export const VESTING_CHIP_SENTENCE = 'First vesting comes in under one year.';

// The compact evidence read-out of a ranked row (issue #617): one chip per
// non-empty status count, each a marker plus a word so no chip is colour only.
// A company with nothing verified or stale shows the single muted
// "Not yet verified" chip, never three zero counts. Counts come from the
// rankings payload only; per-fact detail stays in the company view.
export const evidenceChipSpecs = (evidence?: EvidenceSummaryData | null, acceleratedVesting = false): ChipSpec[] => {
    const chips: ChipSpec[] = [];
    if (!evidence || evidence.verified + evidence.stale === 0) {
        // Never re-checked: one chip says it, and the row has no caption.
        chips.push({key: 'none', status: 'unknown', marker: EVIDENCE_STATUS_META.unknown.marker,
            text: evidence?.last_verified_at ? 'Not yet verified' : 'Never checked'});
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
        chips.push({key: 'vesting', status: 'vesting', marker: '◷', text: VESTING_CHIP_TEXT});
    }
    return chips;
};

// A row never wraps its chips and never cuts one (issue #617): every chip is
// rendered, and the row's container query (popup.css) folds the 3rd and later
// chips (the 4th and later in a middle width, the 2nd and later in a very
// narrow one) into a "+N" chip, whose title names them. The "+N" chips are
// always in the markup; CSS shows the one that matches the room.
const EvidenceChips: React.FC<EvidenceChipsProps> = ({evidence, acceleratedVesting}) => {
    const all = evidenceChipSpecs(evidence, acceleratedVesting);
    const more = (from: number, tier: string) => {
        const hidden = all.slice(from);
        return hidden.length > 0 && (
            <span className={`crank-chip crank-chip--more crank-chip--more-${tier}`} data-status="more"
                  data-testid={`evidence-chips-more-${tier}`}
                  title={hidden.map(chip => (chip.status === 'vesting' ? VESTING_CHIP_SENTENCE : chip.text)).join(', ')}>+{hidden.length}</span>
        );
    };
    return (
    <span className="crank-chips" data-testid="evidence-chips" data-chip-count={all.length}>
        {all.map(chip => (
            <span key={chip.key} className={`crank-chip crank-chip--${chip.status}`} data-status={chip.status}
                  title={chip.status === 'vesting' ? VESTING_CHIP_SENTENCE : EVIDENCE_STATUS_META[chip.status].meaning}>
                <span className="crank-chip-marker" aria-hidden="true">{chip.marker}</span>{chip.text}
            </span>
        ))}
        {more(1, 'tiny')}
        {more(2, 'narrow')}
        {more(3, 'mid')}
    </span>
    );
};

export default EvidenceChips;
