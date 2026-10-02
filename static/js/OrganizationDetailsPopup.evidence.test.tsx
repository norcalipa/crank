// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, waitFor} from '@testing-library/react';
import * as React from 'react';

import OrganizationDetailsPopup from './OrganizationDetailsPopup';
import * as suggestCompanyController from './suggestCompany/controller';
import {resetWorkspaceForTests} from './workspace/store';

const organization = {
    id: 1, name: 'Beta Corp', ranking: 1, avg_score: 4, funding_round: 'S', rto_policy: 'R',
    profile_completeness: 50, accelerated_vesting: false, url: 'https://beta.example', public: true,
    avg_scores: [], rating_dimensions_total: 2, rating_dimensions_covered: 1,
};

const field = (key: string, overrides: Record<string, unknown> = {}) => ({
    field_key: key, state: 'accepted', value: 'Remote first', source_domain: 'example.com',
    source_url: 'https://example.com/about', observed_at: '2025-01-10T12:00:00Z', scope: {},
    last_checked_at: '2025-01-10T12:00:00Z', last_successful_fetch_at: null, last_changed_at: null,
    last_verified_at: '2025-01-10T12:00:00Z', stale: false, status: 'verified', review: 'none', policy_days: 90,
    ...overrides,
});

const payload = (overrides: Record<string, unknown> = {}) => ({
    organization_id: 1, organization_modified: '2025-03-01T00:00:00Z', organization_created: '2024-06-01T00:00:00Z',
    latest_observation: null, evidence_schema: 2,
    fields: [field('rto_policy', {status: 'stale', stale: true, review: 'conflicted'})],
    unverified_fields: ['funding_round'],
    pending_review: [{field_key: 'rto_policy', review: 'conflicted', observed_at: '2025-03-01T00:00:00Z',
        source_domain: 'rival.com', observed_value: 'In office'}],
    summary: {verified: 0, stale: 1, unknown: 6, total: 7, fact_coverage: 1,
        last_verified_at: '2025-01-10T12:00:00Z', pending_review: 1},
    ...overrides,
});

const mockProvenance = (handler: () => Promise<unknown>) => {
    global.fetch = jest.fn().mockImplementation((url: string) =>
        url.includes('/provenance/') ? handler() : Promise.resolve({json: () => Promise.resolve([])}));
};

const ok = (body: unknown) => Promise.resolve({ok: true, json: () => Promise.resolve(body)});

const open = (props: Record<string, unknown> = {}) =>
    render(<OrganizationDetailsPopup organization={organization} visible={true} onClose={() => {}} {...props}/>);

describe('OrganizationDetailsPopup evidence status (#473)', () => {
    beforeEach(() => {
        resetWorkspaceForTests();
    });

    test('shows coverage, the stale row, the pending-review subsection and a safe source link', async () => {
        mockProvenance(() => ok(payload()));
        open({isAuthenticated: true});
        expect(await screen.findByTestId('coverage-summary')).toBeInTheDocument();
        expect(screen.getByTestId('rating-coverage')).toHaveTextContent('1 of 2 rating dimensions');
        expect(screen.getByTestId('last-updated').closest('.row')).toHaveTextContent('Record last edited');
        expect(screen.getByText('Editing the record does not re-verify facts.')).toBeInTheDocument();
        expect(screen.getByTestId('field-stale-rto_policy')).toBeInTheDocument();
        expect(screen.getByTestId('field-review-rto_policy')).toHaveTextContent('Conflicting observation');
        const link = screen.getByTestId('field-source-link-rto_policy');
        expect(link).toHaveAttribute('rel', 'noopener noreferrer nofollow');
        expect(link).toHaveAttribute('target', '_blank');
        const pending = screen.getByTestId('pending-review-rto_policy');
        expect(pending).toHaveTextContent('Observed, not verified');
        expect(pending.querySelector('.evidence-badge-verified')).toBeNull();
        expect(screen.getByTestId('field-unverified-funding_round')).toHaveTextContent('No accepted evidence');
        expect(screen.getByTestId('suggest-correction-field-rto_policy')).toBeInTheDocument();
    });

    test('falls back to the completeness percentage without dimension counts', async () => {
        mockProvenance(() => ok(payload()));
        open({organization: {...organization, rating_dimensions_total: undefined, rating_dimensions_covered: undefined}});
        await screen.findByTestId('coverage-summary');
        expect(screen.getByTestId('rating-coverage')).toHaveTextContent('50% of rating dimensions');
    });

    test('shows the shaped skeleton while loading', async () => {
        mockProvenance(() => new Promise(() => {}));
        open();
        expect(await screen.findByTestId('provenance-loading')).toBeInTheDocument();
    });

    test('empty evidence explains itself and offers the primary action', async () => {
        mockProvenance(() => ok(payload({fields: [], unverified_fields: [], pending_review: [],
            summary: {verified: 0, stale: 0, unknown: 7, total: 7, fact_coverage: 0, last_verified_at: null, pending_review: 0}})));
        open({isAuthenticated: true});
        expect(await screen.findByTestId('evidence-empty')).toBeInTheDocument();
        expect(screen.getByTestId('evidence-empty-action')).toHaveTextContent('Suggest a correction');
    });

    test('a failed provenance request shows an alert with Retry that refetches', async () => {
        const consoleSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
        let calls = 0;
        mockProvenance(() => {
            calls += 1;
            return calls === 1 ? Promise.resolve({ok: false, status: 503, json: () => Promise.resolve({})}) : ok(payload());
        });
        open();
        const alert = await screen.findByTestId('provenance-unavailable');
        expect(alert).toHaveAttribute('role', 'alert');
        expect(alert).toHaveTextContent('Evidence unavailable — try again');
        fireEvent.click(screen.getByTestId('provenance-retry'));
        expect(await screen.findByTestId('coverage-summary')).toBeInTheDocument();
        expect(screen.queryByTestId('provenance-unavailable')).toBeNull();
        expect(calls).toBe(2);
        await waitFor(() => expect(consoleSpy).toHaveBeenCalled());
        consoleSpy.mockRestore();
    });

    const emptyPayload = () => payload({fields: [], unverified_fields: [], pending_review: [],
        summary: {verified: 0, stale: 0, unknown: 7, total: 7, fact_coverage: 0, last_verified_at: null, pending_review: 0}});

    test('the empty-state action opens the correction flow when signed in', async () => {
        const spy = jest.spyOn(suggestCompanyController, 'openSuggestCompany').mockImplementation(() => {});
        mockProvenance(() => ok(emptyPayload()));
        open({isAuthenticated: true});
        fireEvent.click(await screen.findByTestId('evidence-empty-action'));
        expect(spy).toHaveBeenCalled();
        spy.mockRestore();
    });

    test('the empty-state action links to sign-in when anonymous', async () => {
        mockProvenance(() => ok(emptyPayload()));
        open({signInUrlTemplate: '/accounts/login/?next=/c/__COMPANY_ID__/'});
        const action = await screen.findByTestId('evidence-empty-action');
        expect(action).toHaveAttribute('href', '/accounts/login/?next=/c/1/');
    });
});
