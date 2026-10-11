// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {fireEvent, render, screen, waitFor} from '@testing-library/react';
import * as fs from 'fs';
import * as path from 'path';
import * as React from 'react';

import OrganizationDetailsPopup from '../OrganizationDetailsPopup';
import CompanyDetails, {CompanyDetailsOrganization} from './CompanyDetails';

const company = (id: number, overrides: Partial<CompanyDetailsOrganization> = {}): CompanyDetailsOrganization => ({
    id, name: `Company ${id}`, ranking: id, avg_score: 4.2, funding_round: 'S', rto_policy: 'R',
    profile_completeness: 85, accelerated_vesting: true, type: 'C', url: `https://company-${id}.example`,
    gives_ratings: true, public: true,
    ...overrides,
});

const provenance = (id: number, overrides: Record<string, unknown> = {}) => ({
    organization_id: id, organization_modified: '2025-01-15T10:00:00Z', organization_created: '2024-06-01T00:00:00Z',
    latest_observation: null,
    fields: [{
        field_key: 'rto_policy', state: 'accepted', value: 'Remote first', source_domain: 'example.com',
        observed_at: '2025-01-10T12:00:00Z', scope: {}, last_checked_at: null, last_successful_fetch_at: null,
        last_changed_at: null, last_verified_at: '2025-01-10T12:00:00Z', stale: false,
    }],
    ...overrides,
});

type Reply = {ok: boolean; status: number; json: () => Promise<unknown>};
const reply = (body: unknown, status = 200): Reply => (
    {ok: status < 400, status, json: () => Promise.resolve(body)}
);

// One answer per URL fragment; the first fragment found in the URL wins.
const mockFetch = (routes: Array<[string, Reply | Promise<Reply>]>) => {
    global.fetch = jest.fn().mockImplementation((url: string) => {
        const route = routes.find(([fragment]) => url.includes(fragment));
        return route ? Promise.resolve(route[1]) : Promise.reject(new Error(`Fetch not mocked: ${url}`));
    });
};

const loaded = () => waitFor(() => {
    expect(screen.queryByTestId('scores-loading')).not.toBeInTheDocument();
    expect(screen.queryByTestId('provenance-loading')).not.toBeInTheDocument();
});

describe('CompanyDetails', () => {
    let errorSpy: jest.SpyInstance;

    beforeEach(() => {
        errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
        mockFetch([
            ['/api/organizations/1/scores/', reply([{type__name: 'Culture', avg_score: 4.5}])],
            ['/api/organizations/1/provenance/', reply(provenance(1))],
            ['/api/company-corrections/', reply({corrections: []})],
        ]);
    });

    afterEach(() => {
        document.getElementById('assistant-workspace')?.remove();
        errorSpy.mockRestore();
    });

    const renderDetails = (props: Partial<React.ComponentProps<typeof CompanyDetails>> = {}) => {
        const onAsk = jest.fn();
        const onSuggestCorrection = jest.fn();
        const view = render(
            <CompanyDetails organization={company(1)} isAuthenticated onAsk={onAsk}
                            onSuggestCorrection={onSuggestCorrection} {...props}/>
        );
        return {...view, onAsk, onSuggestCorrection};
    };

    const addWorkspaceHost = () => {
        const host = document.createElement('div');
        host.id = 'assistant-workspace';
        document.body.appendChild(host);
    };

    test('renders the content in place, without a dialog, an overlay or a portal', async () => {
        const {container} = renderDetails();
        await loaded();

        // Everything is inside the element it was rendered into.
        expect(container.firstElementChild).toHaveClass('card-body');
        expect(container).toContainElement(screen.getByTestId('company-handoff-actions'));
        expect(container).toContainElement(screen.getByTestId('popup-details-grid'));
        expect(container).toContainElement(screen.getByTestId('popup-details-score-card'));
        expect(container).toContainElement(screen.getByTestId('provenance-section'));
        expect(screen.getByText('Culture')).toBeInTheDocument();
        expect(screen.getByRole('heading', {name: 'Data Freshness & Sources'})).toBeInTheDocument();

        expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
        expect(document.querySelector('[aria-modal]')).toBeNull();
        expect(document.querySelector('.popup-overlay')).toBeNull();
        expect(screen.queryByRole('button', {name: 'Close'})).not.toBeInTheDocument();
        // No background lock: nothing outside the content was made inert.
        expect(document.querySelector('[inert]')).toBeNull();
        expect(document.documentElement.style.overflow).toBe('');
    });

    test('the sections keep the order they have in the dialog', async () => {
        const {container} = renderDetails();
        await loaded();

        const ids = Array.from(container.querySelectorAll('[data-testid]'))
            .map(node => node.getAttribute('data-testid'));
        const order = ['company-handoff-actions', 'company-chat-cta', 'popup-details-grid',
            'popup-details-score-card', 'rating-coverage', 'provenance-section', 'last-updated',
            'added-to-catalog', 'no-observation', 'correction-action', 'suggest-correction-link'];
        expect(ids.filter(id => order.includes(id as string))).toEqual(order);
    });

    test('the module reaches neither the modal lock nor a portal', () => {
        for (const file of ['CompanyDetails.tsx', 'useCompanyData.ts']) {
            const source = fs.readFileSync(path.resolve(__dirname, file), 'utf8');
            expect(source).not.toContain('modalIsolation');
            expect(source).not.toContain('createPortal');
            expect(source).not.toContain('react-dom');
        }
    });

    test('a plain click on Ask calls onAsk and prevents the navigation', async () => {
        addWorkspaceHost();
        const {onAsk} = renderDetails();
        await loaded();

        const link = screen.getByTestId('company-chat-cta');
        expect(link).toHaveAttribute('href', '/chat/?company=1');
        const notPrevented = fireEvent.click(link);

        expect(onAsk).toHaveBeenCalledTimes(1);
        expect(notPrevented).toBe(false);
    });

    test('Ask stays a plain link for modified clicks and without the workspace host', async () => {
        const {onAsk} = renderDetails();
        await loaded();
        const link = screen.getByTestId('company-chat-cta');

        // No workspace host on the page.
        expect(fireEvent.click(link)).toBe(true);

        addWorkspaceHost();
        expect(fireEvent.click(link, {metaKey: true})).toBe(true);
        expect(fireEvent.click(link, {ctrlKey: true})).toBe(true);
        expect(fireEvent.click(link, {shiftKey: true})).toBe(true);
        expect(fireEvent.click(link, {altKey: true})).toBe(true);
        expect(fireEvent.click(link, {button: 1})).toBe(true);

        expect(onAsk).not.toHaveBeenCalled();
    });

    test('a click something else already handled is left alone', async () => {
        addWorkspaceHost();
        const onAsk = jest.fn();
        render(
            <div onClickCapture={event => event.preventDefault()}>
                <CompanyDetails organization={company(1)} isAuthenticated onAsk={onAsk}
                                onSuggestCorrection={jest.fn()}/>
            </div>
        );
        await loaded();

        fireEvent.click(screen.getByTestId('company-chat-cta'));

        expect(onAsk).not.toHaveBeenCalled();
    });

    test('each correction trigger calls onSuggestCorrection with its source and field', async () => {
        const {onSuggestCorrection} = renderDetails();
        await loaded();

        fireEvent.click(screen.getByTestId('suggest-correction-link'));
        expect(onSuggestCorrection).toHaveBeenLastCalledWith('company_details');

        fireEvent.click(screen.getByTestId('suggest-correction-field-rto_policy'));
        expect(onSuggestCorrection).toHaveBeenLastCalledWith('company_evidence', 'rto_policy');
        expect(onSuggestCorrection).toHaveBeenCalledTimes(2);
    });

    test('the empty-evidence action calls onSuggestCorrection', async () => {
        mockFetch([
            ['/scores/', reply([])],
            ['/provenance/', reply(provenance(1, {fields: []}))],
            ['/api/company-corrections/', reply({corrections: []})],
        ]);
        const {onSuggestCorrection} = renderDetails();
        await loaded();

        fireEvent.click(screen.getByTestId('evidence-empty-action'));

        expect(onSuggestCorrection).toHaveBeenCalledWith('company_details');
    });

    test('signed out: sign-in links, no correction triggers and no pending-suggestions request', async () => {
        const {onAsk, onSuggestCorrection} = renderDetails({
            isAuthenticated: false,
            signInUrlTemplate: '/accounts/login/?next=/%3Fcompany%3D__COMPANY_ID__',
        });
        await loaded();

        expect(screen.getByTestId('company-sign-in-cta'))
            .toHaveAttribute('href', '/accounts/login/?next=/%3Fcompany%3D1');
        expect(screen.queryByTestId('company-chat-cta')).not.toBeInTheDocument();
        expect(screen.queryByTestId('suggest-correction-link')).not.toBeInTheDocument();
        expect(screen.queryByTestId('suggest-correction-field-rto_policy')).not.toBeInTheDocument();
        const urls = (global.fetch as jest.Mock).mock.calls.map(([url]) => url);
        expect(urls).toEqual(['/api/organizations/1/scores/', '/api/organizations/1/provenance/']);
        expect(onAsk).not.toHaveBeenCalled();
        expect(onSuggestCorrection).not.toHaveBeenCalled();
    });

    test('a retry that fails again returns focus to Retry', async () => {
        mockFetch([
            ['/scores/', reply([])],
            ['/provenance/', reply({}, 500)],
            ['/api/company-corrections/', reply({corrections: []})],
        ]);
        renderDetails();
        const retry = await screen.findByTestId('provenance-retry');
        // The first failure does not take focus.
        expect(retry).not.toHaveFocus();

        fireEvent.click(retry);
        await waitFor(() => expect(screen.getByTestId('provenance-retry')).toHaveFocus());
    });

    // Issue #486: the score breakdown of one company must never be shown for
    // another. Rendered through the dialog, whose props did not change, so the
    // same tests run against the code before the extraction.
    describe('scores do not carry over between companies', () => {
        const openDialog = (organization: CompanyDetailsOrganization) => (
            <OrganizationDetailsPopup organization={organization} visible onClose={() => {}}/>
        );

        test('open A (scores load), open B with a failing scores request: B shows no scores of A', async () => {
            mockFetch([
                ['/api/organizations/1/scores/', reply([{type__name: 'Culture', avg_score: 4.5}])],
                // What a server error looks like here: an HTML page, so
                // reading it as JSON fails.
                ['/api/organizations/2/scores/', {
                    ok: false, status: 500, json: () => Promise.reject(new SyntaxError('Unexpected token <')),
                }],
                ['/provenance/', reply(provenance(1))],
            ]);
            const {rerender} = render(openDialog(company(1)));
            await loaded();
            expect(screen.getByText('Culture')).toBeInTheDocument();

            rerender(openDialog(company(2, {avg_score: 3.1})));
            await loaded();

            expect(screen.getByRole('heading', {name: 'Company 2'})).toBeInTheDocument();
            expect(screen.queryByText('Culture')).not.toBeInTheDocument();
            expect(screen.queryByText('4.50')).not.toBeInTheDocument();
            // With no breakdown, the card falls back to the company's own overall score.
            expect(screen.getByText('Overall Score')).toBeInTheDocument();
            expect(screen.getByText('3.10')).toBeInTheDocument();
        });

        test('B never shows A\'s scores while its own are still loading', async () => {
            let answerB: (response: Reply) => void = () => {};
            mockFetch([
                ['/api/organizations/1/scores/', reply([{type__name: 'Culture', avg_score: 4.5}])],
                ['/api/organizations/2/scores/', new Promise<Reply>(resolve => { answerB = resolve; })],
                ['/provenance/', reply(provenance(1))],
            ]);
            const {rerender} = render(openDialog(company(1)));
            await loaded();

            rerender(openDialog(company(2)));

            expect(screen.getByTestId('scores-loading')).toBeInTheDocument();
            expect(screen.queryByText('Culture')).not.toBeInTheDocument();
            answerB(reply([{type__name: 'Leadership', avg_score: 3.8}]));
            await loaded();
            expect(screen.getByText('Leadership')).toBeInTheDocument();
            expect(screen.queryByText('Culture')).not.toBeInTheDocument();
        });

        test('a late answer for A does not replace B\'s scores', async () => {
            let answerA: (response: Reply) => void = () => {};
            mockFetch([
                ['/api/organizations/1/scores/', new Promise<Reply>(resolve => { answerA = resolve; })],
                ['/api/organizations/2/scores/', reply([{type__name: 'Leadership', avg_score: 3.8}])],
                ['/provenance/', reply(provenance(1))],
            ]);
            const {rerender} = render(openDialog(company(1)));
            rerender(openDialog(company(2)));
            await loaded();
            expect(screen.getByText('Leadership')).toBeInTheDocument();

            answerA(reply([{type__name: 'Culture', avg_score: 4.5}]));
            await waitFor(() => expect(global.fetch).toHaveBeenCalledWith('/api/organizations/1/scores/'));
            await new Promise(resolve => setTimeout(resolve, 0));

            expect(screen.getByText('Leadership')).toBeInTheDocument();
            expect(screen.queryByText('Culture')).not.toBeInTheDocument();
        });
    });
});
