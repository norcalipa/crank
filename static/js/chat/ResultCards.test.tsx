// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, within} from '@testing-library/react';
import * as React from 'react';

import {OrgCard, ResultCards} from './ResultCards';
import type {OrganizationResult} from './types';

// Assistant organization cards show the same facts / last verified / pending
// review summary as the rankings, with the model's labels (issue #473).

const summary = (overrides: Record<string, unknown> = {}) => ({
    verified: 0, stale: 0, unknown: 7, total: 7, fact_coverage: 0, last_verified_at: null, pending_review: 0,
    ...overrides,
});

const org = (overrides: Partial<OrganizationResult> = {}): OrganizationResult => ({
    id: 7, name: 'Acme Corp', url: '', funding_round: 'O', rto_policy: 'O', ...overrides,
});

describe('OrgCard (issue #473)', () => {
    test('labels equal the model choices, not the old chat wording', () => {
        const {rerender} = render(<OrgCard org={org()}/>);
        const card = screen.getByRole('article', {name: 'Organization: Acme Corp'});
        expect(card).toHaveTextContent('Other Private · In-Office');
        expect(card).not.toHaveTextContent('IPO');
        expect(card).not.toHaveTextContent('On-site');
        rerender(<OrgCard org={org({funding_round: 'P', rto_policy: 'R'})}/>);
        expect(card).toHaveTextContent('Public · Remote');
        rerender(<OrgCard org={org({funding_round: 'X', rto_policy: 'H'})}/>);
        expect(card).toHaveTextContent('Series G or Later · Hybrid');
        expect(card).not.toHaveTextContent('Late Stage');
        // Unrecognized codes are shown as-is; empty ones are omitted.
        rerender(<OrgCard org={org({funding_round: 'Q', rto_policy: ''})}/>);
        expect(card.querySelector('.text-muted')).toHaveTextContent(/^Q$/);
    });

    test('a complete-but-stale profile reads stale, with counts and last verified', () => {
        render(<OrgCard org={org({evidence: summary({stale: 2, unknown: 5, fact_coverage: 2,
            last_verified_at: '2024-08-08T00:00:00Z', pending_review: 1})})}/>);
        const evidence = screen.getByTestId('org-evidence-7');
        expect(evidence.querySelector('.evidence-summary-card')).toBeInTheDocument();
        expect(evidence.querySelector('.evidence-badge-stale')).toHaveTextContent('Stale');
        expect(evidence.querySelector('.evidence-badge-verified')).toBeNull();
        expect(within(evidence).getByTestId('evidence-facts')).toHaveTextContent('0 verified · 2 stale · 5 unknown');
        expect(within(evidence).getByTestId('evidence-last-verified')).toHaveTextContent('Last verified Aug 8, 2024');
        expect(within(evidence).getByTestId('evidence-pending')).toHaveTextContent('Pending review');
        expect(evidence).toHaveTextContent('Fact status when this reply was written:');
    });

    test('an organization with no accepted evidence reads never verified', () => {
        render(<OrgCard org={org({evidence: summary()})}/>);
        const evidence = screen.getByTestId('org-evidence-7');
        expect(evidence.querySelector('.evidence-badge-unknown')).toBeInTheDocument();
        expect(within(evidence).getByTestId('evidence-last-verified')).toHaveTextContent('Never verified');
        expect(within(evidence).queryByTestId('evidence-pending')).toBeNull();
    });

    test('a reply stored before fact status existed still renders, without a guessed status', () => {
        for (const evidence of [undefined, null]) {
            const {unmount} = render(<OrgCard org={org({evidence})}/>);
            const note = screen.getByTestId('org-evidence-7');
            expect(note).toHaveTextContent('Fact status was not recorded for this reply.');
            expect(note.querySelector('.evidence-badge')).toBeNull();
            expect(screen.getByTestId('suggest-correction-org-7')).toBeInTheDocument();
            unmount();
        }
    });

    test('ResultCards renders old and new organization results side by side', () => {
        render(<ResultCards results={{jobs: [], organizations: [
            org({id: 1, name: 'Old Reply Co'}),
            org({id: 2, name: 'Fresh Co', evidence: summary({verified: 7, unknown: 0, fact_coverage: 7,
                last_verified_at: '2026-09-01T00:00:00Z'})}),
        ]}}/>);
        expect(screen.getByTestId('org-evidence-1')).toHaveTextContent('not recorded');
        expect(screen.getByTestId('org-evidence-2').querySelector('.evidence-badge-verified')).toBeInTheDocument();
    });
});
