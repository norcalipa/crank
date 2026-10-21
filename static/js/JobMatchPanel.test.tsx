// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {cleanup, render, screen, fireEvent, waitFor, within} from '@testing-library/react';
import * as React from 'react';

import JobMatchPanel from './JobMatchPanel';
import {MATCH_TERMS} from './labels';
import {act} from '@testing-library/react';
import {
    closeAssistant, getWorkspaceSnapshot, openAssistant, resetPriorities, resetWorkspaceForTests,
    setPrioritiesRevision,
} from './workspace/store';

function jsonResponse(payload: unknown, status = 200): Response {
    return new Response(JSON.stringify(payload), {
        status,
        headers: {'Content-Type': 'application/json'},
    });
}

function statusPayload(state: string, overrides: Partial<{
    title: string;
    message: string;
    actions: string[];
    staff_detail: string;
}> & Record<string, unknown> = {}) {
    const known = ['state', 'title', 'message', 'actions', 'staff_detail'];
    const extra = Object.fromEntries(
        Object.entries(overrides).filter(([key]) => !known.includes(key)),
    );
    return {
        state,
        title: overrides.title ?? 'Test title',
        message: overrides.message ?? 'Test message',
        actions: overrides.actions ?? [],
        ...(overrides.staff_detail ? {staff_detail: overrides.staff_detail} : {}),
        // Additive #476 fields (refreshing, coverage, active_constraints,
        // inventory, relaxation_preview) pass through untouched.
        ...extra,
    };
}

function matchPayload(count: number, results: any[] = []) {
    return {count, next: null, previous: null, results};
}

function rankedPayload(jobs: any[] = [], orgs: any[] = []) {
    return {job_matches: jobs, organization_matches: orgs};
}

const sampleJobMatch = {
    listing_id: 42,
    title: 'Senior Engineer',
    employer_name: 'Acme Corp',
    organization_id: 7,
    organization_name: 'Acme Corp',
    canonical_url: 'https://jobs.example.test/42',
    location_text: 'San Francisco, CA',
    is_remote: true,
    score: 85.5,
    reasons: ['Public company', 'Remote', 'Score 4.2'],
};

const sampleOrgMatch = {
    organization_id: 7,
    name: 'Acme Corp',
    url: 'https://acme.example.test',
    funding_round: 'P',
    rto_policy: 'R',
    score: 50.0,
    fit_score: 50.0,
    priorities_met: {met: 1, counted: 2},
    reasons: ['Public company', 'Remote', 'Score 4.2'],
};

// Helper: mock all three API calls and wait for ready phase.
// Hoisted to module scope so every describe block in this file can reuse it.
async function renderPanel(
    statusState: string = 'ok',
    opts: { count?: number; staffDetail?: string; statusOverrides?: Record<string, unknown>; rankedJobs?: any[]; rankedOrgs?: any[]; rankedStatus?: number; storedResults?: any[] } = {},
) {
    const count = opts.count ?? 0;
    const rankedJobs = opts.rankedJobs ?? [];
    const rankedOrgs = opts.rankedOrgs ?? [];
    const rankedStatus = opts.rankedStatus ?? 200;
    const mock = global.fetch as jest.Mock;
    mock.mockImplementation((url: string) => {
        if (url.includes('/api/job-matches/status/')) {
            return Promise.resolve(jsonResponse(
                statusPayload(statusState, {
                    ...(opts.staffDetail ? { staff_detail: opts.staffDetail } : {}),
                    ...(opts.statusOverrides || {}),
                }),
            ));
        }
        if (url.includes('/api/job-matches/ranked/')) {
            return Promise.resolve(jsonResponse(rankedPayload(rankedJobs, rankedOrgs), rankedStatus));
        }
        // The revision read (issue #485) is not a list read: by default it
        // says there is nothing to poll, so request-count assertions hold.
        if (url.includes('/api/job-matches/revision/')) {
            return Promise.resolve(jsonResponse({result_generation: null, poll_after_seconds: null}));
        }
        if (url.includes('/api/job-matches/')) {
            return Promise.resolve(jsonResponse(matchPayload(count, opts.storedResults ?? [])));
        }
        return Promise.resolve(jsonResponse({}));
    });
    render(<JobMatchPanel/>);
    await waitFor(() => expect(screen.getByTestId('job-match-panel')).not.toHaveTextContent('Loading'));
}

describe('JobMatchPanel', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        resetWorkspaceForTests();
        closeAssistant();
    });

    describe('loading state', () => {
        test('shows a loading indicator while fetching', () => {
            (global.fetch as jest.Mock).mockReturnValue(new Promise(() => {}));
            render(<JobMatchPanel/>);
            expect(screen.getByTestId('job-match-loading')).toBeInTheDocument();
        });

        test('loading state is an emphasised role=status region with skeleton rows', () => {
            (global.fetch as jest.Mock).mockReturnValue(new Promise(() => {}));
            render(<JobMatchPanel/>);
            const loading = screen.getByTestId('job-match-loading');
            expect(loading).toHaveAttribute('role', 'status');
            expect(loading).toHaveAttribute('aria-live', 'polite');
            // Visible spinner label plus skeleton placeholder rows.
            expect(loading).toHaveTextContent(/loading your match status/i);
            expect(loading.querySelectorAll('.job-match-skeleton-row').length).toBeGreaterThan(0);
        });
    });

    describe('error state', () => {
        test('shows an error with retry when the status API fails', async () => {
            (global.fetch as jest.Mock).mockImplementation((url: string) => {
                if (url.includes('/status/')) return Promise.resolve(jsonResponse({}, 500));
                if (url.includes('/ranked/')) return Promise.resolve(jsonResponse(rankedPayload()));
                return Promise.resolve(jsonResponse(matchPayload(0)));
            });
            render(<JobMatchPanel/>);
            const error = await screen.findByTestId('job-match-error');
            expect(error).toBeInTheDocument();
            // Raw implementation details (a bare HTTP status code) never reach
            // the user; the copy is actionable with a labelled retry.
            expect(error).toHaveTextContent(/couldn.t load your job matches/i);
            expect(error.textContent).not.toMatch(/status 500/i);
        });

        test('shows an error when fetch throws', async () => {
            (global.fetch as jest.Mock).mockRejectedValue(new Error('Network down'));
            render(<JobMatchPanel/>);
            const error = await screen.findByTestId('job-match-error');
            // Only the panel's own sentences are shown, never the browser's (issue #485).
            expect(error).toHaveTextContent('We couldn’t load your job matches. Please try again.');
            expect(error).not.toHaveTextContent('Network down');
        });

        test('a dropped connection on first load prints the panel’s sentence, not the browser’s', async () => {
            (global.fetch as jest.Mock).mockRejectedValue(new TypeError('Failed to fetch'));
            render(<JobMatchPanel/>);
            const error = await screen.findByTestId('job-match-error');
            expect(error).toHaveTextContent('We couldn’t load your job matches. Please try again.');
            expect(error).not.toHaveTextContent('Failed to fetch');
        });

        test('translates a non-JSON status body into friendly copy', async () => {
            // A login redirect or HTML error page makes res.json() reject with
            // a DOMException; parseJson must rewrite it as truthful copy.
            (global.fetch as jest.Mock).mockImplementation((url: string) => {
                if (url.includes('/status/')) {
                    return Promise.resolve(new Response('<html><body>Redirecting…</body></html>', {
                        status: 200,
                        headers: {'Content-Type': 'text/html'},
                    }));
                }
                return Promise.resolve(jsonResponse(matchPayload(0)));
            });
            render(<JobMatchPanel/>);
            const error = await screen.findByTestId('job-match-error');
            expect(error).toHaveTextContent(/couldn.t load your job matches/i);
        });

        test('retry button re-fetches status', async () => {
            const mock = global.fetch as jest.Mock;
            mock.mockImplementationOnce((url: string) => {
                if (url.includes('/status/')) return Promise.resolve(jsonResponse({}, 500));
                return Promise.resolve(jsonResponse(matchPayload(0)));
            });
            render(<JobMatchPanel/>);
            await screen.findByTestId('job-match-error');
            mock.mockImplementation((url: string) => {
                if (url.includes('/status/')) return Promise.resolve(jsonResponse(statusPayload('ok')));
                if (url.includes('/ranked/')) return Promise.resolve(jsonResponse(rankedPayload()));
                return Promise.resolve(jsonResponse(matchPayload(3)));
            });
            fireEvent.click(screen.getByLabelText('Retry loading matches'));
            await waitFor(() => expect(screen.queryByTestId('job-match-error')).not.toBeInTheDocument());
        });
    });

    describe('ranked matches', () => {
        test('renders ranked job matches with reasons and links', async () => {
            await renderPanel('ok', { count: 1, rankedJobs: [sampleJobMatch] });
            const jobEl = screen.getByTestId('ranked-job-42');
            expect(jobEl).toBeInTheDocument();
            expect(jobEl).toHaveTextContent('Senior Engineer');
            expect(jobEl).toHaveTextContent('Acme Corp');
            const link = jobEl.querySelector('a[href="https://jobs.example.test/42"]');
            expect(link).not.toBeNull();
            const reasons = screen.getByTestId('job-reasons-42');
            expect(reasons).toHaveTextContent('Public company');
            expect(reasons).toHaveTextContent('Remote');
            expect(reasons).toHaveTextContent('Score 4.2');
        });

        test('org cards and org-linked job cards open the correction form via the cross-bundle event', async () => {
            await renderPanel('ok', { count: 2, rankedJobs: [sampleJobMatch], rankedOrgs: [sampleOrgMatch] });
            const handler = jest.fn();
            window.addEventListener('crank:suggest-company', handler);
            const buttons = screen.getAllByRole('button', {name: 'Suggest a correction for Acme Corp'});
            expect(buttons).toHaveLength(2);
            buttons.forEach(button => fireEvent.click(button));
            expect(handler).toHaveBeenCalledTimes(2);
            expect((handler.mock.calls[0][0] as CustomEvent).detail).toEqual({
                kind: 'correction', source: 'job_results', organizationId: 7, companyName: 'Acme Corp',
            });
            window.removeEventListener('crank:suggest-company', handler);
        });

        test('job cards without an organization have no correction button and fall back to the employer name', async () => {
            await renderPanel('ok', { count: 2, rankedJobs: [
                {...sampleJobMatch, listing_id: 43, organization_id: null},
                {...sampleJobMatch, listing_id: 44, organization_id: 9, organization_name: ''},
            ] });
            expect(screen.queryByTestId('suggest-correction-org-7')).toBeNull();
            expect(screen.getByRole('button', {name: 'Suggest a correction for Acme Corp'}))
                .toHaveAttribute('data-testid', 'suggest-correction-org-9');
        });

        test('renders ranked organization matches with reasons and links', async () => {
            await renderPanel('ok', { count: 1, rankedOrgs: [sampleOrgMatch] });
            const orgEl = screen.getByTestId('ranked-org-7');
            expect(orgEl).toBeInTheDocument();
            expect(orgEl).toHaveTextContent('Acme Corp');
            expect(orgEl).toHaveTextContent('Public');
            const link = orgEl.querySelector('a[href="https://acme.example.test"]');
            expect(link).not.toBeNull();
            const reasons = screen.getByTestId('org-reasons-7');
            expect(reasons).toHaveTextContent('Public company');
            expect(reasons).toHaveTextContent('Remote');
        });

        test('shows one labelled fit figure, never a duplicate blended score badge', async () => {
            await renderPanel('ok', { count: 1, rankedJobs: [sampleJobMatch], rankedOrgs: [sampleOrgMatch] });
            // The blended top-right score pill is gone; the fit figure is the
            // single labelled source of the rank score (AC-10).
            expect(screen.queryByTestId('job-score-42')).not.toBeInTheDocument();
            expect(screen.queryByTestId('org-score-7')).not.toBeInTheDocument();
            expect(screen.getByTestId('job-42-fit-score')).toHaveTextContent('85.5 / 100');
            expect(screen.getByTestId('org-7-priorities-met')).toHaveTextContent('1 of 2');
            // A company has no "Fit x / 100": it prints the figure its own view prints.
            expect(screen.queryByTestId('org-7-fit-score')).not.toBeInTheDocument();
        });

        test('a company row prints "Priorities met n of m" with its meaning, and a job row keeps its fit', async () => {
            await renderPanel('ok', { count: 1, rankedJobs: [sampleJobMatch], rankedOrgs: [sampleOrgMatch] });
            const org = screen.getByTestId('ranked-org-7');
            const label = within(org).getByText(MATCH_TERMS.prioritiesMet.label);
            expect(label).toHaveAttribute('title', MATCH_TERMS.prioritiesMet.meaning);
            expect(within(org).queryByText(MATCH_TERMS.fit.label)).toBeNull();
            expect(org).not.toHaveTextContent('/ 100');
            expect(within(screen.getByTestId('ranked-job-42')).getByText(MATCH_TERMS.fit.label)).toBeInTheDocument();
        });

        test('companies keep the server order, with the tie shown by the rating beside the figure', async () => {
            const tied = (id: number, name: string, rating: number) => ({
                ...sampleOrgMatch, organization_id: id, name, company_score: rating, priorities_met: {met: 1, counted: 2},
            });
            await renderPanel('ok', { count: 1, rankedOrgs: [
                {...sampleOrgMatch, priorities_met: {met: 2, counted: 2}},
                tied(8, 'Birch Labs', 4.6),
                tied(9, 'Cedar Works', 3.2),
            ] });
            const cards = within(screen.getByTestId('ranked-org-matches')).getAllByTestId(/^ranked-org-\d+$/);
            expect(cards.map((card) => card.getAttribute('data-testid'))).toEqual(['ranked-org-7', 'ranked-org-8', 'ranked-org-9']);
            expect(screen.getByTestId('org-7-priorities-met')).toHaveTextContent('2 of 2');
            expect(screen.getByTestId('org-8-priorities-met')).toHaveTextContent('1 of 2');
            expect(screen.getByTestId('org-9-priorities-met')).toHaveTextContent('1 of 2');
            expect(screen.getByTestId('org-8-company-score')).toHaveTextContent('4.6 / 5');
            expect(screen.getByTestId('org-9-company-score')).toHaveTextContent('3.2 / 5');
            expect(screen.getByTestId('ranked-org-order-note'))
                .toHaveTextContent('Most priorities met first, then the higher average rating.');
        });

        test('the plain average is called "Average rating" on a company row and on a job row, and a company row leads with the priorities met', async () => {
            await renderPanel('ok', { count: 1, rankedJobs: [sampleJobMatch], rankedOrgs: [sampleOrgMatch] });
            const org = screen.getByTestId('ranked-org-7');
            const label = within(org).getByText(MATCH_TERMS.averageRating.label);
            expect(label).toHaveAttribute('title', MATCH_TERMS.averageRating.meaning);
            const labels = (row: HTMLElement) => Array.from(row.querySelectorAll('.job-match-figure-label')).map((el) => el.textContent);
            expect(labels(org)).toEqual(['Priorities met', 'Average rating', 'Requirement coverage']);
            const job = screen.getByTestId('ranked-job-42');
            expect(labels(job)).toEqual(['Average rating', 'Fit', 'Requirement coverage']);
            expect(within(job).getByText(MATCH_TERMS.averageRating.label)).toHaveAttribute('title', MATCH_TERMS.averageRating.meaning);
            expect(screen.queryByText(/Company score/)).toBeNull();
        });

        describe('the order note names the priorities checked for each job, once for the list', () => {
            const perJob = (path: string) => ({path, status: 'unknown', observed: null, counted: false});
            const counted = (path: string) => ({path, status: 'match', observed: 'x', counted: true});
            const note = () => screen.getByTestId('ranked-org-order-note');

            test('two priorities are named with "and", and the rows themselves do not repeat it', async () => {
                const requirements = [
                    perJob('compensation.minimum_salary'), counted('work_location.modes'),
                    perJob('industry'), counted('funding_stage'),
                ];
                await renderPanel('ok', { count: 1, rankedOrgs: [
                    {...sampleOrgMatch, priorities_met: {met: 2, counted: 2}, requirements},
                    {...sampleOrgMatch, organization_id: 8, name: 'Birch Labs', requirements},
                ] });
                expect(note()).toHaveTextContent('Checked for each job, not counted: Minimum salary and Industry.');
                expect(within(screen.getByTestId('ranked-org-matches')).getAllByText(/not counted/)).toHaveLength(1);
            });

            test('one priority reads as a sentence of its own', async () => {
                await renderPanel('ok', { count: 1, rankedOrgs: [
                    {...sampleOrgMatch, requirements: [perJob('industry'), counted('funding_stage')]},
                ] });
                expect(note()).toHaveTextContent('Checked for each job, not counted: Industry.');
            });

            test('three priorities are listed with commas', async () => {
                await renderPanel('ok', { count: 1, rankedOrgs: [
                    {...sampleOrgMatch, requirements: [perJob('industry'), perJob('culture'), perJob('compensation.minimum_salary')]},
                ] });
                expect(note()).toHaveTextContent('Checked for each job, not counted: Industry, Culture and Minimum salary.');
            });

            test('nothing is added when every priority is counted, or an older server sends no flag', async () => {
                await renderPanel('ok', { count: 1, rankedOrgs: [
                    {...sampleOrgMatch, requirements: [counted('funding_stage'), {path: 'industry', status: 'match'}]},
                    {...sampleOrgMatch, organization_id: 8, name: 'Birch Labs'},
                ] });
                expect(note().textContent).not.toMatch(/checked for each job/);
            });
        });

        test('the legend defines the average rating once and says which row type shows the other figures', async () => {
            await renderPanel('ok', { count: 1, rankedJobs: [sampleJobMatch], rankedOrgs: [sampleOrgMatch] });
            const legend = document.querySelector('.job-match-legend') as HTMLElement;
            const definition = (label: string) => within(legend).getByText(label, {selector: 'dt'}).nextElementSibling;
            expect(within(legend).getAllByText(MATCH_TERMS.averageRating.label, {selector: 'dt'})).toHaveLength(1);
            expect(definition(MATCH_TERMS.averageRating.label)?.textContent).toBe(MATCH_TERMS.averageRating.meaning);
            expect(within(legend).queryByText(/Company score/)).toBeNull();
            expect(definition(MATCH_TERMS.fit.label)).toHaveTextContent(`On job rows. ${MATCH_TERMS.fit.meaning}`);
            expect(MATCH_TERMS.fit.meaning).toBe('Computed from your saved priorities and never mixed into any company rating.');
            expect(definition(MATCH_TERMS.prioritiesMet.label)).toHaveTextContent(`On company rows. ${MATCH_TERMS.prioritiesMet.meaning}`);
            expect(definition(MATCH_TERMS.requirementCoverage.label)).toHaveTextContent(MATCH_TERMS.requirementCoverage.meaning);
        });

        test('a reader whose priorities are all checked for each job sees "—" and is told why the order is by average rating', async () => {
            await renderPanel('ok', { count: 1, rankedOrgs: [{
                ...sampleOrgMatch, priorities_met: null, score: 0,
                requirements: [{path: 'industry', status: 'unknown', observed: null, counted: false}],
            }] });
            expect(screen.getByTestId('org-7-priorities-met')).toHaveTextContent('—');
            expect(screen.getByTestId('ranked-org-order-note'))
                .toHaveTextContent('None of your priorities can be decided from a fact about a company, so companies are listed by average rating.');
        });

        test('a reader with no company-decidable rows at all is not told a per-job reason', async () => {
            await renderPanel('ok', { count: 1, rankedOrgs: [{...sampleOrgMatch, priorities_met: null, score: 0, requirements: []}] });
            const note = screen.getByTestId('ranked-org-order-note');
            expect(note).toHaveTextContent('Companies are listed by average rating.');
            expect(note.textContent).not.toMatch(/None of your priorities/);
        });

        test('an older server that sends no figure reads as "—" and the list makes no claim about its order', async () => {
            const {priorities_met: _omitted, ...withoutFigure} = sampleOrgMatch;
            await renderPanel('ok', { count: 1, rankedOrgs: [withoutFigure as typeof sampleOrgMatch, {...withoutFigure, organization_id: 8, name: 'Birch Labs'} as typeof sampleOrgMatch] });
            expect(screen.getByTestId('org-7-priorities-met')).toHaveTextContent('—');
            expect(screen.queryByTestId('ranked-org-order-note')).toBeNull();
        });

        test('shows remote badge for remote job listing (neutral, not semantic)', async () => {
            await renderPanel('ok', { count: 1, rankedJobs: [sampleJobMatch] });
            const jobEl = screen.getByTestId('ranked-job-42');
            expect(jobEl).toHaveTextContent('Remote');
            // "Remote" is an ordinary attribute, not a satisfied requirement,
            // so it must never carry a green/semantic treatment.
            expect(jobEl.querySelector('.bg-success')).toBeNull();
        });

        test('falls back to match count when ranked matches are empty', async () => {
            await renderPanel('ok', { count: 5, rankedJobs: [], rankedOrgs: [] });
            const panel = screen.getByTestId('job-match-panel');
            await waitFor(() => expect(panel).toHaveTextContent('5 job matches ready to review'));
            expect(screen.queryByTestId(/ranked-job-/)).not.toBeInTheDocument();
        });

        test('falls back to match count when ranked API fails', async () => {
            await renderPanel('ok', { count: 3, rankedStatus: 500 });
            const panel = screen.getByTestId('job-match-panel');
            await waitFor(() => expect(panel).toHaveTextContent('3 job matches ready to review'));
        });

        test('refresh button re-fetches ranked matches', async () => {
            const mock = global.fetch as jest.Mock;
            let jobScore = 85.5;
            mock.mockImplementation((url: string) => {
                if (url.includes('/status/')) return Promise.resolve(jsonResponse(statusPayload('ok')));
                if (url.includes('/ranked/')) return Promise.resolve(jsonResponse(rankedPayload([
                    {...sampleJobMatch, score: jobScore},
                ])));
                return Promise.resolve(jsonResponse(matchPayload(1)));
            });
            render(<JobMatchPanel/>);
            const fitFigure = await screen.findByTestId('job-42-fit-score');
            expect(fitFigure).toHaveTextContent('85.5 / 100');
            jobScore = 92.0;
            fireEvent.click(screen.getByTestId('job-match-refresh'));
            await waitFor(() => expect(screen.getByTestId('job-42-fit-score')).toHaveTextContent('92.0 / 100'));
        });
    });

    describe('ok state with matches (no ranked data)', () => {
        test('shows match count when matches exist', async () => {
            await renderPanel('ok', { count: 5 });
            const panel = screen.getByTestId('job-match-panel');
            await waitFor(() => expect(panel).toHaveTextContent('5 job matches ready to review'));
            expect(screen.queryByTestId(/empty-state-/)).not.toBeInTheDocument();
        });

        test('shows singular "match" for count of 1', async () => {
            await renderPanel('ok', { count: 1 });
            const panel = screen.getByTestId('job-match-panel');
            await waitFor(() => expect(panel).toHaveTextContent('1 job match ready to review'));
        });

        test('shows stale notice + timestamp from the persisted result revision', async () => {
            // The ranked endpoint is recomputed live (never stale); the persisted
            // list is the only surface that can flag a stale result. Feed a
            // stored result whose revision is behind the current document.
            await renderPanel('ok', {
                count: 1,
                storedResults: [{
                    id: 1,
                    revision: {
                        preference_revision: 1,
                        stale: true,
                        generated_at: '2026-09-22T08:00:00Z',
                    },
                }],
            });
            expect(await screen.findByTestId('stale-notice')).toBeInTheDocument();
            // The time is printed once, by the "Results generated at" line.
            expect(screen.getByTestId('stale-notice')).toHaveTextContent(/^Newer data or priorities exist\.\s*Check again$/);
            expect(screen.getByTestId('stale-refresh')).toHaveTextContent('Check again');
            expect(screen.getByTestId('results-timestamp')).toBeInTheDocument();
        });

        test('omits stale notice when persisted revision is not stale', async () => {
            await renderPanel('ok', {
                count: 1,
                storedResults: [{id: 1, revision: {preference_revision: 1, stale: false}}],
            });
            expect(screen.queryByTestId('stale-notice')).not.toBeInTheDocument();
        });
    });

    describe('empty states', () => {
        test('no_source: shows title, message, and recovery actions', async () => {
            await renderPanel('no_source', {
                statusOverrides: {
                    title: 'No job sources configured',
                    message: "CRank hasn't been connected to any job sources yet.",
                    actions: ['explore_companies', 'suggest_company', 'help'],
                },
            });
            const el = screen.getByTestId('empty-state-no_source');
            expect(el).toHaveTextContent('No job sources configured');
            expect(el).toHaveTextContent("hasn't been connected");
            // AC-4: explore_companies is the recommended (primary) recovery
            // path where openings can't be confirmed; the rest are secondary.
            const explore = screen.getByTestId('action-explore_companies');
            expect(explore).toBeInTheDocument();
            expect(explore).toHaveClass('btn-primary');
            expect(screen.getByTestId('action-suggest_company')).toHaveClass('btn-outline-info');
            expect(screen.getByTestId('action-help')).toHaveClass('btn-outline-info');
        });

        test('source_disabled: shows appropriate copy and actions', async () => {
            await renderPanel('source_disabled', {
                statusOverrides: {
                    title: 'Job sources are being set up',
                    message: 'Job sources exist but none are enabled yet.',
                    actions: ['suggest_company', 'help'],
                },
            });
            expect(screen.getByTestId('empty-state-source_disabled')).toBeInTheDocument();
            expect(screen.getByTestId('action-suggest_company')).toBeInTheDocument();
        });

        test('crawl_running: shows in-progress message and retry action', async () => {
            await renderPanel('crawl_running', {
                statusOverrides: {
                    title: 'Jobs are being gathered',
                    message: 'A crawl is in progress right now.',
                    actions: ['retry'],
                },
            });
            expect(screen.getByTestId('empty-state-crawl_running')).toBeInTheDocument();
            expect(screen.getByTestId('action-retry')).toBeInTheDocument();
        });

        test('crawl_failed: shows failure message with actions', async () => {
            await renderPanel('crawl_failed', {
                statusOverrides: {
                    title: 'Latest job crawl encountered a problem',
                    message: "The most recent crawl didn't complete successfully.",
                    actions: ['retry', 'suggest_company', 'help'],
                },
            });
            expect(screen.getByTestId('empty-state-crawl_failed')).toBeInTheDocument();
            expect(screen.getByText(/didn't complete successfully/i)).toBeInTheDocument();
        });

        test('crawl_stale: shows stale message', async () => {
            await renderPanel('crawl_stale', {
                statusOverrides: {
                    title: 'Job listings are stale',
                    message: 'All previous job listings have expired or been closed.',
                    actions: ['retry', 'suggest_company'],
                },
            });
            expect(screen.getByTestId('empty-state-crawl_stale')).toBeInTheDocument();
        });

        test('crawl_empty: shows no-listings message', async () => {
            await renderPanel('crawl_empty', {
                statusOverrides: {
                    title: 'No job listings yet',
                    message: 'Job sources are enabled, but no listings have been crawled yet.',
                    actions: ['retry', 'suggest_company', 'help'],
                },
            });
            expect(screen.getByTestId('empty-state-crawl_empty')).toBeInTheDocument();
        });

        test('no_preferences: shows guidance to chat with assistant', async () => {
            await renderPanel('no_preferences', {
                statusOverrides: {
                    title: 'Tell us what you\'re looking for',
                    message: "There are active job listings, but you haven't shared your preferences yet.",
                    actions: ['chat', 'help'],
                },
            });
            expect(screen.getByTestId('empty-state-no_preferences')).toBeInTheDocument();
            expect(screen.getByTestId('action-chat')).toBeInTheDocument();
        });

        test('no_matches: shows no-match guidance', async () => {
            await renderPanel('no_matches', {
                statusOverrides: {
                    title: 'No matches right now',
                    message: 'Your preferences are set and jobs are available, but none matched your criteria.',
                    actions: ['chat', 'suggest_company', 'help'],
                },
            });
            expect(screen.getByTestId('empty-state-no_matches')).toBeInTheDocument();
            expect(screen.getByText(/none matched your criteria/i)).toBeInTheDocument();
        });
    });

    describe('staff detail', () => {
        test('renders staff_detail when present', async () => {
            await renderPanel('no_source', {
                staffDetail: 'No JobSourceCatalog rows exist in the database.',
                statusOverrides: {
                    title: 'No job sources configured',
                    message: 'Test',
                    actions: [],
                },
            });
            expect(screen.getByTestId('staff-detail')).toBeInTheDocument();
            expect(screen.getByTestId('staff-detail')).toHaveTextContent(/No JobSourceCatalog/);
        });

        test('does not render staff_detail section when absent', async () => {
            await renderPanel('no_source', {
                statusOverrides: {
                    title: 'No job sources configured',
                    message: 'Test',
                    actions: [],
                },
            });
            expect(screen.queryByTestId('staff-detail')).not.toBeInTheDocument();
        });
    });

    describe('action handlers', () => {
        test('retry action re-fetches status', async () => {
            let callCount = 0;
            const mock = global.fetch as jest.Mock;
            mock.mockImplementation((url: string) => {
                callCount++;
                if (url.includes('/status/')) return Promise.resolve(jsonResponse(statusPayload('crawl_running', {
                    actions: ['retry'],
                })));
                if (url.includes('/ranked/')) return Promise.resolve(jsonResponse(rankedPayload()));
                return Promise.resolve(jsonResponse(matchPayload(0)));
            });
            render(<JobMatchPanel/>);
            await waitFor(() => expect(screen.getByTestId('empty-state-crawl_running')).toBeInTheDocument());
            const initialCalls = mock.mock.calls.length;
            fireEvent.click(screen.getByTestId('action-retry'));
            await waitFor(() => expect(mock.mock.calls.length).toBeGreaterThan(initialCalls));
        });

        test('suggest_company dispatches custom event', async () => {
            await renderPanel('no_source', {
                statusOverrides: {
                    title: 'No sources',
                    message: 'Test',
                    actions: ['suggest_company'],
                },
            });
            const handler = jest.fn();
            window.addEventListener('crank:suggest-company', handler);
            fireEvent.click(screen.getByTestId('action-suggest_company'));
            expect(handler).toHaveBeenCalled();
            window.removeEventListener('crank:suggest-company', handler);
        });

        test('help action navigates to /help/', async () => {
            await renderPanel('no_source', {
                statusOverrides: {
                    title: 'No sources',
                    message: 'Test',
                    actions: ['help'],
                },
            });
            const original = window.location;
            const mockLocation = { ...original, href: '' };
            Object.defineProperty(window, 'location', {value: mockLocation, writable: true});
            fireEvent.click(screen.getByTestId('action-help'));
            expect(mockLocation.href).toBe('/help/');
            Object.defineProperty(window, 'location', {value: original, writable: true});
        });

        test('chat action dispatches crank:assistant-open (issue #472)', async () => {
            const handler = jest.fn();
            window.addEventListener('crank:assistant-open', handler);
            await renderPanel('no_preferences', {
                statusOverrides: {
                    title: 'Tell us',
                    message: 'Test',
                    actions: ['chat'],
                },
            });
            fireEvent.click(screen.getByTestId('action-chat'));
            expect(handler).toHaveBeenCalledTimes(1);
            window.removeEventListener('crank:assistant-open', handler);
        });

        test('unknown action is a no-op (does not throw)', async () => {
            await renderPanel('no_source', {
                statusOverrides: {
                    title: 'Test',
                    message: 'Test',
                    actions: ['unknown_action'],
                },
            });
            fireEvent.click(screen.getByTestId('action-unknown_action'));
            expect(screen.getByTestId('job-match-panel')).toBeInTheDocument();
        });

        test('complete_profile action dispatches crank:assistant-open (issue #472)', async () => {
            const handler = jest.fn();
            window.addEventListener('crank:assistant-open', handler);
            await renderPanel('no_preferences', {
                statusOverrides: {
                    title: 'Complete profile',
                    message: 'Tell us your preferences',
                    actions: ['complete_profile'],
                },
            });
            fireEvent.click(screen.getByTestId('action-complete_profile'));
            expect(handler).toHaveBeenCalledTimes(1);
            window.removeEventListener('crank:assistant-open', handler);
        });
    });

    describe('accessibility', () => {
        test('uses an accessible section with aria-labelledby', async () => {
            await renderPanel('ok', { count: 1 });
            const section = screen.getByTestId('job-match-panel');
            expect(section.tagName).toBe('SECTION');
            const heading = screen.getByText('Your Job Matches');
            expect(section.getAttribute('aria-labelledby')).toBe(heading.id);
        });

        test('empty-state action buttons have accessible labels', async () => {
            await renderPanel('no_matches', {
                statusOverrides: {
                    title: 'No matches',
                    message: 'Test',
                    actions: ['chat', 'suggest_company', 'help'],
                },
            });
            expect(screen.getByLabelText('Chat with the assistant')).toBeInTheDocument();
            expect(screen.getByLabelText('Suggest a company')).toBeInTheDocument();
            expect(screen.getByLabelText('View help')).toBeInTheDocument();
        });
    });

    describe('DOMContentLoaded bootstrap', () => {
        test('the component module no longer self-mounts (issue #472: jobmatch.tsx owns the mount)', () => {
            const container = document.createElement('div');
            container.id = 'job-match-panel';
            document.body.appendChild(container);

            const mockRender = jest.fn();
            const mockCreateRoot = jest.fn(() => ({render: mockRender}));
            jest.doMock('react-dom/client', () => ({createRoot: mockCreateRoot}));

            jest.isolateModules(() => {
                require('./JobMatchPanel');
            });

            document.dispatchEvent(new Event('DOMContentLoaded'));

            expect(mockCreateRoot).not.toHaveBeenCalled();

            document.body.removeChild(container);
            jest.dontMock('react-dom/client');
        });

        test('jobmatch.tsx entry renders the panel into #job-match-panel on DOMContentLoaded', () => {
            const container = document.createElement('div');
            container.id = 'job-match-panel';
            document.body.appendChild(container);

            const mockRender = jest.fn();
            const mockCreateRoot = jest.fn(() => ({render: mockRender}));
            jest.doMock('react-dom/client', () => ({createRoot: mockCreateRoot}));

            jest.isolateModules(() => {
                require('./jobmatch');
            });

            document.dispatchEvent(new Event('DOMContentLoaded'));

            expect(mockCreateRoot).toHaveBeenCalledWith(container);
            expect(mockRender).toHaveBeenCalled();

            document.body.removeChild(container);
            jest.dontMock('react-dom/client');
        });
    });
});

// ---------------------------------------------------------------------------
// Issue #476: combined states, refresh/coverage notices, zero-match context
// ---------------------------------------------------------------------------

describe('JobMatchPanel combined states (#476)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('shows refresh notice alongside ranked matches when refreshing', async () => {
        await renderPanel('ok', {
            count: 3,
            statusOverrides: {refreshing: true},
            rankedJobs: [sampleJobMatch],
            rankedOrgs: [sampleOrgMatch],
        });
        expect(screen.getByTestId('refresh-notice')).toBeInTheDocument();
        expect(screen.getByTestId('ranked-job-matches')).toBeInTheDocument();
        expect(screen.getByTestId('ranked-org-matches')).toBeInTheDocument();
        expect(screen.getByRole('status')).toBeInTheDocument();
    });

    test('shows coverage notice with numbers alongside results for partial_coverage', async () => {
        await renderPanel('partial_coverage', {
            count: 2,
            statusOverrides: {
                coverage: {enabled_sources: 2, failing_sources: 1},
                inventory: {active_listings: 4, last_success_at: null, age_hours: null},
            },
            rankedJobs: [sampleJobMatch],
        });
        const notice = screen.getByTestId('coverage-notice');
        expect(notice).toBeInTheDocument();
        expect(notice).toHaveTextContent('1 of 2');
        expect(screen.getByTestId('ranked-job-matches')).toBeInTheDocument();
    });

    test('partial_coverage is a recognized empty-state with icon when no matches', async () => {
        await renderPanel('partial_coverage', {
            statusOverrides: {coverage: {enabled_sources: 3, failing_sources: 2}},
        });
        expect(screen.getByTestId('empty-state-partial_coverage')).toBeInTheDocument();
    });

    test('no_matches renders active constraints, inventory facts, and preview', async () => {
        await renderPanel('no_matches', {
            statusOverrides: {
                actions: ['chat', 'explore_companies', 'suggest_company', 'help'],
                active_constraints: ['Minimum salary 150,000', 'Excluded companies: acme'],
                inventory: {active_listings: 3, last_success_at: '2026-09-14T10:00:00Z', age_hours: 2.5},
                relaxation_preview: {
                    field: 'work_location',
                    label: 'Broadening work location (currently Remote)',
                    added_count: 4,
                },
            },
        });
        expect(screen.getByTestId('empty-state-no_matches')).toBeInTheDocument();
        const constraints = screen.getByTestId('active-constraints');
        expect(constraints).toHaveTextContent('Minimum salary 150,000');
        expect(constraints).toHaveTextContent('Excluded companies: acme');
        expect(screen.getByTestId('inventory-facts')).toHaveTextContent('3 active listings checked');
        // Round-1 visual critique: the preview names the concrete dimension
        // and carries an exact bounded count — no vague "about".
        expect(screen.getByTestId('relaxation-preview')).toHaveTextContent(
            'Broadening work location (currently Remote) would surface 4 more listings.',
        );
        expect(screen.getByTestId('relaxation-preview').textContent).not.toContain('about');
        // The recommended first action is primary; the rest are secondary.
        expect(screen.getByTestId('action-chat')).toHaveClass('btn-primary');
        expect(screen.getByTestId('action-explore_companies')).toHaveClass('btn-outline-info');
    });

    test('chat action becomes a secondary Focus assistant affordance while the panel is open', async () => {
        // Issue #469 re-critique: with the assistant panel open the
        // floating launcher is gone from the DOM, so the match panel's
        // "chat" action is no longer an open affordance — it relabels to a
        // quieter outline/cyan-text "Focus assistant" action (>=44px via
        // the .btn rule) instead of a redundant primary "open" button.
        openAssistant();
        await renderPanel('no_matches', {
            statusOverrides: {actions: ['chat', 'help']},
        });
        const chat = screen.getByTestId('action-chat');
        expect(chat).toHaveTextContent('Focus assistant');
        expect(chat).toHaveAccessibleName('Focus assistant');
        expect(chat).toHaveClass('btn-outline-info', 'job-match-focus-assistant');
        expect(chat).not.toHaveClass('btn-primary');
        const help = screen.getByTestId('action-help');
        expect(help).toHaveTextContent('View help');
    });

    test('chat action dispatches an assistant focus request alongside open', async () => {
        // Issue #469 review MINOR: the "Focus assistant"/chat action must ask
        // the workspace to move keyboard focus to the assistant, not merely
        // re-notify its open state.
        const dispatch = jest.spyOn(window, 'dispatchEvent');
        await renderPanel('no_matches', {
            statusOverrides: {actions: ['chat']},
        });
        fireEvent.click(screen.getByTestId('action-chat'));
        expect(dispatch).toHaveBeenCalledWith(
            expect.objectContaining({type: 'crank:assistant-open'}),
        );
        expect(dispatch).toHaveBeenCalledWith(
            expect.objectContaining({type: 'crank:assistant-focus'}),
        );
        dispatch.mockRestore();
    });

    test('explore_companies action renders with label and navigates to rankings', async () => {
        const originalHref = window.location;
        // jsdom location is read-only; replace it to observe navigation.
        Object.defineProperty(window, 'location', {
            configurable: true,
            writable: true,
            value: {href: 'https://crank.test/jobs/'},
        });
        try {
            await renderPanel('no_matches', {
                statusOverrides: {actions: ['explore_companies']},
            });
            const button = screen.getByTestId('action-explore_companies');
            expect(button).toHaveTextContent('Explore company rankings');
            fireEvent.click(button);
            expect(window.location.href).toBe('/');
        } finally {
            Object.defineProperty(window, 'location', {
                configurable: true,
                writable: true,
                value: originalHref,
            });
        }
    });

    test('empty states expose an accessible live region', async () => {
        await renderPanel('no_matches');
        const region = screen.getByTestId('empty-state-no_matches');
        expect(region).toHaveAttribute('role', 'status');
        expect(region).toHaveAttribute('aria-live', 'polite');
    });

    test('no active constraints renders no constraints block', async () => {
        await renderPanel('no_matches', {
            statusOverrides: {active_constraints: [], relaxation_preview: null},
        });
        expect(screen.queryByTestId('active-constraints')).not.toBeInTheDocument();
        expect(screen.queryByTestId('relaxation-preview')).not.toBeInTheDocument();
    });
});

describe('JobMatchPanel round-2 visual fixes (contrast + icons)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('panel pins the dark-surface contrast system (r2 contrast fix)', async () => {
        await renderPanel('no_matches', {
            statusOverrides: {
                title: 'No matches for your current requirements',
                message: 'Jobs are available, but none meet your saved requirements yet.',
                actions: ['chat', 'suggest_company', 'help'],
                active_constraints: ['Work location: Remote'],
                inventory: {active_listings: 3, last_success_at: '2026-09-14T10:00:00Z', age_hours: 2.5},
            },
        });
        const section = screen.getByTestId('job-match-panel');
        // The panel is a dark surface, so it must opt into Bootstrap's dark
        // theme tokens: the light-theme body foreground computed to
        // rgb(33,37,41) on the rgb(33,37,41) card in round 2.
        expect(section).toHaveAttribute('data-bs-theme', 'dark');
        expect(section).toHaveClass('bg-dark');
        // Every phase (loading, error, results, empty states) uses the same
        // dark-themed section.
        expect(section.className).toContain('card');
        // Muted copy inside the panel is scoped to the light-muted token so
        // it stays legible on the dark surface (popup.css pins the color).
        expect(section.querySelector('.text-muted')).not.toBeNull();
        // State copy carries a visible heading and body, not DOM-only text.
        expect(screen.getByTestId('empty-state-no_matches').textContent).toContain(
            'No matches for your current requirements',
        );
    });

    test('header refresh control renders a visible inline glyph (r2 icon fix)', async () => {
        await renderPanel('ok', {count: 1, rankedJobs: [sampleJobMatch]});
        const refresh = screen.getByTestId('job-match-refresh');
        const glyph = refresh.querySelector('svg[data-icon]');
        expect(glyph).not.toBeNull();
        expect(glyph).toHaveAttribute('data-icon', 'refresh-cw');
        expect(glyph).toHaveAttribute('aria-hidden', 'true');
        // Accessible name is preserved on the control itself.
        expect(refresh).toHaveAttribute('aria-label', 'Refresh match status');
    });

    test('empty state renders as a plain block, not a list item', async () => {
        await renderPanel('no_matches', {
            statusOverrides: {
                title: 'No matches',
                message: 'Test',
                actions: ['chat'],
            },
        });
        const empty = screen.getByTestId('empty-state-no_matches');
        // Round-2 critique: a single-item message is a plain block — no
        // leading list-bullet glyph and no indent.
        expect(empty.querySelector('svg[data-icon]')).toBeNull();
        expect(empty).toHaveTextContent('No matches');
        expect(empty).toHaveTextContent('Test');
    });
});

describe('JobMatchPanel inline icons (r2 icon fix)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('no panel markup depends on the FontAwesome webfont', async () => {
        await renderPanel('no_matches', {
            statusOverrides: {
                title: 'Test',
                message: 'Test',
                actions: ['chat', 'explore_companies', 'suggest_company', 'help'],
                active_constraints: ['Work location: Remote'],
                inventory: {active_listings: 3, last_success_at: null, age_hours: 2},
                relaxation_preview: {field: 'work_location', label: 'Broadening work location', added_count: 4},
            },
        });
        // Every glyph is an inline SVG with a data-icon name; no <i class="fa-...">
        // references remain anywhere in the panel.
        const section = screen.getByTestId('job-match-panel');
        expect(section.querySelectorAll('svg[data-icon]').length).toBeGreaterThan(0);
        expect(section.querySelectorAll('.fa-solid, i[class*="fa-"]').length).toBe(0);
        // All action glyphs resolve to real inline icons (never the fallback
        // missing for a mistyped name).
        for (const action of ['chat', 'explore_companies', 'suggest_company', 'help']) {
            const btn = screen.getByTestId(`action-${action}`);
            expect(btn.querySelector('svg[data-icon]')).not.toBeNull();
        }
    });
});

describe('JobMatchPanel signed-out state (issue #465)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('isAuthenticated=false renders a sign-in message and CTA without fetching any job-matches endpoint', async () => {
        render(<JobMatchPanel isAuthenticated={false} signInUrl="/accounts/login/?next=%2Fchat%2F"/>);

        expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent(/sign in to see personalized job matches/i);
        const cta = screen.getByTestId('job-match-sign-in-cta');
        expect(cta).toHaveAttribute('href', '/accounts/login/?next=%2Fchat%2F');
        expect(screen.queryByTestId('job-match-loading')).not.toBeInTheDocument();

        // Give any (wrongly issued) fetch a tick to fire, then assert it
        // never did: /chat/ is now public, so an anonymous visitor's browser
        // must never hit the @login_required /api/job-matches/* endpoints.
        await Promise.resolve();
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('isAuthenticated=true (default) fetches normally', async () => {
        await renderPanel('ok', {count: 3});
        expect(screen.queryByTestId('job-match-signed-out')).not.toBeInTheDocument();
        expect(global.fetch).toHaveBeenCalled();
    });
});

describe('JobMatchPanel requirement figures and states (issue #467)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        resetWorkspaceForTests();
        closeAssistant();
    });

    const jobWithRequirements = {
        ...sampleJobMatch,
        fit_score: 85.5,
        company_score: 4.2,
        coverage: 0.75,
        requirements: [
            {path: 'compensation.minimum_salary', status: 'match'},
            {path: 'industry', status: 'mismatch'},
            {path: 'work_location.max_in_office_days', status: 'unknown'},
        ],
        unsupported: ['compensation.minimum_total_compensation'],
        revision: {stale: false, generated_at: '2026-09-22T08:00:00Z'},
    };

    test('renders three separately labelled figures with explicit scales', async () => {
        await renderPanel('ok', {count: 1, rankedJobs: [jobWithRequirements]});
        const card = screen.getByTestId('ranked-job-42');
        expect(card).toHaveTextContent('Average rating');
        expect(card).toHaveTextContent('Fit');
        expect(card).toHaveTextContent('Requirement coverage');
        // Round-3: each value carries its scale so the number reads on its own.
        expect(screen.getByTestId('job-42-company-score')).toHaveTextContent('4.2 / 5');
        expect(screen.getByTestId('job-42-fit-score')).toHaveTextContent('85.5 / 100');
        expect(screen.getByTestId('job-42-coverage')).toHaveTextContent('75%');
    });

    test('renders requirement chips distinctly, unknown never styled satisfied', async () => {
        await renderPanel('ok', {count: 1, rankedJobs: [jobWithRequirements]});
        const matchChip = screen.getByTestId('requirement-compensation.minimum_salary');
        expect(matchChip).toHaveAttribute('data-status', 'match');
        expect(matchChip).toHaveClass('job-match-chip--match');
        expect(matchChip).toHaveTextContent('✓');
        const mismatchChip = screen.getByTestId('requirement-industry');
        expect(mismatchChip).toHaveAttribute('data-status', 'mismatch');
        expect(mismatchChip).toHaveClass('job-match-chip--mismatch');
        expect(mismatchChip).toHaveTextContent('✗');
        const unknownChip = screen.getByTestId('requirement-work_location.max_in_office_days');
        expect(unknownChip).toHaveAttribute('data-status', 'unknown');
        expect(unknownChip).toHaveClass('job-match-chip--unknown');
        // Unknown is amber — a distinct outcome, never the neutral gray
        // metadata treatment or the green satisfied treatment.
        expect(unknownChip).not.toHaveClass('bg-secondary', 'job-match-chip--match');
        expect(unknownChip).toHaveTextContent('?');
    });

    test('requirement chips expose status in their accessible name', async () => {
        // The status must not be conveyed by the punctuation glyph alone (which
        // screen readers announce inconsistently): the accessible name carries
        // the outcome word, e.g. "Industry: mismatch".
        await renderPanel('ok', {count: 1, rankedJobs: [jobWithRequirements]});
        expect(screen.getByTestId('requirement-industry')).toHaveAttribute('aria-label', 'Industry: mismatch');
        expect(screen.getByTestId('requirement-compensation.minimum_salary')).toHaveAttribute('aria-label', 'Minimum salary: match');
        expect(screen.getByTestId('requirement-work_location.max_in_office_days')).toHaveAttribute('aria-label', 'In-office days: unknown');
    });

    test('renders unsupported-criteria notice with human labels, never raw field names', async () => {
        await renderPanel('ok', {count: 1, rankedJobs: [jobWithRequirements]});
        const notice = screen.getByTestId('unsupported-notice');
        expect(notice).toHaveTextContent('Not used in these matches: Minimum total compensation.');
        expect(notice).not.toHaveTextContent(/could not be evaluated/i);
        // Internal field names never leak into the UI.
        expect(notice.textContent).not.toContain('minimum_total_compensation');
    });

    test('renders a single consolidated stale banner with a labelled refresh action', async () => {
        const staleJob = {...jobWithRequirements, revision: {...jobWithRequirements.revision, stale: true}};
        const secondStale = {...staleJob, listing_id: 43, title: 'Another'};
        await renderPanel('ok', {count: 1, rankedJobs: [staleJob, secondStale]});
        const stale = screen.getByTestId('stale-notice');
        expect(stale).toHaveTextContent(/newer data or priorities exist/i);
        expect(stale).not.toHaveTextContent(/recompute with your latest preferences/i);
        expect(screen.getByTestId('stale-refresh')).toHaveTextContent('Check again');
        // One banner above the list, not one per card.
        expect(screen.getAllByTestId('stale-notice')).toHaveLength(1);
    });

    test('renders the results-generated timestamp in healthy views', async () => {
        await renderPanel('ok', {count: 1, rankedJobs: [jobWithRequirements]});
        const ts = screen.getByTestId('results-timestamp');
        expect(ts).toHaveTextContent(/results generated at/i);
        expect(ts.querySelector('time')).toHaveAttribute('dateTime', '2026-09-22T08:00:00Z');
    });

    test('omits the timestamp when generated_at is unparseable', async () => {
        const bad = {...jobWithRequirements, revision: {stale: false, generated_at: 'not-a-date'}};
        await renderPanel('ok', {count: 1, rankedJobs: [bad]});
        expect(screen.queryByTestId('results-timestamp')).not.toBeInTheDocument();
    });

    test('renders no stale notice when revision absent', async () => {
        await renderPanel('ok', {count: 1, rankedJobs: [sampleJobMatch]});
        expect(screen.queryByTestId('stale-notice')).not.toBeInTheDocument();
    });

    test('renders no requirement chips when requirements absent', async () => {
        await renderPanel('ok', {count: 1, rankedJobs: [sampleJobMatch]});
        expect(screen.queryByTestId('requirement-compensation.minimum_salary')).not.toBeInTheDocument();
    });
});

describe('JobMatchPanel round-3 visual fixes (type hierarchy + grids)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        resetWorkspaceForTests();
        closeAssistant();
    });

    test('panel title uses the emphasised text-lg title class in every phase', async () => {
        await renderPanel('no_matches', {
            statusOverrides: {
                title: 'No matches',
                message: 'Test',
                actions: ['chat'],
            },
        });
        const title = screen.getByText('Your Job Matches');
        expect(title).toHaveClass('job-match-panel-title');
        // It is no longer the Bootstrap h6 (1rem) treatment that made the
        // underlined job links read as the primary heading.
        expect(title).not.toHaveClass('h6');
    });

    test('section and state headings use the base semibold heading class', async () => {
        await renderPanel('ok', {count: 1, rankedJobs: [
            {...sampleJobMatch, fit_score: 85.5, company_score: 4.2, coverage: 0.75},
        ]});
        expect(screen.getByText('Ranked Job Listings')).toHaveClass('job-match-section-heading');
    });

    test('empty-state title uses the base semibold heading class', async () => {
        await renderPanel('no_matches', {
            statusOverrides: {title: 'No matches', message: 'Test', actions: ['chat']},
        });
        expect(screen.getByText('No matches')).toHaveClass('job-match-section-heading');
    });

    test('stale banner lays out the icon, message, and refresh as a grid', async () => {
        const staleJob = {...sampleJobMatch, listing_id: 42, revision: {stale: true}};
        await renderPanel('ok', {count: 1, rankedJobs: [staleJob]});
        const banner = screen.getByTestId('stale-notice').querySelector('.job-match-stale-banner');
        expect(banner).not.toBeNull();
        expect(banner!.querySelector('.job-match-stale-icon')).not.toBeNull();
        expect(banner!.querySelector('.job-match-stale-message')).not.toBeNull();
        expect(banner!.querySelector('.job-match-stale-refresh')).not.toBeNull();
    });

    test('empty-state actions render as a single responsive grid of full-width targets', async () => {
        await renderPanel('no_matches', {
            statusOverrides: {
                title: 'No matches',
                message: 'Test',
                actions: ['chat', 'explore_companies', 'suggest_company', 'help'],
            },
        });
        const group = screen.getByRole('group', {name: 'Recovery actions'});
        expect(group).toHaveClass('job-match-actions');
        // Every action is a button inside the grid group (no ragged flex rows).
        const buttons = group.querySelectorAll('button');
        expect(buttons.length).toBe(4);
    });

    test('loading spinner carries the emphasised sky/cyan class', () => {
        (global.fetch as jest.Mock).mockReturnValue(new Promise(() => {}));
        render(<JobMatchPanel/>);
        const loading = screen.getByTestId('job-match-loading');
        const spinner = loading.querySelector('svg.job-match-loading-spinner');
        expect(spinner).not.toBeNull();
    });

    test('each score figure is a labelled mini-grid cell with tabular value for stable baselines', async () => {
        await renderPanel('ok', {count: 1, rankedJobs: [
            {...sampleJobMatch, fit_score: 85.5, company_score: 4.2, coverage: 0.75},
        ]});
        const figures = screen.getByRole('list', {name: 'Match figures'});
        expect(figures).toHaveClass('job-match-figures');
        // Three figure cells, each a mini-grid so a wrapping first label can
        // never shift its value onto a different baseline than the others.
        const cells = figures.querySelectorAll(':scope > .job-match-figure');
        expect(cells.length).toBe(3);
        cells.forEach((cell) => {
            expect(cell.querySelector('.job-match-figure-label')).not.toBeNull();
            const value = cell.querySelector('.job-match-figure-value');
            expect(value).not.toBeNull();
            expect(value).toHaveClass('job-match-figure-value');
        });
        // Company score keeps the wider first column label from wrapping past
        // the fixed label row: the value still reads as a single scalar.
        expect(screen.getByTestId('job-42-company-score')).toHaveClass('job-match-figure-value');
        expect(screen.getByTestId('job-42-fit-score')).toHaveClass('job-match-figure-value');
        expect(screen.getByTestId('job-42-coverage')).toHaveClass('job-match-figure-value');
    });
});

describe('JobMatchPanel navigation state (issue #479)', () => {
    interface Batch {
        release: () => void;
        title: string;
        generation: number | null;
    }

    // Each refresh issues three fetches (status, matches, ranked). Every
    // batch of three is held until `release()` so overlapping refreshes can
    // resolve in any order.
    function installBatchedFetch(specs: Array<{
        title: string;
        generation: number | null;
        hold: boolean;
        // Generation stamped on the ranked entries; defaults to `generation`.
        rankedGeneration?: number | null;
        rankedEmpty?: boolean;
        matchesFail?: boolean;
        withOrgs?: boolean;
        matchesEmpty?: boolean;
    }>): Batch[] {
        const batches: Batch[] = [];
        const gates: Array<Promise<void>> = [];
        let calls = 0;
        global.fetch = jest.fn().mockImplementation((url: string) => {
            const index = Math.floor(calls / 3);
            calls += 1;
            const spec = specs[index];
            if (!gates[index]) {
                gates[index] = new Promise<void>((resolve) => {
                    batches[index] = {release: resolve, title: spec.title, generation: spec.generation};
                    if (!spec.hold) resolve();
                });
            }
            const gate = gates[index];
            return gate.then(() => {
                if (url.includes('/status/')) return jsonResponse(statusPayload('ok'));
                if (url.includes('/ranked/')) {
                    const rankedGeneration = spec.rankedGeneration === undefined ? spec.generation : spec.rankedGeneration;
                    return jsonResponse(rankedPayload(spec.rankedEmpty ? [] : [{
                        ...sampleJobMatch,
                        title: spec.title,
                        revision: rankedGeneration === null ? undefined : {result_generation: rankedGeneration},
                    }], spec.withOrgs ? [sampleOrgMatch] : []));
                }
                if (spec.matchesFail) return {ok: false, status: 500, json: () => Promise.resolve({})} as Response;
                if (spec.matchesEmpty) return jsonResponse(matchPayload(0, []));
                return jsonResponse(matchPayload(1, [{
                    ...sampleJobMatch,
                    revision: spec.generation === null ? undefined : {result_generation: spec.generation},
                }]));
            });
        });
        return batches;
    }

    async function refresh(): Promise<void> {
        fireEvent.click(screen.getByTestId('job-match-refresh'));
    }

    afterEach(() => {
        jest.restoreAllMocks();
        resetWorkspaceForTests();
        window.history.replaceState(null, '', '/');
    });

    // Cross-tab receivers announce the purge, then the post-change whoami.
    const purgeAndHydrate = (detail: Record<string, unknown>) => {
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail}));
        });
    };

    test('reports the jobs surface on mount', async () => {
        installBatchedFetch([{title: 'First', generation: 1, hold: false}]);
        render(<JobMatchPanel/>);
        await screen.findByTestId('ranked-job-42');
        expect(getWorkspaceSnapshot().context?.surface).toBe('jobs');
    });

    test('reports the displayed result generation and preference revision (issue #484)', async () => {
        global.fetch = jest.fn().mockImplementation((url: string) => {
            if (url.includes('/status/')) return Promise.resolve(jsonResponse(statusPayload('ok')));
            if (url.includes('/ranked/')) {
                return Promise.resolve(jsonResponse(rankedPayload([{
                    ...sampleJobMatch, revision: {result_generation: 6},
                }])));
            }
            return Promise.resolve(jsonResponse(matchPayload(1, [{
                ...sampleJobMatch, revision: {result_generation: 6, preference_revision: 3},
            }])));
        });
        render(<JobMatchPanel/>);
        await screen.findByTestId('ranked-job-42');
        expect(getWorkspaceSnapshot().context).toMatchObject({
            surface: 'jobs', resultGeneration: 6, preferenceRevision: 3,
        });
    });

    const revisionFetch = (block: Record<string, unknown>) => {
        global.fetch = jest.fn().mockImplementation((url: string) => {
            if (url.includes('/status/')) return Promise.resolve(jsonResponse(statusPayload('ok')));
            if (url.includes('/ranked/')) {
                return Promise.resolve(jsonResponse(rankedPayload([{...sampleJobMatch, revision: {result_generation: 6}}])));
            }
            return Promise.resolve(jsonResponse(matchPayload(1, [{...sampleJobMatch, revision: {result_generation: 6, ...block}}])));
        });
    };

    test('stored rows behind an edit made in this tab report the current revision, not the lagging one', async () => {
        revisionFetch({preference_revision: 3, stale: true});
        act(() => setPrioritiesRevision(9));
        render(<JobMatchPanel/>);
        await screen.findByTestId('ranked-job-42');
        expect(getWorkspaceSnapshot().context).toMatchObject({surface: 'jobs', preferenceRevision: 9});
    });

    test('an edit made after load is reported immediately, before the refetch lands', async () => {
        revisionFetch({preference_revision: 3, stale: true});
        render(<JobMatchPanel/>);
        await screen.findByTestId('ranked-job-42');
        expect(getWorkspaceSnapshot().context).not.toHaveProperty('preferenceRevision');
        act(() => setPrioritiesRevision(7));
        expect(getWorkspaceSnapshot().context).toMatchObject({preferenceRevision: 7});
    });

    test('stale rows with no known edit report no preference revision rather than a lagging one', async () => {
        revisionFetch({preference_revision: 3, stale: true});
        render(<JobMatchPanel/>);
        await screen.findByTestId('ranked-job-42');
        expect(getWorkspaceSnapshot().context).toMatchObject({resultGeneration: 6});
        expect(getWorkspaceSnapshot().context).not.toHaveProperty('preferenceRevision');
    });

    test('rows that are current report their own revision even when this tab saved an older one', async () => {
        revisionFetch({preference_revision: 3});
        act(() => setPrioritiesRevision(2));
        render(<JobMatchPanel/>);
        await screen.findByTestId('ranked-job-42');
        expect(getWorkspaceSnapshot().context).toMatchObject({preferenceRevision: 3});
    });

    test('nothing is reported when the payloads carry no generation or revision', async () => {
        installBatchedFetch([{title: 'Bare', generation: null, hold: false}]);
        render(<JobMatchPanel/>);
        await screen.findByText('Bare');
        const context = getWorkspaceSnapshot().context;
        expect(context).not.toHaveProperty('resultGeneration');
        expect(context).not.toHaveProperty('preferenceRevision');
    });

    test('a rejected older result set does not change the reported generation', async () => {
        installBatchedFetch([
            {title: 'Generation five', generation: 5, hold: false},
            {title: 'Generation four', generation: 4, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Generation five');
        expect(getWorkspaceSnapshot().context?.resultGeneration).toBe(5);
        await refreshAndSettle(3);
        expect(getWorkspaceSnapshot().context?.resultGeneration).toBe(5);
    });

    test('an older overlapping request that resolves last never replaces newer data', async () => {
        const batches = installBatchedFetch([
            {title: 'Stale', generation: 1, hold: true},
            {title: 'Fresh', generation: 1, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await waitFor(() => expect(batches[0]).toBeDefined());
        // A purge issues a second request while the first is still in flight.
        purgeAndHydrate({authenticated: true, username: 'bob'});
        await screen.findByText('Fresh');
        await act(async () => {
            batches[0].release();
            await new Promise((resolve) => setTimeout(resolve, 20));
        });
        expect(screen.getByText('Fresh')).toBeInTheDocument();
        // Scoped to the results: the legend (issue #473) also defines the word "Stale".
        const results = within(screen.getByTestId('ranked-job-matches'));
        expect(results.getByText('Fresh')).toBeInTheDocument();
        expect(results.queryByText('Stale')).not.toBeInTheDocument();
    });

    test('a superseded request that fails late does not surface an error', async () => {
        let calls = 0;
        let rejectOld: (error: Error) => void = () => undefined;
        global.fetch = jest.fn().mockImplementation((url: string) => {
            calls += 1;
            if (calls <= 3) {
                return new Promise((_resolve, reject) => { rejectOld = reject; });
            }
            if (url.includes('/status/')) return Promise.resolve(jsonResponse(statusPayload('ok')));
            if (url.includes('/ranked/')) return Promise.resolve(jsonResponse(rankedPayload([{...sampleJobMatch, title: 'Recovered'}])));
            return Promise.resolve(jsonResponse(matchPayload(1, [sampleJobMatch])));
        });
        render(<JobMatchPanel/>);
        purgeAndHydrate({authenticated: true, username: 'bob'});
        await screen.findByText('Recovered');
        await act(async () => {
            rejectOld(new Error('late boom'));
            await new Promise((resolve) => setTimeout(resolve, 20));
        });
        expect(screen.queryByText('late boom')).not.toBeInTheDocument();
        expect(screen.getByText('Recovered')).toBeInTheDocument();
        expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument();
    });

    test('a lower result_generation never replaces a higher one', async () => {
        installBatchedFetch([
            {title: 'Generation five', generation: 5, hold: false},
            {title: 'Generation four', generation: 4, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Generation five');
        await refresh();
        await waitFor(() => expect((global.fetch as jest.Mock).mock.calls.length).toBeGreaterThanOrEqual(6));
        await waitFor(() => expect(screen.getByTestId('job-match-refresh')).toBeInTheDocument());
        expect(screen.getByText('Generation five')).toBeInTheDocument();
        expect(screen.queryByText('Generation four')).not.toBeInTheDocument();
    });

    async function refreshAndSettle(callsBefore: number): Promise<void> {
        await refresh();
        await waitFor(() => expect((global.fetch as jest.Mock).mock.calls.length).toBeGreaterThanOrEqual(callsBefore + 3));
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    }

    test('a job payload without a generation is unguarded when the matches page carries none', async () => {
        installBatchedFetch([
            {title: 'Old', generation: 5, hold: false},
            {title: 'Ungenerated', generation: null, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Old');
        await refreshAndSettle(3);
        expect(await screen.findByText('Ungenerated')).toBeInTheDocument();
        expect(screen.queryByText('Old')).not.toBeInTheDocument();
    });

    test('a job payload without a generation is rejected when the matches page is older', async () => {
        installBatchedFetch([
            {title: 'Old', generation: 5, hold: false},
            {title: 'Ungenerated', generation: 4, rankedGeneration: null, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Old');
        await refreshAndSettle(3);
        expect(screen.getByText('Old')).toBeInTheDocument();
        expect(screen.queryByText('Ungenerated')).not.toBeInTheDocument();
    });

    test('an org-only refresh with zero jobs replaces a displayed generation and Refresh keeps working', async () => {
        installBatchedFetch([
            {title: 'Gen five', generation: 5, withOrgs: true, hold: false},
            {title: 'x', generation: null, rankedEmpty: true, withOrgs: true, matchesEmpty: true, hold: false},
            {title: 'x', generation: null, rankedEmpty: true, withOrgs: true, matchesEmpty: true, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Gen five');
        await refreshAndSettle(3);
        expect(screen.queryByText('Gen five')).not.toBeInTheDocument();
        expect(screen.getByText('Acme Corp')).toBeInTheDocument();
        const callsBefore = (global.fetch as jest.Mock).mock.calls.length;
        await refreshAndSettle(6);
        expect((global.fetch as jest.Mock).mock.calls.length).toBeGreaterThan(callsBefore);
    });

    test('a zero-job refresh at a newer matches generation applies with no organizations', async () => {
        installBatchedFetch([
            {title: 'Gen five', generation: 5, hold: false},
            {title: 'x', generation: 6, rankedEmpty: true, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Gen five');
        await refreshAndSettle(3);
        expect(screen.queryByText('Gen five')).not.toBeInTheDocument();
    });

    test('a mixed payload guards on the job generation and ignores ungenerated organizations', async () => {
        installBatchedFetch([
            {title: 'Gen five', generation: 5, withOrgs: true, hold: false},
            {title: 'Gen four', generation: 4, withOrgs: true, hold: false},
            {title: 'Gen six', generation: 6, withOrgs: true, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Gen five');
        await refreshAndSettle(3);
        expect(screen.getByText('Gen five')).toBeInTheDocument();
        expect(screen.queryByText('Gen four')).not.toBeInTheDocument();
        await refreshAndSettle(6);
        expect(await screen.findByText('Gen six')).toBeInTheDocument();
    });

    test('a lower generation on the ranked payload is rejected even when the matches page looks newer', async () => {
        installBatchedFetch([
            {title: 'Gen five', generation: 5, hold: false},
            {title: 'Ranked four', generation: 6, rankedGeneration: 4, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Gen five');
        await refreshAndSettle(3);
        expect(screen.getByText('Gen five')).toBeInTheDocument();
        expect(screen.queryByText('Ranked four')).not.toBeInTheDocument();
    });

    test('an endpoint generation mismatch keeps the displayed list', async () => {
        installBatchedFetch([
            {title: 'Gen five', generation: 5, hold: false},
            {title: 'Mismatched pair', generation: 6, rankedGeneration: 7, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Gen five');
        await refreshAndSettle(3);
        expect(screen.getByText('Gen five')).toBeInTheDocument();
        expect(screen.queryByText('Mismatched pair')).not.toBeInTheDocument();
    });

    test('a newer ranked generation applies even when the matches page fails', async () => {
        installBatchedFetch([
            {title: 'Gen five', generation: 5, hold: false},
            {title: 'Gen six', generation: 6, matchesFail: true, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Gen five');
        await refreshAndSettle(3);
        await screen.findByText('Gen six');
    });

    test('a failed ranked fetch never blanks the displayed list', async () => {
        installBatchedFetch([{title: 'Gen five', generation: 5, hold: false}]);
        render(<JobMatchPanel/>);
        await screen.findByText('Gen five');
        (global.fetch as jest.Mock).mockImplementation((url: string) => Promise.resolve(
            url.includes('/status/') ? jsonResponse(statusPayload('ok'))
                : url.includes('/ranked/') ? {ok: false, status: 500, json: () => Promise.resolve({})} as Response
                    : jsonResponse(matchPayload(1, [{...sampleJobMatch, revision: {result_generation: 9}}])),
        ));
        await refresh();
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
        expect(screen.getByText('Gen five')).toBeInTheDocument();
    });

    test('inconsistent generations inside one ranked payload keep the displayed list', async () => {
        installBatchedFetch([{title: 'Gen five', generation: 5, hold: false}]);
        render(<JobMatchPanel/>);
        await screen.findByText('Gen five');
        (global.fetch as jest.Mock).mockImplementation((url: string) => Promise.resolve(
            url.includes('/status/') ? jsonResponse(statusPayload('ok'))
                : url.includes('/ranked/') ? jsonResponse(rankedPayload([
                    {...sampleJobMatch, title: 'Split A', revision: {result_generation: 6}},
                    {...sampleJobMatch, listing_id: 43, title: 'Split B', revision: {result_generation: 7}},
                ]))
                    : jsonResponse(matchPayload(1, [{...sampleJobMatch, revision: {result_generation: 7}}])),
        ));
        await refresh();
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
        expect(screen.getByText('Gen five')).toBeInTheDocument();
        expect(screen.queryByText('Split A')).not.toBeInTheDocument();
    });

    test('an empty ranked list at an equal matches generation applies', async () => {
        installBatchedFetch([
            {title: 'Gen five', generation: 5, hold: false},
            {title: 'x', generation: 5, rankedEmpty: true, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Gen five');
        await refreshAndSettle(3);
        expect(screen.queryByText('Gen five')).not.toBeInTheDocument();
    });

    test('an empty ranked list at a lower matches generation keeps the displayed list', async () => {
        installBatchedFetch([
            {title: 'Gen five', generation: 5, hold: false},
            {title: 'x', generation: 4, rankedEmpty: true, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Gen five');
        await refreshAndSettle(3);
        expect(screen.getByText('Gen five')).toBeInTheDocument();
    });

    test('crank:private-state-purged drops the data and reloads', async () => {
        installBatchedFetch([
            {title: 'Alice data', generation: 1, hold: false},
            {title: 'Bob data', generation: 1, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Alice data');
        purgeAndHydrate({authenticated: true, username: 'bob'});
        expect(screen.queryByText('Alice data')).not.toBeInTheDocument();
        await screen.findByText('Bob data');
    });

    test('a purge holds the private re-fetch until the hydration arrives (issue #479)', async () => {
        installBatchedFetch([
            {title: 'Alice data', generation: 1, hold: false},
            {title: 'Bob data', generation: 1, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Alice data');
        const before = (global.fetch as jest.Mock).mock.calls.length;
        act(() => { document.dispatchEvent(new CustomEvent('crank:private-state-purged')); });
        await new Promise((resolve) => setTimeout(resolve, 20));
        expect((global.fetch as jest.Mock).mock.calls.length).toBe(before);
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true, username: 'bob'}}));
        });
        await screen.findByText('Bob data');
        // Hydrations that are not following a purge never refetch.
        const after = (global.fetch as jest.Mock).mock.calls.length;
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true, username: 'bob'}}));
        });
        expect((global.fetch as jest.Mock).mock.calls.length).toBe(after);
    });

    test('another tab signing out shows the signed-out state, not an error with a retry (issue #479)', async () => {
        installBatchedFetch([{title: 'Alice data', generation: 1, hold: false}]);
        render(<JobMatchPanel/>);
        await screen.findByText('Alice data');
        const before = (global.fetch as jest.Mock).mock.calls.length;
        purgeAndHydrate({authenticated: false, username: null});
        expect(await screen.findByTestId('job-match-signed-out')).toBeInTheDocument();
        expect(screen.queryByText(/couldn’t load your job matches/i)).not.toBeInTheDocument();
        expect(screen.queryByRole('button', {name: /try again/i})).not.toBeInTheDocument();
        expect((global.fetch as jest.Mock).mock.calls.length).toBe(before);
    });

    test('another tab signing in turns a signed-out panel into the loaded matches (issue #479)', async () => {
        installBatchedFetch([{title: 'Bob data', generation: 1, hold: false}]);
        render(<JobMatchPanel isAuthenticated={false}/>);
        expect(screen.getByTestId('job-match-signed-out')).toBeInTheDocument();
        expect(global.fetch).not.toHaveBeenCalled();
        purgeAndHydrate({authenticated: true, username: 'bob'});
        await screen.findByText('Bob data');
    });

    test('a failed whoami after a purge keeps the last known account and retries (issue #479)', async () => {
        installBatchedFetch([
            {title: 'Alice data', generation: 1, hold: false},
            {title: 'Alice again', generation: 1, hold: false},
        ]);
        render(<JobMatchPanel/>);
        await screen.findByText('Alice data');
        purgeAndHydrate({authenticated: false, username: null, unobserved: true});
        await screen.findByText('Alice again');
        expect(screen.queryByTestId('job-match-signed-out')).not.toBeInTheDocument();
    });

    test('a prop change from signed-in to signed-out is followed (issue #479)', async () => {
        installBatchedFetch([{title: 'Alice data', generation: 1, hold: false}]);
        const {rerender} = render(<JobMatchPanel isAuthenticated/>);
        await screen.findByText('Alice data');
        rerender(<JobMatchPanel isAuthenticated={false}/>);
        expect(screen.getByTestId('job-match-signed-out')).toBeInTheDocument();
    });

    test('keeps the list with a retry notice when a refresh fails, and only for the latest request', async () => {
        installBatchedFetch([{title: 'Initial', generation: 1, hold: false}]);
        render(<JobMatchPanel/>);
        await screen.findByText('Initial');
        (global.fetch as jest.Mock).mockRejectedValue(new Error('boom'));
        await refresh();
        // The list stays and the failure is a notice over it (issue #485).
        expect(await screen.findByTestId('refresh-failed-notice')).toHaveTextContent('Couldn’t refresh. Showing results from');
        expect(screen.getByText('Initial')).toBeInTheDocument();
        expect(screen.queryByTestId('job-match-error')).not.toBeInTheDocument();
    });

    test('restores the saved scroll position once results are ready', async () => {
        window.history.replaceState({crankPosition: {scrollY: 300, anchor: null}}, '', '/');
        const scrollTo = jest.fn();
        window.scrollTo = scrollTo as unknown as typeof window.scrollTo;
        installBatchedFetch([{title: 'Initial', generation: 1, hold: false}]);
        render(<JobMatchPanel/>);
        await screen.findByText('Initial');
        expect(scrollTo).toHaveBeenCalledWith({top: 300, behavior: 'instant'});
    });

    test('unmounting cancels the in-flight request', async () => {
        installBatchedFetch([{title: 'Held', generation: 1, hold: true}]);
        const {unmount} = render(<JobMatchPanel/>);
        unmount();
        expect(() => act(() => { document.dispatchEvent(new CustomEvent('crank:private-state-purged')); })).not.toThrow();
    });
});

describe('JobMatchPanel priorities integration (issue #480)', () => {
    beforeEach(() => {
        resetWorkspaceForTests();
        global.fetch = jest.fn();
    });

    const statusCalls = () =>
        (global.fetch as jest.Mock).mock.calls.filter((c) => String(c[0]).includes('/api/job-matches/status/')).length;

    test('edit_priorities opens the main editor through the store', async () => {
        await renderPanel('no_preferences', {statusOverrides: {actions: ['edit_priorities', 'chat']}});
        const button = screen.getByTestId('action-edit_priorities');
        expect(button).toHaveTextContent('Edit your priorities');
        expect(button).toHaveClass('btn-primary');
        fireEvent.click(button);
        expect(getWorkspaceSnapshot().prioritiesEditorOpenIn).toBe('main');
    });

    test('a priorities revision change refetches once; repeats and purge resets do not', async () => {
        await renderPanel('ok');
        const base = statusCalls();
        act(() => setPrioritiesRevision(4));
        await waitFor(() => expect(statusCalls()).toBe(base + 1));
        act(() => setPrioritiesRevision(4));
        act(() => resetPriorities());
        act(() => setPrioritiesRevision(4));
        await waitFor(() => expect(statusCalls()).toBe(base + 2));
        act(() => setPrioritiesRevision(5));
        await waitFor(() => expect(statusCalls()).toBe(base + 3));
    });
});

describe('JobMatchPanel quiet refresh and truthful states (issue #485)', () => {
    type Part = 'status' | 'matches' | 'ranked';
    const STALE = '2026-09-22T08:00:00Z';
    let status: () => Response | Promise<Response>;
    let matches: () => Response | Promise<Response>;
    let ranked: () => Response | Promise<Response>;
    let fetchMock: jest.Mock;

    const job = (generation: number | null, extra: Record<string, unknown> = {}) => ({
        ...sampleJobMatch,
        revision: generation === null ? undefined : {result_generation: generation, generated_at: STALE, ...extra},
    });
    const healthy = (generation: number | null = 3, extra: Record<string, unknown> = {}) => {
        status = () => jsonResponse(statusPayload('ok'));
        matches = () => jsonResponse(matchPayload(1, [job(generation, extra)]));
        ranked = () => jsonResponse(rankedPayload([job(generation, extra)]));
    };
    // A followed redirect: by default one that ends on the sign-in page.
    const LOGIN_URL = 'http://localhost/accounts/login/?next=%2Fchat%2F';
    const redirected = (url: string = LOGIN_URL, body = '<html>login</html>', contentType = 'text/html'): Response => {
        const res = new Response(body, {status: 200, headers: {'Content-Type': contentType}});
        Object.defineProperty(res, 'redirected', {value: true});
        Object.defineProperty(res, 'url', {value: url});
        return res;
    };
    const parts: Record<Part, (fn: () => Response | Promise<Response>) => void> = {
        status: (fn) => { status = fn; },
        matches: (fn) => { matches = fn; },
        ranked: (fn) => { ranked = fn; },
    };
    const settled = () => waitFor(() => expect(document.querySelector('[aria-busy]')).toBeNull());
    const pressRefresh = () => fireEvent.click(screen.getByTestId('job-match-refresh'));

    beforeEach(() => {
        healthy();
        fetchMock = jest.fn((url: string) => Promise.resolve(
            url.includes('/status/') ? status() : url.includes('/ranked/') ? ranked() : matches(),
        ));
        global.fetch = fetchMock as unknown as typeof fetch;
    });
    afterEach(() => {
        jest.restoreAllMocks();
        resetWorkspaceForTests();
        window.history.replaceState(null, '', '/');
    });

    async function showResults() {
        render(<JobMatchPanel/>);
        await screen.findByTestId('ranked-job-42');
    }

    test('a refresh keeps the same nodes, the open legend and the focused control, with no skeleton', async () => {
        await showResults();
        const card = screen.getByTestId('ranked-job-42');
        const legend = screen.getByTestId('job-match-legend') as HTMLDetailsElement;
        legend.open = true;
        const button = screen.getByTestId('job-match-refresh');
        button.focus();
        let release!: () => void;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        const original = ranked;
        ranked = () => gate.then(() => original());
        fireEvent.click(button);
        expect(screen.queryByTestId('job-match-loading')).not.toBeInTheDocument();
        expect(document.querySelector('[aria-busy="true"]')).not.toBeNull();
        expect(screen.getByTestId('ranked-job-42')).toBe(card);
        release();
        await settled();
        expect(screen.queryByTestId('job-match-loading')).not.toBeInTheDocument();
        expect(screen.getByTestId('ranked-job-42')).toBe(card);
        expect((screen.getByTestId('job-match-legend') as HTMLDetailsElement).open).toBe(true);
        expect(button).toHaveFocus();
    });

    test('a priorities revision change and the retry action also re-read behind the list', async () => {
        await showResults();
        const card = screen.getByTestId('ranked-job-42');
        const before = fetchMock.mock.calls.length;
        act(() => setPrioritiesRevision(11));
        await waitFor(() => expect(fetchMock.mock.calls.length).toBe(before + 3));
        await settled();
        expect(screen.queryByTestId('job-match-loading')).not.toBeInTheDocument();
        expect(screen.getByTestId('ranked-job-42')).toBe(card);
    });

    test('the first load shows the skeleton', () => {
        fetchMock.mockReturnValue(new Promise(() => {}));
        render(<JobMatchPanel/>);
        expect(screen.getByTestId('job-match-loading')).toBeInTheDocument();
    });

    test('the first load failing still shows the error panel and Try again loads with the skeleton', async () => {
        status = () => jsonResponse({}, 500);
        render(<JobMatchPanel/>);
        expect(await screen.findByTestId('job-match-error')).toBeInTheDocument();
        expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument();
        healthy();
        fireEvent.click(screen.getByLabelText('Retry loading matches'));
        expect(screen.getByTestId('job-match-loading')).toBeInTheDocument();
        await screen.findByTestId('ranked-job-42');
    });

    describe.each([
        ['with a result generation', 5],
        ['without a result generation (live results)', null],
    ])('a failed refresh %s', (_label, generation) => {
        const failures: Array<[string, Part, () => Response | Promise<Response>]> = [
            ['the status read returns 503', 'status', () => jsonResponse({}, 503)],
            ['the status read cannot be parsed', 'status', () => new Response('<html>', {status: 200})],
            ['the ranked read returns 503', 'ranked', () => jsonResponse({}, 503)],
            ['the ranked read cannot be parsed', 'ranked', () => new Response('<html>', {status: 200})],
            ['the matches read cannot be parsed', 'matches', () => new Response('<html>', {status: 200})],
            ['the network is down', 'ranked', () => Promise.reject(new Error('Network down'))],
        ];
        test.each(failures)('keeps the list and says so when %s', async (_name, part, failure) => {
            healthy(generation);
            await showResults();
            const card = screen.getByTestId('ranked-job-42');
            parts[part](failure);
            pressRefresh();
            const notice = await screen.findByTestId('refresh-failed-notice');
            expect(notice).toHaveAttribute('role', 'status');
            expect(notice).toHaveTextContent('Couldn’t refresh. Showing results from');
            expect(notice).toHaveTextContent('Try again');
            expect(screen.queryByTestId('job-match-error')).not.toBeInTheDocument();
            expect(screen.queryByTestId('job-match-loading')).not.toBeInTheDocument();
            expect(screen.getByTestId('ranked-job-42')).toBe(card);
        });

        test('Try again re-reads, clears the notice and puts focus on the heading', async () => {
            healthy(generation);
            await showResults();
            ranked = () => jsonResponse({}, 503);
            pressRefresh();
            const retry = within(await screen.findByTestId('refresh-failed-notice')).getByRole('button', {name: 'Try again'});
            healthy(generation);
            retry.focus();
            fireEvent.click(retry);
            await waitFor(() => expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument());
            await waitFor(() => expect(screen.getByRole('heading', {name: 'Your Job Matches'})).toHaveFocus());
        });
    });

    test('a failed refresh says when the shown results were loaded if they carry no time', async () => {
        healthy(null);
        await showResults();
        ranked = () => jsonResponse({}, 503);
        pressRefresh();
        expect(await screen.findByTestId('refresh-failed-notice')).toHaveTextContent(/Showing results from \S+/);
    });

    test('a failed refresh over the stored-count view and the empty state keeps what was shown', async () => {
        ranked = () => jsonResponse(rankedPayload());
        matches = () => jsonResponse(matchPayload(4));
        render(<JobMatchPanel/>);
        await screen.findByText(/job matches ready to review/);
        ranked = () => jsonResponse({}, 503);
        pressRefresh();
        expect(await screen.findByTestId('refresh-failed-notice')).toBeInTheDocument();
        expect(screen.getByText(/job matches ready to review/)).toBeInTheDocument();
        cleanup();

        status = () => jsonResponse(statusPayload('no_matches', {actions: ['retry']}));
        ranked = () => jsonResponse(rankedPayload());
        matches = () => jsonResponse(matchPayload(0));
        render(<JobMatchPanel/>);
        await screen.findByTestId('empty-state-no_matches');
        status = () => jsonResponse({}, 503);
        pressRefresh();
        expect(await screen.findByTestId('refresh-failed-notice')).toBeInTheDocument();
        expect(screen.getByTestId('empty-state-no_matches')).toBeInTheDocument();
    });

    test('a failed refresh is the only notice: the stale banner waits behind it', async () => {
        healthy(3, {stale: true});
        await showResults();
        expect(screen.getByTestId('stale-notice')).toBeInTheDocument();
        ranked = () => jsonResponse({}, 503);
        pressRefresh();
        await screen.findByTestId('refresh-failed-notice');
        expect(screen.queryByTestId('stale-notice')).not.toBeInTheDocument();
    });

    describe.each([
        ['a login redirect', redirected],
        ['a 401', () => jsonResponse({detail: 'Authentication required'}, 401)],
    ])('%s', (_label, expired) => {
        test.each<Part>(['status', 'matches', 'ranked'])('on the %s read at first load shows the signed-out card', async (part) => {
            parts[part](expired);
            render(<JobMatchPanel/>);
            expect(await screen.findByTestId('job-match-signed-out')).toBeInTheDocument();
            expect(screen.queryByTestId('job-match-error')).not.toBeInTheDocument();
        });

        test.each<Part>(['status', 'matches', 'ranked'])('on the %s read during a refresh replaces the results with the signed-out card', async (part) => {
            await showResults();
            parts[part](expired);
            pressRefresh();
            expect(await screen.findByTestId('job-match-sign-in-cta')).toBeInTheDocument();
            expect(screen.queryByTestId('job-match-error')).not.toBeInTheDocument();
            expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument();
            expect(screen.queryByText('Senior Engineer')).not.toBeInTheDocument();
            expect(screen.queryByTestId('results-timestamp')).not.toBeInTheDocument();
        });
    });

    test('a purge removes the last-known results, notice and age, and a failed re-read afterwards is the error panel', async () => {
        await showResults();
        ranked = () => jsonResponse({}, 503);
        pressRefresh();
        await screen.findByTestId('refresh-failed-notice');
        act(() => { document.dispatchEvent(new CustomEvent('crank:private-state-purged')); });
        expect(screen.getByTestId('job-match-loading')).toBeInTheDocument();
        expect(screen.queryByText('Senior Engineer')).not.toBeInTheDocument();
        expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument();
        expect(screen.queryByTestId('results-timestamp')).not.toBeInTheDocument();
        status = () => jsonResponse({}, 503);
        act(() => { document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true}})); });
        expect(await screen.findByTestId('job-match-error')).toBeInTheDocument();
        expect(screen.queryByText('Senior Engineer')).not.toBeInTheDocument();
        expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument();
    });

    test('a session that expires during a refresh leaves nothing for the next account', async () => {
        await showResults();
        ranked = redirected;
        pressRefresh();
        await screen.findByTestId('job-match-signed-out');
        // The next account signs in: the panel starts again from the skeleton.
        healthy(7);
        act(() => { document.dispatchEvent(new CustomEvent('crank:private-state-purged')); });
        act(() => { document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true}})); });
        expect(screen.getByTestId('job-match-loading')).toBeInTheDocument();
        await screen.findByTestId('ranked-job-42');
    });

    test('a purge over the count-only view leaves nothing of the earlier count for the next account', async () => {
        // First account: the stored list has 77 matches and nothing is ranked.
        parts.ranked(() => jsonResponse(rankedPayload()));
        parts.matches(() => jsonResponse(matchPayload(77, [job(3)])));
        render(<JobMatchPanel/>);
        expect(await screen.findByText(/job matches ready to review/)).toHaveTextContent('You have 77 job matches ready to review.');
        expect(screen.getByTestId('results-timestamp')).toBeInTheDocument();
        act(() => { document.dispatchEvent(new CustomEvent('crank:private-state-purged')); });
        // Next account: the matches page read fails and nothing is ranked.
        parts.matches(() => jsonResponse({}, 500));
        act(() => { document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true}})); });
        await waitFor(() => expect(screen.queryByTestId('job-match-loading')).not.toBeInTheDocument());
        expect(screen.getByTestId('job-match-panel')).toBeInTheDocument();
        expect(screen.queryByText(/job matches ready to review/)).not.toBeInTheDocument();
        expect(screen.queryByText(/77/)).not.toBeInTheDocument();
        expect(screen.queryByTestId('results-timestamp')).not.toBeInTheDocument();
    });

    describe('stale results', () => {
        test('stale and pending: says the results are waiting to be updated and offers no action', async () => {
            healthy(3, {stale: true, pending: true});
            await showResults();
            const notice = screen.getByTestId('stale-notice');
            // `pending` says a recompute is switched on, not that one is running.
            expect(notice).toHaveTextContent('Newer data or priorities exist. These results are waiting to be updated.');
            expect(notice).not.toHaveTextContent('being updated');
            expect(notice).toHaveAttribute('role', 'status');
            expect(within(notice).queryByRole('button')).not.toBeInTheDocument();
            expect(notice).not.toHaveTextContent(/recompute/i);
        });

        test('stale and not pending: offers Check again and leaves the time to the timestamp line', async () => {
            healthy(3, {stale: true, pending: false});
            await showResults();
            const notice = screen.getByTestId('stale-notice');
            expect(notice).toHaveTextContent(/^Newer data or priorities exist\.\s*Check again$/);
            expect(notice).not.toHaveTextContent('computed');
            expect(notice).not.toHaveTextContent(/recompute with your latest preferences/i);
            const before = fetchMock.mock.calls.length;
            fireEvent.click(within(notice).getByRole('button', {name: 'Check again'}));
            await waitFor(() => expect(fetchMock.mock.calls.length).toBe(before + 3));
            expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/refresh/'))).toBe(false);
        });

        test('without a generated-at time the sentence about when is left out', async () => {
            healthy(3, {stale: true, generated_at: null});
            await showResults();
            expect(screen.getByTestId('stale-notice')).toHaveTextContent(/^Newer data or priorities exist\.\s*Check again$/);
        });

        test('Check again that clears the stale flag moves focus to the heading instead of <body>', async () => {
            healthy(3, {stale: true});
            await showResults();
            const button = within(screen.getByTestId('stale-notice')).getByRole('button', {name: 'Check again'});
            button.focus();
            healthy(3);
            fireEvent.click(button);
            await waitFor(() => expect(screen.queryByTestId('stale-notice')).not.toBeInTheDocument());
            await waitFor(() => expect(screen.getByRole('heading', {name: 'Your Job Matches'})).toHaveFocus());
        });

        test('the stored-count view says the same', async () => {
            ranked = () => jsonResponse(rankedPayload());
            matches = () => jsonResponse(matchPayload(2, [{id: 1, revision: {stale: true, pending: true, generated_at: STALE}}]));
            render(<JobMatchPanel/>);
            expect(await screen.findByTestId('stale-notice')).toHaveTextContent('Newer data or priorities exist. These results are waiting to be updated.');
        });
    });

    describe('inventory and coverage', () => {
        const inventory = {active_listings: 12, last_success_at: '2026-09-21T10:00:00Z', age_hours: 20.5};

        test('the ok state shows the active listings and the date of the last successful refresh', async () => {
            status = () => jsonResponse(statusPayload('ok', {inventory}));
            await showResults();
            const age = screen.getByTestId('inventory-age');
            expect(age).toHaveTextContent('12 active listings checked · inventory last refreshed');
            expect(age.textContent).not.toMatch(/verified/i);
        });

        test('a never-succeeded inventory shows only the count, and the ok state without inventory shows nothing', async () => {
            status = () => jsonResponse(statusPayload('ok', {inventory: {active_listings: 1, last_success_at: null, age_hours: null}}));
            await showResults();
            expect(screen.getByTestId('inventory-age')).toHaveTextContent(/^1 active listing checked$/);
            cleanup();
            status = () => jsonResponse(statusPayload('ok'));
            await showResults();
            expect(screen.queryByTestId('inventory-age')).not.toBeInTheDocument();
        });

        test('partial coverage keeps its count copy and the inventory line', async () => {
            status = () => jsonResponse(statusPayload('partial_coverage', {coverage: {enabled_sources: 3, failing_sources: 1}, inventory}));
            await showResults();
            const notice = screen.getByTestId('coverage-notice');
            expect(notice).toHaveTextContent('Coverage is limited: 1 of 3 job sources aren’t returning listings right now');
            expect(notice).toHaveTextContent('12 active listings checked');
            expect(screen.queryByTestId('inventory-age')).not.toBeInTheDocument();
        });

        test('every source failing says no source is returning listings, above the last-known results', async () => {
            status = () => jsonResponse(statusPayload('partial_coverage', {coverage: {enabled_sources: 2, failing_sources: 2}, inventory}));
            await showResults();
            const notice = screen.getByTestId('coverage-notice');
            expect(notice).toHaveTextContent('No job source is returning listings right now. These are the last results we have');
            expect(notice).not.toHaveTextContent('2 of 2');
            expect(notice.compareDocumentPosition(screen.getByTestId('ranked-job-matches')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        });
    });

    describe('a read in flight is visible and single-flight (F1)', () => {
        const hold = () => {
            let release!: () => void;
            const gate = new Promise<void>((resolve) => { release = resolve; });
            return {gate, release};
        };
        const views: Array<[string, () => void, string]> = [
            ['the results view', () => healthy(3), 'ranked-job-42'],
            ['the stored-count view', () => {
                ranked = () => jsonResponse(rankedPayload());
                matches = () => jsonResponse(matchPayload(2));
            }, 'job-match-refresh'],
            ['the empty state', () => {
                status = () => jsonResponse(statusPayload('no_matches', {actions: ['retry']}));
                ranked = () => jsonResponse(rankedPayload());
                matches = () => jsonResponse(matchPayload(0));
            }, 'empty-state-no_matches'],
        ];

        test.each(views)('the header refresh in %s shows the spinner, keeps its label and focus, and ignores further presses', async (_name, setup, anchor) => {
            setup();
            render(<JobMatchPanel/>);
            await screen.findByTestId(anchor);
            await settled();
            const button = screen.getByTestId('job-match-refresh');
            expect(button.querySelector('[data-icon="refresh-cw"]')).not.toBeNull();
            expect(button).not.toHaveAttribute('aria-disabled');
            const {gate, release} = hold();
            const slow = ranked;
            ranked = () => gate.then(() => slow());
            button.focus();
            const before = fetchMock.mock.calls.length;
            fireEvent.click(button);
            await waitFor(() => expect(button.querySelector('[data-icon="spinner"]')).not.toBeNull());
            expect(button.querySelector('[data-icon="refresh-cw"]')).toBeNull();
            expect(button).toHaveAttribute('aria-disabled', 'true');
            expect(button).not.toBeDisabled();
            expect(button).toHaveAttribute('aria-label', 'Refresh match status');
            expect(button).toHaveFocus();
            const reads = fetchMock.mock.calls.length - before;
            expect(reads).toBe(3);
            fireEvent.click(button);
            fireEvent.click(button);
            fireEvent.keyDown(button, {key: 'Enter'});
            fireEvent.click(button);
            expect(fetchMock.mock.calls.length).toBe(before + 3);
            // Nothing in flight was aborted.
            for (const call of fetchMock.mock.calls.slice(before)) {
                expect(call[1].signal.aborted).toBe(false);
            }
            release();
            await settled();
            expect(button.querySelector('[data-icon="refresh-cw"]')).not.toBeNull();
            expect(button).not.toHaveAttribute('aria-disabled');
            expect(fetchMock.mock.calls.length).toBe(before + 3);
        });

        test('Check again and Try again do the same, and the retry action in the empty state too', async () => {
            healthy(3, {stale: true});
            await showResults();
            const check = screen.getByTestId('stale-refresh');
            const {gate, release} = hold();
            ranked = () => gate.then(() => jsonResponse(rankedPayload([job(3, {stale: true})])));
            check.focus();
            const before = fetchMock.mock.calls.length;
            fireEvent.click(check);
            await waitFor(() => expect(check).toHaveAttribute('aria-disabled', 'true'));
            expect(check.querySelector('[data-icon="spinner"]')).not.toBeNull();
            expect(check).toHaveTextContent('Check again');
            expect(check).toHaveFocus();
            fireEvent.click(check);
            expect(fetchMock.mock.calls.length).toBe(before + 3);
            release();
            await settled();
            expect(check.querySelector('[data-icon="refresh-cw"]')).not.toBeNull();
            cleanup();

            healthy(3);
            await showResults();
            ranked = () => jsonResponse({}, 503);
            pressRefresh();
            const retry = await screen.findByTestId('refresh-failed-retry');
            const second = hold();
            healthy(3);
            const fast = ranked;
            ranked = () => second.gate.then(() => fast());
            retry.focus();
            const mark = fetchMock.mock.calls.length;
            fireEvent.click(retry);
            await waitFor(() => expect(retry).toHaveAttribute('aria-disabled', 'true'));
            expect(retry.querySelector('[data-icon="spinner"]')).not.toBeNull();
            expect(retry).toHaveTextContent('Try again');
            expect(retry).toHaveFocus();
            fireEvent.click(retry);
            expect(fetchMock.mock.calls.length).toBe(mark + 3);
            second.release();
            await waitFor(() => expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument());
            cleanup();

            status = () => jsonResponse(statusPayload('no_matches', {actions: ['retry']}));
            ranked = () => jsonResponse(rankedPayload());
            matches = () => jsonResponse(matchPayload(0));
            render(<JobMatchPanel/>);
            const action = await screen.findByTestId('action-retry');
            await settled();
            const third = hold();
            status = () => third.gate.then(() => jsonResponse(statusPayload('no_matches', {actions: ['retry']})));
            const base = fetchMock.mock.calls.length;
            fireEvent.click(action);
            await waitFor(() => expect(action).toHaveAttribute('aria-disabled', 'true'));
            expect(action.querySelector('[data-icon="spinner"]')).not.toBeNull();
            fireEvent.click(action);
            expect(fetchMock.mock.calls.length).toBe(base + 3);
            third.release();
            await settled();
            expect(action.querySelector('[data-icon="refresh-cw"]')).not.toBeNull();
        });

        test('a priorities change still restarts a read in flight: the newest read wins', async () => {
            await showResults();
            const {gate, release} = hold();
            ranked = () => gate.then(() => jsonResponse(rankedPayload([job(3)])));
            const before = fetchMock.mock.calls.length;
            pressRefresh();
            await waitFor(() => expect(screen.getByTestId('job-match-refresh')).toHaveAttribute('aria-disabled', 'true'));
            act(() => { setPrioritiesRevision(getWorkspaceSnapshot().prioritiesRevision === 41 ? 42 : 41); });
            await waitFor(() => expect(fetchMock.mock.calls.length).toBe(before + 6));
            expect(fetchMock.mock.calls[before][1].signal.aborted).toBe(true);
            release();
            await settled();
        });

        test('a press during the re-check the evidence notice asks for sends nothing', async () => {
            const changed = {requirements: [{path: 'industry', status: 'unknown', evidence_status: {state: 'superseded', last_verified_at: null, source_domain: null}}]};
            ranked = () => jsonResponse(rankedPayload([{...job(3), ...changed}]));
            render(<JobMatchPanel/>);
            await screen.findByTestId('evidence-changed-notice');
            const {gate, release} = hold();
            const slow = ranked;
            ranked = () => gate.then(() => slow());
            const before = fetchMock.mock.calls.length;
            fireEvent.click(screen.getByTestId('job-match-refresh'));
            await waitFor(() => expect(screen.getByTestId('job-match-refresh')).toHaveAttribute('aria-disabled', 'true'));
            const calls = fetchMock.mock.calls.length;
            fireEvent.click(screen.getByTestId('job-match-refresh'));
            fireEvent.click(screen.getByTestId('evidence-changed-refresh'));
            expect(fetchMock.mock.calls.length).toBe(calls);
            expect(calls).toBeGreaterThan(before);
            release();
            await waitFor(() => expect(screen.getByTestId('job-match-refresh')).not.toHaveAttribute('aria-disabled', 'true'));
        });

        test('one visually hidden status line says a check started and how it ended, and a repeated failure is not silent', async () => {
            await showResults();
            const line = screen.getByTestId('read-announcement');
            expect(line).toHaveAttribute('aria-live', 'polite');
            expect(line).toHaveClass('visually-hidden');
            expect(line).toBeEmptyDOMElement();
            const {gate, release} = hold();
            ranked = () => gate.then(() => jsonResponse(rankedPayload([job(3)])));
            pressRefresh();
            await waitFor(() => expect(line).toHaveTextContent('Checking your job matches…'));
            release();
            await waitFor(() => expect(line).toHaveTextContent('Finished checking your job matches.'));
            expect(line).not.toHaveTextContent(/up to date/i);

            // The first failure is announced by the failed notice itself.
            ranked = () => jsonResponse({}, 503);
            pressRefresh();
            await screen.findByTestId('refresh-failed-notice');
            expect(line).toBeEmptyDOMElement();
            // A Try again that fails again must not be silent.
            fireEvent.click(screen.getByTestId('refresh-failed-retry'));
            await waitFor(() => expect(line).toHaveTextContent('Couldn’t refresh your job matches again.'));
            expect(screen.getAllByRole('status').filter((el) => el.classList.contains('alert'))).toHaveLength(1);
            // A first-load read says nothing.
            cleanup();
            healthy();
            render(<JobMatchPanel/>);
            expect(screen.getByTestId('read-announcement')).toBeEmptyDOMElement();
            await screen.findByTestId('ranked-job-42');
            expect(screen.getByTestId('read-announcement')).toBeEmptyDOMElement();
        });

        test('a stale read that is discarded still ends the check', async () => {
            await showResults();
            ranked = () => jsonResponse(rankedPayload([job(1)]));
            matches = () => jsonResponse(matchPayload(1, [job(1)]));
            pressRefresh();
            await waitFor(() => expect(screen.getByTestId('read-announcement')).toHaveTextContent('Finished checking'));
            expect(screen.getByTestId('job-match-refresh')).not.toHaveAttribute('aria-disabled');
        });
    });

    describe('the failed notice prints the time of what is on screen (F2)', () => {
        const GENERATED = '2026-10-11T20:53:00Z';
        const OLDER = '2026-10-08T14:53:00Z';
        const when = (iso: string) => new Date(iso).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'});

        test.each([
            ['with a result generation', 5],
            ['live (no result generation)', null],
        ])('over the results %s both lines show the listed rows’ instant, not the stored one', async (_label, generation) => {
            const rows = {generated_at: GENERATED, ...(generation === null ? {} : {result_generation: generation})};
            status = () => jsonResponse(statusPayload('ok'));
            matches = () => jsonResponse(matchPayload(1, [{...sampleJobMatch, revision: {generated_at: OLDER, result_generation: generation}}]));
            ranked = () => jsonResponse(rankedPayload([{...sampleJobMatch, revision: rows}]));
            render(<JobMatchPanel/>);
            await screen.findByTestId('ranked-job-42');
            ranked = () => jsonResponse({}, 503);
            pressRefresh();
            const notice = await screen.findByTestId('refresh-failed-notice');
            expect(notice).toHaveTextContent(`Couldn’t refresh. Showing results from ${when(GENERATED)}.`);
            expect(notice).not.toHaveTextContent(when(OLDER));
            expect(screen.getByTestId('results-timestamp')).toHaveTextContent(when(GENERATED));
        });

        test('in the count-only view the stored revision is the one both lines show', async () => {
            ranked = () => jsonResponse(rankedPayload());
            matches = () => jsonResponse(matchPayload(2, [{id: 1, revision: {generated_at: OLDER}}]));
            render(<JobMatchPanel/>);
            await screen.findByText(/job matches ready to review/);
            ranked = () => jsonResponse({}, 503);
            pressRefresh();
            const notice = await screen.findByTestId('refresh-failed-notice');
            expect(notice).toHaveTextContent(`Couldn’t refresh. This count is from ${when(OLDER)}.`);
            expect(notice).not.toHaveTextContent('Showing results');
            expect(screen.getByTestId('results-timestamp')).toHaveTextContent(when(OLDER));
        });

        test('over an empty state it says when it last checked and never says "Showing results"', async () => {
            status = () => jsonResponse(statusPayload('no_matches', {actions: ['retry']}));
            ranked = () => jsonResponse(rankedPayload());
            matches = () => jsonResponse(matchPayload(0));
            render(<JobMatchPanel/>);
            await screen.findByTestId('empty-state-no_matches');
            status = () => jsonResponse({}, 503);
            pressRefresh();
            const notice = await screen.findByTestId('refresh-failed-notice');
            expect(notice).toHaveTextContent(/^Couldn’t refresh\. Last checked .+\s*Try again$/);
            expect(notice).not.toHaveTextContent('Showing results');
            expect(within(notice).getByRole('button', {name: 'Try again'})).toBeInTheDocument();
            expect(within(notice).getByText('Couldn’t refresh.').tagName).toBe('STRONG');
        });
    });

    describe('one alert box at a time (F3)', () => {
        const inventory = {active_listings: 12, last_success_at: '2026-10-11T10:00:00Z', age_hours: 2};
        const partial = (failing: number) => ({coverage: {enabled_sources: 3, failing_sources: failing}, inventory});
        type Case = {name: string; status: Record<string, unknown>; state: string; row: Record<string, unknown>; failed: boolean; box: string; folded: string | null; text: RegExp};
        const cases: Case[] = [
            {name: 'failed + crawl', state: 'ok', status: {refreshing: true, inventory}, row: {}, failed: true,
                box: 'refresh-failed-notice', folded: null, text: /Couldn’t refresh/},
            {name: 'failed + partial coverage', state: 'partial_coverage', status: partial(1), row: {}, failed: true,
                box: 'refresh-failed-notice', folded: '1 of 3 job sources aren’t returning listings.', text: /Couldn’t refresh/},
            {name: 'failed + all sources failing', state: 'partial_coverage', status: partial(3), row: {}, failed: true,
                box: 'refresh-failed-notice', folded: 'No job source is returning listings right now.', text: /Couldn’t refresh/},
            {name: 'failed + stale', state: 'ok', status: {inventory}, row: {stale: true}, failed: true,
                box: 'refresh-failed-notice', folded: null, text: /Couldn’t refresh/},
            {name: 'stale (not pending) + partial coverage', state: 'partial_coverage', status: partial(1), row: {stale: true}, failed: false,
                box: 'coverage-notice', folded: 'Newer data or priorities exist.', text: /Coverage is limited: 1 of 3/},
            {name: 'stale (pending) + partial coverage', state: 'partial_coverage', status: partial(1), row: {stale: true, pending: true}, failed: false,
                box: 'coverage-notice', folded: 'Newer data or priorities exist.', text: /Coverage is limited: 1 of 3/},
            {name: 'crawl + partial coverage + stale', state: 'partial_coverage', status: {...partial(1), refreshing: true}, row: {stale: true}, failed: false,
                box: 'coverage-notice', folded: 'Newer data or priorities exist.', text: /Coverage is limited/},
            {name: 'all four', state: 'partial_coverage', status: {...partial(1), refreshing: true}, row: {stale: true}, failed: true,
                box: 'refresh-failed-notice', folded: '1 of 3 job sources aren’t returning listings.', text: /Couldn’t refresh/},
            {name: 'stale + crawl', state: 'ok', status: {refreshing: true, inventory}, row: {stale: true}, failed: false,
                box: 'stale-notice', folded: null, text: /^Newer data or priorities exist\./},
            {name: 'crawl alone', state: 'ok', status: {refreshing: true, inventory}, row: {}, failed: false,
                box: 'refresh-notice', folded: null, text: /^Job listings are being gathered right now\. These are your current results\.$/},
        ];
        test.each(cases)('$name: one alert, the highest-priority condition', async ({state, status: extra, row, failed, box, folded, text}) => {
            status = () => jsonResponse(statusPayload(state, extra));
            ranked = () => jsonResponse(rankedPayload([job(3, row)]));
            matches = () => jsonResponse(matchPayload(1, [job(3, row)]));
            await showResults();
            if (failed) {
                ranked = () => jsonResponse({}, 503);
                pressRefresh();
                await screen.findByTestId('refresh-failed-notice');
            }
            const boxes = Array.from(document.querySelectorAll('.alert[role="status"]'));
            expect(boxes).toHaveLength(1);
            expect(boxes[0]).toBe(screen.getByTestId(box));
            expect(boxes[0]).toHaveTextContent(text);
            const line = boxes[0].querySelector('[data-testid="notice-folded"]');
            if (folded) {
                expect(line).toHaveTextContent(folded);
                expect(line).toHaveClass('text-muted');
                expect(line!.querySelector('svg,button')).toBeNull();
            } else {
                expect(line).toBeNull();
            }
            // Nothing else renders as its own box.
            for (const other of ['refresh-failed-notice', 'coverage-notice', 'stale-notice', 'refresh-notice']) {
                if (other !== box) {
                    expect(screen.queryByTestId(other)).not.toBeInTheDocument();
                }
            }
        });

        test('the box carries its own control: Try again, Check again, or none', async () => {
            status = () => jsonResponse(statusPayload('partial_coverage', partial(1)));
            healthy(3, {stale: true});
            status = () => jsonResponse(statusPayload('partial_coverage', partial(1)));
            await showResults();
            expect(within(screen.getByTestId('coverage-notice')).queryByRole('button')).not.toBeInTheDocument();
            ranked = () => jsonResponse({}, 503);
            pressRefresh();
            const failed = await screen.findByTestId('refresh-failed-notice');
            expect(within(failed).getAllByRole('button')).toHaveLength(1);
        });

        test('the coverage notice uses the database icon, the failure alone the warning triangle', async () => {
            status = () => jsonResponse(statusPayload('partial_coverage', partial(1)));
            await showResults();
            expect(screen.getByTestId('coverage-notice').querySelector('[data-icon="database"]')).not.toBeNull();
            expect(screen.getByTestId('coverage-notice').querySelector('[data-icon="alert-triangle"]')).toBeNull();
            ranked = () => jsonResponse({}, 503);
            pressRefresh();
            const failed = await screen.findByTestId('refresh-failed-notice');
            expect(failed.querySelector('[data-icon="alert-triangle"]')).not.toBeNull();
            expect(failed.querySelector('strong')).toHaveTextContent('Couldn’t refresh.');
        });
    });

    describe('the inventory line sits with the timestamp (F6)', () => {
        const inventory = {active_listings: 12, last_success_at: '2026-10-11T10:00:00Z', age_hours: 2};

        test('it follows "Results generated at" directly, in the medium date style', async () => {
            status = () => jsonResponse(statusPayload('ok', {inventory}));
            await showResults();
            const stamp = screen.getByTestId('results-timestamp');
            const age = screen.getByTestId('inventory-age');
            expect(stamp.nextElementSibling).toBe(age);
            expect(stamp).toHaveClass('mb-1');
            expect(age).toHaveClass('mb-3');
            const date = new Date(inventory.last_success_at).toLocaleDateString(undefined, {dateStyle: 'medium'});
            expect(age).toHaveTextContent(`12 active listings checked · inventory last refreshed ${date}`);
        });

        test('the stored-count view puts the pair together too, and a stamp without inventory keeps its gap', async () => {
            status = () => jsonResponse(statusPayload('ok', {inventory}));
            ranked = () => jsonResponse(rankedPayload());
            matches = () => jsonResponse(matchPayload(2, [{id: 1, revision: {generated_at: STALE}}]));
            render(<JobMatchPanel/>);
            await screen.findByTestId('inventory-age');
            expect(screen.getByTestId('results-timestamp').nextElementSibling).toBe(screen.getByTestId('inventory-age'));
            cleanup();
            healthy();
            await showResults();
            expect(screen.getByTestId('results-timestamp')).toHaveClass('mb-3');
        });

        test('the coverage notice and the empty state print the same date style', async () => {
            const date = new Date(inventory.last_success_at).toLocaleDateString(undefined, {dateStyle: 'medium'});
            status = () => jsonResponse(statusPayload('partial_coverage', {coverage: {enabled_sources: 3, failing_sources: 1}, inventory}));
            await showResults();
            expect(screen.getByTestId('coverage-notice')).toHaveTextContent(`inventory last refreshed ${date}`);
            cleanup();
            status = () => jsonResponse(statusPayload('no_matches', {inventory}));
            ranked = () => jsonResponse(rankedPayload());
            matches = () => jsonResponse(matchPayload(0));
            render(<JobMatchPanel/>);
            expect(await screen.findByTestId('inventory-facts')).toHaveTextContent(`inventory last refreshed ${date}`);
        });
    });

    test('the crawl notice names the crawl and promises nothing (F4)', async () => {
        status = () => jsonResponse(statusPayload('ok', {refreshing: true}));
        await showResults();
        const notice = screen.getByTestId('refresh-notice');
        expect(notice).toHaveTextContent('Job listings are being gathered right now. These are your current results.');
        expect(notice).not.toHaveTextContent(/refresh|shortly|may appear/i);
    });

    describe('an ended session says so (F7)', () => {
        const ENDED = 'Your session has ended. Sign in again to see your job matches.';
        const VISITOR = 'Sign in to see personalized job matches based on your saved preferences.';

        test('a read answered by the login page says the session ended and names the button', async () => {
            await showResults();
            ranked = redirected;
            pressRefresh();
            expect(await screen.findByTestId('job-match-signed-out')).toHaveTextContent(ENDED);
            expect(screen.getByTestId('job-match-sign-in-cta')).toHaveTextContent('Sign in again');
        });

        test('a visitor who never signed in sees the existing copy, also after a failed first load of the login page', async () => {
            render(<JobMatchPanel isAuthenticated={false}/>);
            expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent(VISITOR);
            expect(screen.getByTestId('job-match-sign-in-cta')).toHaveTextContent('Sign in to save your search');
            cleanup();
            ranked = redirected;
            render(<JobMatchPanel/>);
            expect(await screen.findByTestId('job-match-signed-out')).toHaveTextContent(ENDED);
        });

        test('focus goes to the heading when the read started from a panel control', async () => {
            await showResults();
            screen.getByTestId('job-match-refresh').focus();
            ranked = redirected;
            pressRefresh();
            await screen.findByTestId('job-match-signed-out');
            await waitFor(() => expect(screen.getByRole('heading', {name: 'Your Job Matches'})).toHaveFocus());
        });

        test('focus is left alone when it was elsewhere on the page, and on the first load', async () => {
            const composer = document.createElement('textarea');
            document.body.appendChild(composer);
            await showResults();
            composer.focus();
            ranked = redirected;
            act(() => { setPrioritiesRevision(getWorkspaceSnapshot().prioritiesRevision === 7 ? 8 : 7); });
            await screen.findByTestId('job-match-signed-out');
            expect(composer).toHaveFocus();
            cleanup();
            composer.blur();
            render(<JobMatchPanel/>);
            await screen.findByTestId('job-match-signed-out');
            expect(document.body).toHaveFocus();
            composer.remove();
        });

        test('the ended wording is cleared when the panel is signed in again', async () => {
            const view = render(<JobMatchPanel/>);
            await screen.findByTestId('ranked-job-42');
            ranked = redirected;
            pressRefresh();
            await screen.findByTestId('job-match-signed-out');
            expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent(ENDED);
            healthy();
            act(() => { document.dispatchEvent(new CustomEvent('crank:private-state-purged')); });
            act(() => { document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true}})); });
            await screen.findByTestId('ranked-job-42');
            // Signed out again by the page: a plain sign-out, not an ended session.
            view.rerender(<JobMatchPanel isAuthenticated={false}/>);
            expect(await screen.findByTestId('job-match-signed-out')).toHaveTextContent(VISITOR);
            expect(screen.getByTestId('job-match-sign-in-cta')).toHaveTextContent('Sign in to save your search');
        });

        test('the ended wording is cleared when private state is purged and the hydration says signed out', async () => {
            await showResults();
            ranked = redirected;
            pressRefresh();
            await screen.findByTestId('job-match-signed-out');
            act(() => { document.dispatchEvent(new CustomEvent('crank:private-state-purged')); });
            expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent(VISITOR);
            act(() => { document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: false}})); });
            expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent(VISITOR);
        });

        const purge = () => act(() => { document.dispatchEvent(new CustomEvent('crank:private-state-purged')); });
        const hydrate = (detail: Record<string, unknown>) => act(() => {
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail}));
        });

        test('a control remembered before the session ended does not pull focus out of a field on a later load', async () => {
            healthy(3, {stale: true});
            await showResults();
            const check = screen.getByTestId('stale-refresh');
            check.focus();
            parts.ranked(redirected);
            fireEvent.click(check);
            await screen.findByTestId('job-match-signed-out');
            const field = document.createElement('input');
            document.body.appendChild(field);
            field.focus();
            // Signed in again in another tab: purge, hydration, first load.
            healthy();
            purge();
            hydrate({authenticated: true});
            await screen.findByTestId('ranked-job-42');
            await settled();
            expect(field).toHaveFocus();
            field.remove();
        });

        test('after an ended session an unobserved hydration retries the read and shows only what it brings', async () => {
            await showResults();
            parts.ranked(redirected);
            pressRefresh();
            expect(await screen.findByTestId('job-match-signed-out')).toHaveTextContent(ENDED);
            expect(screen.queryByText('Senior Engineer')).not.toBeInTheDocument();
            // The next account's results: nothing of the first one may remain.
            healthy(7);
            ranked = () => jsonResponse(rankedPayload([{...job(7), title: 'Staff Designer', listing_id: 77}]));
            const before = fetchMock.mock.calls.length;
            purge();
            expect(screen.queryByText('Senior Engineer')).not.toBeInTheDocument();
            hydrate({unobserved: true});
            await screen.findByTestId('ranked-job-77');
            expect(fetchMock.mock.calls.length).toBe(before + 3);
            expect(screen.queryByText('Senior Engineer')).not.toBeInTheDocument();
            expect(screen.queryByTestId('job-match-signed-out')).not.toBeInTheDocument();
        });

        test('after an ended session an unobserved hydration whose read finds the session still gone shows the ended card again', async () => {
            await showResults();
            parts.ranked(redirected);
            pressRefresh();
            await screen.findByTestId('job-match-signed-out');
            purge();
            hydrate({unobserved: true});
            await waitFor(() => expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent(ENDED));
            expect(screen.queryByText('Senior Engineer')).not.toBeInTheDocument();
        });

        test('a page rendered signed out sends nothing on an unobserved hydration', async () => {
            render(<JobMatchPanel isAuthenticated={false}/>);
            purge();
            hydrate({unobserved: true});
            await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
            expect(fetchMock).not.toHaveBeenCalled();
            expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent('Sign in to see personalized job matches');
        });

        test('after an observed sign-out a purge whose whoami fails sends no read and shows nothing of the earlier account', async () => {
            await showResults();
            purge();
            hydrate({authenticated: false});
            expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent(VISITOR);
            const before = fetchMock.mock.calls.length;
            purge();
            hydrate({unobserved: true});
            await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
            expect(fetchMock.mock.calls.length).toBe(before);
            expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent(VISITOR);
            expect(screen.queryByText('Senior Engineer')).not.toBeInTheDocument();
        });

        test('after the panel ended the session and a hydration observed the sign-out, a purge whose whoami fails sends no read', async () => {
            await showResults();
            parts.ranked(redirected);
            pressRefresh();
            expect(await screen.findByTestId('job-match-signed-out')).toHaveTextContent(ENDED);
            purge();
            hydrate({authenticated: false});
            expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent(VISITOR);
            const before = fetchMock.mock.calls.length;
            purge();
            hydrate({unobserved: true});
            await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
            expect(fetchMock.mock.calls.length).toBe(before);
            expect(screen.getByTestId('job-match-signed-out')).toHaveTextContent(VISITOR);
            expect(screen.queryByText('Senior Engineer')).not.toBeInTheDocument();
        });
    });

    describe('a redirect is an ended session only when it lands on the sign-in page', () => {
        const json = (body: unknown) => redirected('http://localhost/api/job-matches/elsewhere/', JSON.stringify(body), 'application/json');

        test('a redirect to the sign-in page ends the session whatever its query string', async () => {
            await showResults();
            parts.status(() => redirected('http://localhost/accounts/login/?next=%2Fsomewhere%2Felse%2F&x=1'));
            pressRefresh();
            expect(await screen.findByTestId('job-match-signed-out')).toHaveTextContent('Your session has ended');
            expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument();
        });

        test('the sign-in page is the one the card links to, resolved against the page', async () => {
            render(<JobMatchPanel signInUrl="/sign-in/?next=/chat/"/>);
            await screen.findByTestId('ranked-job-42');
            parts.matches(() => redirected('http://localhost/sign-in/'));
            pressRefresh();
            expect(await screen.findByTestId('job-match-sign-in-cta')).toHaveAttribute('href', '/sign-in/?next=/chat/');
            cleanup();

            healthy();
            render(<JobMatchPanel signInUrl="/sign-in/"/>);
            await screen.findByTestId('ranked-job-42');
            parts.matches(() => redirected('http://localhost/accounts/login/'));
            pressRefresh();
            expect(await screen.findByTestId('refresh-failed-notice')).toBeInTheDocument();
            expect(screen.queryByTestId('job-match-signed-out')).not.toBeInTheDocument();
        });

        test.each<[Part, () => Response]>([
            ['status', () => json(statusPayload('ok'))],
            ['matches', () => json(matchPayload(1, [job(3)]))],
            ['ranked', () => json(rankedPayload([job(3)]))],
        ])('a same-origin redirect that ends in valid JSON on the %s read keeps every result and shows no card', async (part, answer) => {
            await showResults();
            const card = screen.getByTestId('ranked-job-42');
            parts[part](answer);
            pressRefresh();
            await settled();
            expect(screen.queryByTestId('job-match-signed-out')).not.toBeInTheDocument();
            expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument();
            expect(screen.queryByTestId('job-match-error')).not.toBeInTheDocument();
            expect(screen.getByTestId('ranked-job-42')).toBe(card);
        });

        test('a redirect whose final address cannot be read is a failed refresh, never an ended session', async () => {
            await showResults();
            parts.ranked(() => redirected('http://'));
            pressRefresh();
            expect(await screen.findByTestId('refresh-failed-notice')).toBeInTheDocument();
            expect(screen.queryByTestId('job-match-signed-out')).not.toBeInTheDocument();
        });

        test('a same-origin redirect on a first load applies valid JSON', async () => {
            parts.ranked(() => json(rankedPayload([job(3)])));
            render(<JobMatchPanel/>);
            expect(await screen.findByTestId('ranked-job-42')).toBeInTheDocument();
            expect(screen.queryByTestId('job-match-signed-out')).not.toBeInTheDocument();
        });

        test.each<Part>(['status', 'matches', 'ranked'])('a redirect that ends in another HTML page on the %s read is a failed refresh with the list kept', async (part) => {
            await showResults();
            const card = screen.getByTestId('ranked-job-42');
            parts[part](() => redirected('http://localhost/maintenance/', '<html>back soon</html>'));
            pressRefresh();
            expect(await screen.findByTestId('refresh-failed-notice')).toHaveTextContent('Couldn’t refresh.');
            expect(screen.queryByTestId('job-match-signed-out')).not.toBeInTheDocument();
            expect(screen.getByTestId('ranked-job-42')).toBe(card);
        });
    });

    describe('a read that a priorities change replaces', () => {
        test('that then fails leaves the newer read running, the busy state set and no failed notice', async () => {
            await showResults();
            let rejectOlder!: (error: Error) => void;
            ranked = () => new Promise<Response>((_resolve, reject) => { rejectOlder = reject; });
            const before = fetchMock.mock.calls.length;
            pressRefresh();
            await waitFor(() => expect(fetchMock.mock.calls.length).toBe(before + 3));
            let releaseNewer!: () => void;
            const gate = new Promise<void>((resolve) => { releaseNewer = resolve; });
            ranked = () => gate.then(() => jsonResponse(rankedPayload([job(3)])));
            act(() => { setPrioritiesRevision(getWorkspaceSnapshot().prioritiesRevision === 51 ? 52 : 51); });
            await waitFor(() => expect(fetchMock.mock.calls.length).toBe(before + 6));
            await act(async () => {
                rejectOlder(new Error('late boom'));
                await new Promise((resolve) => setTimeout(resolve, 20));
            });
            expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument();
            expect(document.querySelector('[aria-busy="true"]')).not.toBeNull();
            expect(screen.getByTestId('job-match-refresh')).toHaveAttribute('aria-disabled', 'true');
            releaseNewer();
            await settled();
            expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument();
            expect(screen.getByTestId('job-match-refresh')).not.toHaveAttribute('aria-disabled');
        });
    });

    describe('the retry action in an empty state', () => {
        const emptyWithRetry = () => {
            status = () => jsonResponse(statusPayload('no_matches', {actions: ['retry']}));
            ranked = () => jsonResponse(rankedPayload());
            matches = () => jsonResponse(matchPayload(0));
        };

        test('moves focus to the heading when its read brings results and the button goes', async () => {
            emptyWithRetry();
            render(<JobMatchPanel/>);
            const action = await screen.findByTestId('action-retry');
            await settled();
            action.focus();
            healthy();
            fireEvent.click(action);
            await screen.findByTestId('ranked-job-42');
            await waitFor(() => expect(screen.getByRole('heading', {name: 'Your Job Matches'})).toHaveFocus());
            expect(screen.queryByTestId('action-retry')).not.toBeInTheDocument();
        });

        test('keeps focus on the button while the empty state stays', async () => {
            emptyWithRetry();
            render(<JobMatchPanel/>);
            const action = await screen.findByTestId('action-retry');
            await settled();
            action.focus();
            fireEvent.click(action);
            await waitFor(() => expect(action).toHaveAttribute('aria-disabled', 'true'));
            await settled();
            expect(screen.getByTestId('action-retry')).toBe(action);
            expect(action).toHaveFocus();
        });
    });

    describe('the criteria matching cannot use (item A of the visual review)', () => {
        const inventory = {active_listings: 12, last_success_at: '2026-10-11T10:00:00Z', age_hours: 2};
        const withUnsupported = (unsupported: string[]) => {
            status = () => jsonResponse(statusPayload('ok', {inventory}));
            ranked = () => jsonResponse(rankedPayload([{...job(3), unsupported}]));
            matches = () => jsonResponse(matchPayload(1, [{...job(3), unsupported}]));
        };

        test('one label is one muted line with no role and no live region', async () => {
            withUnsupported(['roles.titles']);
            await showResults();
            const line = screen.getByTestId('unsupported-notice');
            expect(line).toHaveTextContent('Not used in these matches: Job titles.');
            expect(line).toHaveClass('text-muted', 'small');
            expect(line).not.toHaveAttribute('role');
            expect(line).not.toHaveAttribute('aria-live');
            expect(line.querySelector('[role], [aria-live], svg, [data-icon]')).toBeNull();
            expect(line).not.toHaveClass('alert');
        });

        test('several labels are joined with commas in the order sent, and no field name reaches the page', async () => {
            withUnsupported(['roles.titles', 'compensation.minimum_total_compensation', 'notes']);
            await showResults();
            const line = screen.getByTestId('unsupported-notice');
            expect(line).toHaveTextContent('Not used in these matches: Job titles, Minimum total compensation, Notes.');
            expect(line.textContent).not.toMatch(/roles\.|minimum_total_compensation/);
        });

        test('it sits under the timestamp and the inventory line, which keep a 4 px gap, and the group ends with the usual margin', async () => {
            withUnsupported(['roles.titles']);
            await showResults();
            const stamp = screen.getByTestId('results-timestamp');
            const age = screen.getByTestId('inventory-age');
            const line = screen.getByTestId('unsupported-notice');
            expect(stamp.nextElementSibling).toBe(age);
            expect(age.nextElementSibling).toBe(line);
            expect(stamp).toHaveClass('mb-1');
            expect(age).toHaveClass('mb-1');
            expect(line).toHaveClass('mb-3');
        });

        test('without an inventory line it follows the timestamp, and without a timestamp it stands alone', async () => {
            status = () => jsonResponse(statusPayload('partial_coverage', {coverage: {enabled_sources: 3, failing_sources: 1}, inventory}));
            ranked = () => jsonResponse(rankedPayload([{...job(3), unsupported: ['roles.titles']}]));
            await showResults();
            expect(screen.queryByTestId('inventory-age')).not.toBeInTheDocument();
            const stamp = screen.getByTestId('results-timestamp');
            expect(stamp.nextElementSibling).toBe(screen.getByTestId('unsupported-notice'));
            expect(stamp).toHaveClass('mb-1');
            cleanup();

            status = () => jsonResponse(statusPayload('ok'));
            ranked = () => jsonResponse(rankedPayload([{...sampleJobMatch, unsupported: ['roles.titles']}]));
            await showResults();
            expect(screen.queryByTestId('results-timestamp')).not.toBeInTheDocument();
            expect(screen.getByTestId('unsupported-notice')).toHaveClass('mb-3');
        });

        test('a notice box without a button is drawn with the first-line icon variant, one with a button is not', async () => {
            const banner = (testid: string) => screen.getByTestId(testid).querySelector('.job-match-stale-banner')!;
            status = () => jsonResponse(statusPayload('partial_coverage', {coverage: {enabled_sources: 3, failing_sources: 1}, inventory}));
            await showResults();
            expect(banner('coverage-notice')).toHaveClass('job-match-stale-banner--plain');
            cleanup();
            status = () => jsonResponse(statusPayload('ok', {refreshing: true}));
            await showResults();
            expect(banner('refresh-notice')).toHaveClass('job-match-stale-banner--plain');
            cleanup();
            healthy(3, {stale: true, pending: true});
            await showResults();
            expect(banner('stale-notice')).toHaveClass('job-match-stale-banner--plain');
            cleanup();
            healthy(3, {stale: true});
            await showResults();
            expect(banner('stale-notice')).not.toHaveClass('job-match-stale-banner--plain');
            ranked = () => jsonResponse({}, 503);
            pressRefresh();
            await screen.findByTestId('refresh-failed-notice');
            expect(banner('refresh-failed-notice')).not.toHaveClass('job-match-stale-banner--plain');
        });

        test('it does not count as an alert box', async () => {
            withUnsupported(['roles.titles']);
            healthy(3, {stale: true});
            ranked = () => jsonResponse(rankedPayload([{...job(3, {stale: true}), unsupported: ['roles.titles']}]));
            await showResults();
            expect(screen.getAllByRole('status').filter((el) => el.classList.contains('alert'))).toHaveLength(1);
            expect(screen.getByTestId('unsupported-notice')).toBeInTheDocument();
        });
    });
});
