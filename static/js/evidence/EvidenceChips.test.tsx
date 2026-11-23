// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen} from '@testing-library/react';
import * as React from 'react';

import EvidenceChips, {evidenceChipSpecs} from './EvidenceChips';
import {EvidenceSummaryData} from '../labels';

const evidence = (overrides: Partial<EvidenceSummaryData> = {}): EvidenceSummaryData => ({
    verified: 0, stale: 0, unknown: 7, total: 7, fact_coverage: 0, last_verified_at: null, pending_review: 0,
    ...overrides,
});

const texts = (data?: EvidenceSummaryData | null, vesting = false) =>
    evidenceChipSpecs(data, vesting).map(chip => `${chip.marker} ${chip.text}`);

describe('EvidenceChips', () => {
    it('shows one muted "Not yet verified" chip when nothing is verified or stale', () => {
        expect(texts(evidence())).toEqual(['? Not yet verified']);
    });

    it('adds no first-vesting chip unless asked', () => {
        expect(evidenceChipSpecs(evidence({verified: 1, unknown: 6})).map(chip => chip.key)).toEqual(['verified', 'unknown']);
    });

    it('treats a missing summary as not yet verified', () => {
        expect(texts(undefined)).toEqual(['? Not yet verified']);
        expect(texts(null)).toEqual(['? Not yet verified']);
    });

    it('shows one chip per non-empty count with a marker and a word', () => {
        expect(texts(evidence({verified: 2, stale: 1, unknown: 4}))).toEqual(['✓ 2 verified', '! 1 stale', '? 4 unknown']);
        expect(texts(evidence({verified: 7, unknown: 0}))).toEqual(['✓ 7 verified']);
        expect(texts(evidence({stale: 3, unknown: 0}))).toEqual(['! 3 stale']);
    });

    it('adds pending review and the first-vesting profile chip when they apply', () => {
        expect(texts(evidence({verified: 1, unknown: 0, pending_review: 2}), true))
            .toEqual(['✓ 1 verified', '… Pending review', '◷ First vesting < 1 yr']);
        expect(texts(undefined, true)).toEqual(['? Not yet verified', '◷ First vesting < 1 yr']);
    });

    it('renders status classes, hidden markers and explanatory titles', () => {
        render(<EvidenceChips evidence={evidence({verified: 1, unknown: 6})} acceleratedVesting/>);
        const chips = screen.getByTestId('evidence-chips');
        expect(chips.querySelector('.crank-chip--verified')).toHaveTextContent('✓1 verified');
        expect(chips.querySelector('.crank-chip--unknown')).toHaveAttribute('title', 'No accepted evidence for this fact.');
        expect(chips.querySelector('.crank-chip--vesting')).toHaveAttribute('title', 'First vesting comes in under one year.');
        chips.querySelectorAll('.crank-chip-marker').forEach(marker => expect(marker).toHaveAttribute('aria-hidden', 'true'));
    });

    it('folds chips past three into one "+N" chip that names them', () => {
        render(<EvidenceChips evidence={evidence({verified: 1, stale: 1, unknown: 2, pending_review: 1})} acceleratedVesting/>);
        const chips = screen.getByTestId('evidence-chips');
        expect(chips.querySelectorAll('.crank-chip')).toHaveLength(3);
        const more = screen.getByTestId('evidence-chips-more');
        expect(more).toHaveTextContent('+3');
        expect(more).toHaveAttribute('title', '2 unknown, Pending review, First vesting < 1 yr');
    });
});
