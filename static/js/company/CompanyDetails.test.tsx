// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import * as fs from 'fs';
import * as path from 'path';
import * as React from 'react';

import CompanyView from './CompanyView';
import CompanyDetails, {CompanyDetailsOrganization} from './CompanyDetails';
import {getWorkspaceSnapshot, resetWorkspaceForTests} from '../workspace/store';

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

// Every module reachable through static or dynamic imports, as paths relative
// to static/js, and every package imported along the way (the same walk as
// workspace/bundleIsolation.test.ts).
const JS_ROOT = path.resolve(__dirname, '..');
const IMPORT_RE = /(?:import|export)\s[^'"]*?from\s+['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)|import\s+['"]([^'"]+)['"]/g;

const resolveModule = (from: string, spec: string): string | null => {
    const base = path.resolve(path.dirname(from), spec);
    const candidates = [base, `${base}.ts`, `${base}.tsx`, `${base}.js`,
        path.join(base, 'index.ts'), path.join(base, 'index.tsx')];
    return candidates.find(candidate => fs.existsSync(candidate) && fs.statSync(candidate).isFile()) ?? null;
};

const reachable = (entry: string): {files: string[]; packages: string[]} => {
    const seen = new Set<string>();
    const packages = new Set<string>();
    const queue = [entry];
    while (queue.length) {
        const file = queue.pop() as string;
        if (seen.has(file)) continue;
        seen.add(file);
        for (const match of fs.readFileSync(file, 'utf8').matchAll(IMPORT_RE)) {
            const spec = match[1] || match[2] || match[3];
            if (!spec.startsWith('.')) {
                packages.add(spec);
                continue;
            }
            const resolved = resolveModule(file, spec);
            if (resolved) queue.push(resolved);
        }
    }
    return {
        files: Array.from(seen).map(file => path.relative(JS_ROOT, file).split(path.sep).join('/')).sort(),
        packages: Array.from(packages).sort(),
    };
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

    test('the module graph of the content reaches neither the modal lock nor a portal', () => {
        const graph = reachable(path.resolve(__dirname, 'CompanyDetails.tsx'));

        expect(graph.files).toContain('company/useCompanyData.ts');
        expect(graph.files.length).toBeGreaterThan(2);
        expect(graph.files).not.toContain('modalIsolation.ts');
        expect(graph.packages).toContain('react');
        expect(graph.packages).not.toContain('react-dom');
        for (const file of graph.files) {
            expect(fs.readFileSync(path.join(JS_ROOT, file), 'utf8')).not.toContain('createPortal');
        }
    });

    test('the same walk does find the modal lock and the portal from a real dialog', () => {
        const graph = reachable(path.join(JS_ROOT, 'CompanyCorrectionForm.tsx'));

        expect(graph.files).toContain('modalIsolation.ts');
    });

    test('the view around the content reaches neither the modal lock nor a portal', () => {
        const graph = reachable(path.resolve(__dirname, 'CompanyView.tsx'));

        expect(graph.files).toContain('company/CompanyDetails.tsx');
        expect(graph.files).not.toContain('modalIsolation.ts');
        expect(graph.packages).not.toContain('react-dom');
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

    test.each([
        ['at the limit', 'N'.repeat(40), 'N'.repeat(40)],
        ['over the limit', 'N'.repeat(41), 'this company'],
    ])('the Ask label keeps the name or says "this company" (%s)', async (_label, name, shown) => {
        renderDetails({organization: company(1, {name})});
        await loaded();
        expect(screen.getByTestId('company-chat-cta')).toHaveTextContent(`Ask the assistant about ${shown}`);
        expect(screen.getByTestId('company-chat-cta')).not.toHaveClass('btn-sm');
    });

    test.each([
        ['at the limit', 'S'.repeat(40), 'S'.repeat(40)],
        ['over the limit', 'S'.repeat(41), 'this company'],
    ])('the signed-out button follows the same rule (%s)', async (_label, name, shown) => {
        renderDetails({
            organization: company(1, {name}), isAuthenticated: false,
            signInUrlTemplate: '/accounts/login/?next=%2F%3Fcompany%3D__COMPANY_ID__',
        });
        await loaded();
        expect(screen.getByTestId('company-sign-in-cta')).toHaveTextContent(`Sign in to ask about ${shown}`);
        expect(screen.getByTestId('company-sign-in-cta')).not.toHaveClass('btn-sm');
    });

    test('Not ranked is plain text and the empty score line is the muted one', async () => {
        renderDetails({organization: company(1, {ranking: undefined, avg_score: undefined, avg_scores: []})});
        await loaded();
        expect(screen.getByTestId('not-ranked')).not.toHaveClass('text-muted');
        await waitFor(() => expect(screen.getByTestId('no-scores')).toHaveClass('text-muted'));
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

    // The date helpers moved with the markup. Dates are placed relative to
    // the real clock, an hour inside each range, so no timer is replaced.
    describe('dates in "Data Freshness & Sources"', () => {
        const HOUR = 60 * 60 * 1000;
        const ago = (days: number) => new Date(Date.now() - days * 24 * HOUR - HOUR).toISOString();
        const showProvenance = async (overrides: Record<string, unknown>) => {
            mockFetch([
                ['/scores/', reply([])],
                ['/provenance/', reply(provenance(1, overrides))],
                ['/api/company-corrections/', reply({corrections: []})],
            ]);
            renderDetails();
            await loaded();
        };

        test.each([
            [0, 'Today ('],
            [1, '1 day ago ('],
            [3, '3 days ago ('],
        ])('a record edited %i day(s) ago reads "%s…"', async (days, text) => {
            await showProvenance({organization_modified: ago(days)});

            expect(screen.getByTestId('last-updated').textContent).toContain(text);
        });

        test('a missing date reads "Unknown"', async () => {
            await showProvenance({
                organization_modified: null,
                organization_created: null,
                latest_observation: {
                    source_url: 'https://example.com/careers', observed_domain: 'example.com', observed_at: null,
                    extraction_version: 'v1', status: 'accepted',
                },
            });

            expect(screen.getByTestId('last-updated')).toHaveTextContent(/^Unknown$/);
            expect(screen.getByTestId('added-to-catalog')).toHaveTextContent(/^Unknown$/);
            expect(screen.getByTestId('observation-details')).toHaveTextContent('Last Observed:Unknown (Unknown)');
        });
    });

    // Issue #486: the score breakdown of one company must never be shown for
    // another. Rendered through the dialog, whose props did not change, so the
    // same tests run against the code before the extraction.
    describe('scores do not carry over between companies', () => {
        const openDialog = (organization: CompanyDetailsOrganization) => (
            <CompanyView organizationId={organization.id} status="ready" organization={organization}
                         onBack={() => {}} onAsk={() => {}} onSuggestCorrection={() => {}}/>
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

        // The path a modal dialog actually takes: A is closed before B is opened (Back to results, then another row).
        test('open A, close the dialog, open B with a failing scores request: B shows no scores of A', async () => {
            mockFetch([
                ['/api/organizations/1/scores/', reply([{type__name: 'Culture', avg_score: 4.5}])],
                ['/api/organizations/2/scores/', {
                    ok: false, status: 500, json: () => Promise.reject(new SyntaxError('Unexpected token <')),
                }],
                ['/provenance/', reply(provenance(1))],
            ]);
            const {rerender} = render(openDialog(company(1)));
            await loaded();
            expect(screen.getByText('Culture')).toBeInTheDocument();

            // Closing keeps the company selected, as the list does.
            rerender(<div/>);
            expect(screen.queryByTestId('company-view')).not.toBeInTheDocument();
            rerender(openDialog(company(2, {avg_score: 3.1})));
            await loaded();

            expect(screen.getByRole('heading', {name: 'Company 2'})).toBeInTheDocument();
            expect(screen.queryByText('Culture')).not.toBeInTheDocument();
            expect(screen.queryByText('4.50')).not.toBeInTheDocument();
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

describe('CompanyDetails: fit, jobs and View jobs (issue #486, AC 15-18)', () => {
    const answer = (body: unknown) => ({ok: true, status: 200, redirected: false, json: () => Promise.resolve(body)});
    let errorSpy: jest.SpyInstance;

    beforeEach(() => {
        errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
        resetWorkspaceForTests();
        mockFetch([
            ['/api/organizations/1/scores/', reply([{type__name: 'Culture', avg_score: 4.5}])],
            ['/api/organizations/1/provenance/', reply(provenance(1))],
            ['/api/organizations/1/fit/', answer({
                organization_id: 1, has_preferences: false, fit_score: null, company_score: null, coverage: null,
                requirements: [],
            }) as unknown as Reply],
            ['/api/organizations/1/jobs/', answer({
                organization_id: 1, inventory: 'available', count: 0, results: [],
            }) as unknown as Reply],
            ['/api/company-corrections/', reply({corrections: []})],
        ]);
    });

    afterEach(() => {
        errorSpy.mockRestore();
        resetWorkspaceForTests();
    });

    const renderDetails = (props: Partial<React.ComponentProps<typeof CompanyDetails>> = {}) => render(
        <CompanyDetails organization={company(1)} isAuthenticated onAsk={() => {}} onSuggestCorrection={() => {}}
                        {...props}/>);

    test('the sections sit between the profile and the evidence, with headings in order', async () => {
        renderDetails();
        await screen.findByTestId('fit-no-priorities');
        await screen.findByTestId('jobs-empty');
        const order = ['company-handoff-actions', 'popup-details-grid', 'company-fit', 'company-jobs', 'provenance-section'];
        await screen.findByTestId('provenance-section');
        const nodes = order.map(id => screen.getByTestId(id));
        for (let i = 1; i < nodes.length; i += 1) {
            expect(nodes[i - 1].compareDocumentPosition(nodes[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        }
        const headings = screen.getAllByRole('heading').map(heading => heading.textContent);
        expect(headings.indexOf('Fit with your priorities')).toBeLessThan(headings.indexOf('Open jobs'));
        expect(headings.indexOf('Open jobs')).toBeLessThan(headings.indexOf('Data Freshness & Sources'));
    });

    test('View jobs moves focus to the Jobs heading and scrolls it into view', async () => {
        const scrollIntoView = jest.fn();
        Element.prototype.scrollIntoView = scrollIntoView;
        renderDetails();
        fireEvent.click(screen.getByTestId('company-view-jobs'));
        const heading = screen.getByRole('heading', {name: 'Open jobs'});
        expect(heading).toHaveFocus();
        expect(scrollIntoView).toHaveBeenCalledWith({block: 'start'});
        await screen.findByTestId('jobs-empty');
        expect(screen.getByTestId('company-view-jobs')).toHaveTextContent('View jobs');
    });

    test('View jobs is there when signed out and lands on the sign-in prompt, with no request', async () => {
        Element.prototype.scrollIntoView = jest.fn();
        renderDetails({isAuthenticated: false, signInUrlTemplate: '/accounts/login/?next=%2F%3Fcompany%3D__COMPANY_ID__'});
        fireEvent.click(screen.getByTestId('company-view-jobs'));
        expect(screen.getByRole('heading', {name: 'Open jobs'})).toHaveFocus();
        expect(screen.getByTestId('jobs-sign-in')).toBeInTheDocument();
        expect(screen.getByTestId('fit-sign-in').querySelector('a')).toHaveAttribute(
            'href', '/accounts/login/?next=%2F%3Fcompany%3D1');
        await screen.findByTestId('provenance-section');
        const urls = (global.fetch as jest.Mock).mock.calls.map(([url]) => String(url));
        expect(urls.filter(url => url.includes('/fit/') || url.includes('/jobs/'))).toEqual([]);
    });

    test('the priorities control opens the editor and the assistant', async () => {
        renderDetails();
        fireEvent.click(await screen.findByTestId('fit-set-priorities'));
        expect(getWorkspaceSnapshot().prioritiesEditorOpenIn).toBe('sidebar');
        expect(getWorkspaceSnapshot().visibility).toBe('open');
    });

    test('View jobs does nothing visible without the jobs heading mounted', () => {
        // Defensive: the handler never throws if the heading is gone.
        renderDetails();
        const button = screen.getByTestId('company-view-jobs');
        screen.getByRole('heading', {name: 'Open jobs'}).remove();
        expect(() => fireEvent.click(button)).not.toThrow();
    });
    test('"See your fit" comes first in the action row, before "View jobs", and focuses the fit heading', async () => {
        const scrollIntoView = jest.fn();
        Element.prototype.scrollIntoView = scrollIntoView;
        renderDetails();
        const jumps = screen.getByTestId('company-see-fit').parentElement as HTMLElement;
        expect(Array.from(jumps.querySelectorAll('button')).map(button => button.textContent))
            .toEqual(['See your fit', 'View jobs']);
        fireEvent.click(screen.getByTestId('company-see-fit'));
        expect(screen.getByRole('heading', {name: 'Fit with your priorities'})).toHaveFocus();
        expect(scrollIntoView).toHaveBeenCalledWith({block: 'start'});
        await screen.findByTestId('fit-no-priorities');
    });

    describe('a late fit answer and the heading a jump focused (F7)', () => {
        let release: () => void;
        let scrollIntoView: jest.Mock;
        const holdFit = () => {
            const fitAnswer = answer({
                organization_id: 1, has_preferences: false, fit_score: null, company_score: null, coverage: null,
                requirements: [],
            });
            const held = new Promise<Reply>(resolve => {
                release = () => resolve(fitAnswer as unknown as Reply);
            });
            mockFetch([
                ['/api/organizations/1/scores/', reply([])],
                ['/api/organizations/1/provenance/', reply(provenance(1))],
                ['/api/organizations/1/fit/', held],
                ['/api/organizations/1/jobs/', answer({
                    organization_id: 1, inventory: 'available', count: 0, results: [],
                }) as unknown as Reply],
                ['/api/company-corrections/', reply({corrections: []})],
            ]);
        };
        beforeEach(() => {
            scrollIntoView = jest.fn();
            Element.prototype.scrollIntoView = scrollIntoView;
            holdFit();
        });

        const settle = async () => {
            await act(async () => {
                release();
            });
            await screen.findByTestId('fit-no-priorities');
        };

        test('the heading is brought back into view when the answer lands and the reader has not moved', async () => {
            renderDetails();
            fireEvent.click(screen.getByTestId('company-view-jobs'));
            expect(scrollIntoView).toHaveBeenCalledTimes(1);
            await settle();
            expect(scrollIntoView).toHaveBeenCalledTimes(2);
            expect(scrollIntoView).toHaveBeenLastCalledWith({block: 'start'});
        });

        test.each([
            ['a wheel', () => fireEvent.wheel(window)],
            ['a touch drag', () => fireEvent.touchMove(window)],
            ['a pointer press', () => fireEvent.pointerDown(window)],
            ['Page Down', () => fireEvent.keyDown(window, {key: 'PageDown'})],
        ])('%s since the jump: the page is not moved', async (_name, move) => {
            renderDetails();
            fireEvent.click(screen.getByTestId('company-view-jobs'));
            move();
            await settle();
            expect(scrollIntoView).toHaveBeenCalledTimes(1);
        });

        test.each(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '])(
            'the scroll key %j since the jump: the page is not moved', async key => {
                renderDetails();
                fireEvent.click(screen.getByTestId('company-view-jobs'));
                fireEvent.keyDown(window, {key});
                await settle();
                expect(scrollIntoView).toHaveBeenCalledTimes(1);
            });

        test('every listener a jump adds is removed when the next jump replaces it and when the view unmounts', () => {
            const added = jest.spyOn(window, 'addEventListener');
            const removed = jest.spyOn(window, 'removeEventListener');
            const names = ['wheel', 'touchmove', 'pointerdown', 'keydown'];
            const count = (spy: jest.SpyInstance, name: string) => spy.mock.calls.filter(call => call[0] === name).length;
            try {
                const view = renderDetails();
                fireEvent.click(screen.getByTestId('company-view-jobs'));
                for (const name of names) expect(count(added, name)).toBe(1);
                for (const name of names) expect(count(removed, name)).toBe(0);
                fireEvent.click(screen.getByTestId('company-see-fit'));
                // The second jump stops the first before it starts its own.
                for (const name of names) expect(count(added, name)).toBe(2);
                for (const name of names) expect(count(removed, name)).toBe(1);
                view.unmount();
                for (const name of names) expect(count(removed, name)).toBe(2);
            } finally {
                added.mockRestore();
                removed.mockRestore();
            }
        });

        test('a key that does not scroll leaves the guard on', async () => {
            renderDetails();
            fireEvent.click(screen.getByTestId('company-view-jobs'));
            fireEvent.keyDown(window, {key: 'a'});
            await settle();
            expect(scrollIntoView).toHaveBeenCalledTimes(2);
        });

        test('focus elsewhere: the page is not moved', async () => {
            renderDetails();
            fireEvent.click(screen.getByTestId('company-view-jobs'));
            screen.getByTestId('company-see-fit').focus();
            await settle();
            expect(scrollIntoView).toHaveBeenCalledTimes(1);
        });

        test('a second jump replaces the first; leaving the view releases the listeners', async () => {
            const view = renderDetails();
            fireEvent.click(screen.getByTestId('company-view-jobs'));
            fireEvent.click(screen.getByTestId('company-see-fit'));
            expect(scrollIntoView).toHaveBeenCalledTimes(2);
            await settle();
            expect(scrollIntoView).toHaveBeenCalledTimes(3);
            view.unmount();
            const before = scrollIntoView.mock.calls.length;
            fireEvent.wheel(window);
            expect(scrollIntoView).toHaveBeenCalledTimes(before);
        });

        test('without a jump the answer moves nothing', async () => {
            renderDetails();
            await settle();
            expect(scrollIntoView).not.toHaveBeenCalled();
        });
    });
});
