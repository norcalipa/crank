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
    it('says "Never checked" once, as the only chip, when nothing was ever checked', () => {
        expect(texts(evidence())).toEqual(['? Never checked']);
    });

    it('shows one muted "Not yet verified" chip when it was checked but nothing is verified or stale', () => {
        expect(texts(evidence({last_verified_at: '2026-11-20T10:00:00Z'}))).toEqual(['? Not yet verified']);
    });

    it('adds no first-vesting chip unless asked', () => {
        expect(evidenceChipSpecs(evidence({verified: 1, unknown: 6})).map(chip => chip.key)).toEqual(['verified', 'unknown']);
    });

    it('treats a missing summary as never checked', () => {
        expect(texts(undefined)).toEqual(['? Never checked']);
        expect(texts(null)).toEqual(['? Never checked']);
    });

    it('shows one chip per non-empty count with a marker and a word', () => {
        expect(texts(evidence({verified: 2, stale: 1, unknown: 4}))).toEqual(['✓ 2 verified', '! 1 stale', '? 4 unknown']);
        expect(texts(evidence({verified: 7, unknown: 0}))).toEqual(['✓ 7 verified']);
        expect(texts(evidence({stale: 3, unknown: 0}))).toEqual(['! 3 stale']);
    });

    it('adds pending review and the first-vesting profile chip when they apply', () => {
        expect(texts(evidence({verified: 1, unknown: 0, pending_review: 2}), true))
            .toEqual(['✓ 1 verified', '… Pending review', '◷ Vests < 1 yr']);
        expect(texts(undefined, true)).toEqual(['? Never checked', '◷ Vests < 1 yr']);
    });

    it('renders status classes, hidden markers and explanatory titles', () => {
        render(<EvidenceChips evidence={evidence({verified: 1, unknown: 6})} acceleratedVesting/>);
        const chips = screen.getByTestId('evidence-chips');
        expect(chips.querySelector('.crank-chip--verified')).toHaveTextContent('✓1 verified');
        expect(chips.querySelector('.crank-chip--unknown')).toHaveAttribute('title', 'No accepted evidence for this fact.');
        expect(chips.querySelector('.crank-chip--vesting')).toHaveAttribute('title', 'First vesting comes in under one year.');
        chips.querySelectorAll('.crank-chip-marker').forEach(marker => expect(marker).toHaveAttribute('aria-hidden', 'true'));
    });

    it('renders every chip and both fold chips, each naming what it folds', () => {
        render(<EvidenceChips evidence={evidence({verified: 1, stale: 1, unknown: 2, pending_review: 1})} acceleratedVesting/>);
        const chips = screen.getByTestId('evidence-chips');
        expect(chips).toHaveAttribute('data-chip-count', '5');
        expect(chips.querySelectorAll('.crank-chip:not(.crank-chip--more)')).toHaveLength(5);
        const tiny = screen.getByTestId('evidence-chips-more-tiny');
        expect(tiny).toHaveTextContent('+4');
        const narrow = screen.getByTestId('evidence-chips-more-narrow');
        expect(narrow).toHaveTextContent('+3');
        expect(narrow).toHaveAttribute('title', '2 unknown, Pending review, First vesting comes in under one year.');
        const mid = screen.getByTestId('evidence-chips-more-mid');
        expect(mid).toHaveTextContent('+2');
        expect(mid).toHaveAttribute('title', 'Pending review, First vesting comes in under one year.');
    });

    it('has no fold chip a row has nothing to fold into', () => {
        render(<EvidenceChips evidence={evidence({verified: 1, unknown: 0})}/>);
        expect(screen.queryByTestId('evidence-chips-more-tiny')).toBeNull();
        expect(screen.queryByTestId('evidence-chips-more-narrow')).toBeNull();
        expect(screen.queryByTestId('evidence-chips-more-mid')).toBeNull();
    });

    it('keeps the vesting sentence in the title of the short chip', () => {
        render(<EvidenceChips evidence={evidence({verified: 1, unknown: 0})} acceleratedVesting/>);
        expect(screen.getByTestId('evidence-chips').querySelector('.crank-chip--vesting'))
            .toHaveAttribute('title', 'First vesting comes in under one year.');
    });
});
