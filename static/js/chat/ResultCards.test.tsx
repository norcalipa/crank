// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, within} from '@testing-library/react';
import * as React from 'react';

import {resetWorkspaceForTests, registerCompanyTarget} from '../workspace/store';
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
        // The reply-time snapshot is labelled for every reader, not only screen readers.
        const label = within(evidence).getByTestId('evidence-recorded-label');
        expect(label).toHaveTextContent('Facts as of this reply');
        expect(label).not.toHaveClass('visually-hidden');
        expect(evidence.querySelector('.visually-hidden')).toBeNull();
    });

    test('an organization with no accepted evidence reads never verified', () => {
        render(<OrgCard org={org({evidence: summary()})}/>);
        const evidence = screen.getByTestId('org-evidence-7');
        expect(evidence.querySelector('.evidence-badge-unknown')).toBeInTheDocument();
        expect(within(evidence).getByTestId('evidence-last-verified')).toHaveTextContent('Never verified');
        expect(within(evidence).queryByTestId('evidence-pending')).toBeNull();
    });

    test('a reply stored before fact status existed still renders, without a guessed status', () => {
        const unreadable = [undefined, null, 'verified', {verified: 'all'}, {verified: 7}] as unknown[];
        for (const evidence of unreadable) {
            const {unmount} = render(<OrgCard org={org({evidence: evidence as OrganizationResult['evidence']})}/>);
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
        expect(within(screen.getByTestId('org-evidence-2')).getByTestId('evidence-facts'))
            .toHaveTextContent('7 verified · 0 stale · 0 unknown');
    });

    test('a stored summary never carries the "Verified" verdict, which would not age', () => {
        // Every fact was verified when the reply was written; the stored
        // counts say so with their date, without a present-tense badge.
        render(<OrgCard org={org({evidence: summary({verified: 7, unknown: 0, fact_coverage: 7,
            last_verified_at: '2026-09-01T00:00:00Z'})})}/>);
        const evidence = screen.getByTestId('org-evidence-7');
        expect(evidence.querySelector('.evidence-badge-verified')).toBeNull();
        expect(evidence.querySelector('.evidence-badge')).toBeNull();
        expect(evidence).not.toHaveTextContent('✓');
        expect(within(evidence).getByTestId('evidence-recorded-label')).toHaveTextContent('Facts as of this reply');
        expect(within(evidence).getByTestId('evidence-facts')).toHaveTextContent('7 verified · 0 stale · 0 unknown');
        expect(within(evidence).getByTestId('evidence-last-verified')).toHaveTextContent('Last verified Sep 1, 2026');
    });
});

describe('OrgCard "View company" (issue #486, AC 20)', () => {
    afterEach(() => resetWorkspaceForTests());

    const click = (element: Element, init: MouseEventInit = {}) => {
        const event = new MouseEvent('click', {bubbles: true, cancelable: true, button: 0, ...init});
        element.dispatchEvent(event);
        return event;
    };

    test('is a link to /?company=<id> beside a website link named as such', () => {
        render(<OrgCard org={org({url: 'https://acme.example.test'})}/>);
        const view = screen.getByTestId('view-company-7');
        expect(view).toHaveAttribute('href', '/?company=7');
        expect(view).toHaveAccessibleName('View company: Acme Corp');
        const site = screen.getByRole('link', {name: 'Website of Acme Corp (opens in a new tab)'});
        expect(site).toHaveAttribute('href', 'https://acme.example.test');
        expect(site).toHaveAttribute('rel', 'noopener noreferrer');
        expect(screen.queryByText(/Details/)).toBeNull();
        // The arrow is an icon from the set the view uses, not a glyph.
        expect(site.querySelector('i.fa-arrow-up-right-from-square')).toHaveAttribute('aria-hidden', 'true');
        expect(site.textContent).not.toContain('↗');
        // The in-app action is the one shaped as the main action.
        expect(view).toHaveClass('org-card-view-company');
        expect(site).not.toHaveClass('org-card-view-company');
    });

    test('a plain click opens the view through the typed target and stays on the page', () => {
        const target = jest.fn().mockReturnValue(true);
        const unregister = registerCompanyTarget(target);
        render(<OrgCard org={org()}/>);
        const event = click(screen.getByTestId('view-company-7'));
        expect(target).toHaveBeenCalledWith(7);
        expect(event.defaultPrevented).toBe(true);
        unregister();
    });

    test('without a list on the page, or when it does not show the company, the link navigates', () => {
        render(<OrgCard org={org()}/>);
        expect(click(screen.getByTestId('view-company-7')).defaultPrevented).toBe(false);
        const target = jest.fn().mockReturnValue(false);
        const unregister = registerCompanyTarget(target);
        expect(click(screen.getByTestId('view-company-7')).defaultPrevented).toBe(false);
        expect(target).toHaveBeenCalledWith(7);
        unregister();
    });

    test('a modified or non-primary click keeps the browser\'s own behaviour', () => {
        const target = jest.fn().mockReturnValue(true);
        const unregister = registerCompanyTarget(target);
        render(<OrgCard org={org()}/>);
        const link = screen.getByTestId('view-company-7');
        for (const init of [{ctrlKey: true}, {metaKey: true}, {shiftKey: true}, {altKey: true}, {button: 1}]) {
            expect(click(link, init).defaultPrevented).toBe(false);
        }
        expect(target).not.toHaveBeenCalled();
        unregister();
    });

    test('an id that cannot be a company id gets no link and no target call', () => {
        const target = jest.fn().mockReturnValue(true);
        const unregister = registerCompanyTarget(target);
        for (const id of [0, -1, 1.5, Number.NaN, 2 ** 31]) {
            const {unmount} = render(<OrgCard org={org({id})}/>);
            expect(screen.queryByRole('link', {name: /View company/})).toBeNull();
            expect(screen.getByRole('article')).toBeInTheDocument();
            unmount();
        }
        expect(target).not.toHaveBeenCalled();
        unregister();
    });
});
