// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, waitFor} from '@testing-library/react';
import * as React from 'react';

import JobMatchPanel from './JobMatchPanel';

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
}> = {}) {
    return {
        state,
        title: overrides.title ?? 'Test title',
        message: overrides.message ?? 'Test message',
        actions: overrides.actions ?? [],
        ...(overrides.staff_detail ? {staff_detail: overrides.staff_detail} : {}),
    };
}

function matchPayload(count: number) {
    return {count, next: null, previous: null, results: []};
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
    score: 72.3,
    reasons: ['Public company', 'Remote', 'Score 4.2'],
};

describe('JobMatchPanel', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    // Helper: mock all three API calls and wait for ready phase
    async function renderPanel(
        statusState: string = 'ok',
        opts: { count?: number; staffDetail?: string; statusOverrides?: Record<string, unknown>; rankedJobs?: any[]; rankedOrgs?: any[]; rankedStatus?: number } = {},
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
            if (url.includes('/api/job-matches/')) {
                return Promise.resolve(jsonResponse(matchPayload(count)));
            }
            return Promise.resolve(jsonResponse({}));
        });
        render(<JobMatchPanel/>);
        await waitFor(() => expect(screen.getByTestId('job-match-panel')).not.toHaveTextContent('Loading'));
    }

    describe('loading state', () => {
        test('shows a loading indicator while fetching', () => {
            (global.fetch as jest.Mock).mockReturnValue(new Promise(() => {}));
            render(<JobMatchPanel/>);
            expect(screen.getByTestId('job-match-loading')).toBeInTheDocument();
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
            expect(error).toHaveTextContent(/status 500/i);
        });

        test('shows an error when fetch throws', async () => {
            (global.fetch as jest.Mock).mockRejectedValue(new Error('Network down'));
            render(<JobMatchPanel/>);
            const error = await screen.findByTestId('job-match-error');
            expect(error).toHaveTextContent('Network down');
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
            fireEvent.click(screen.getByLabelText('Retry loading match status'));
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

        test('shows score badge for ranked job match', async () => {
            await renderPanel('ok', { count: 1, rankedJobs: [sampleJobMatch] });
            const scoreBadge = screen.getByTestId('job-score-42');
            expect(scoreBadge).toHaveTextContent('85.5');
        });

        test('shows score badge for ranked org match', async () => {
            await renderPanel('ok', { count: 1, rankedOrgs: [sampleOrgMatch] });
            const scoreBadge = screen.getByTestId('org-score-7');
            expect(scoreBadge).toHaveTextContent('72.3');
        });

        test('shows remote badge for remote job listing', async () => {
            await renderPanel('ok', { count: 1, rankedJobs: [sampleJobMatch] });
            const jobEl = screen.getByTestId('ranked-job-42');
            expect(jobEl).toHaveTextContent('Remote');
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
            const scoreBadge = await screen.findByTestId('job-score-42');
            expect(scoreBadge).toHaveTextContent('85.5');
            jobScore = 92.0;
            fireEvent.click(screen.getByTestId('job-match-refresh'));
            await waitFor(() => expect(screen.getByTestId('job-score-42')).toHaveTextContent('92.0'));
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
    });

    describe('empty states', () => {
        test('no_source: shows title, message, and recovery actions', async () => {
            await renderPanel('no_source', {
                statusOverrides: {
                    title: 'No job sources configured',
                    message: "CRank hasn't been connected to any job sources yet.",
                    actions: ['suggest_company', 'help'],
                },
            });
            const el = screen.getByTestId('empty-state-no_source');
            expect(el).toHaveTextContent('No job sources configured');
            expect(el).toHaveTextContent("hasn't been connected");
            expect(screen.getByTestId('action-suggest_company')).toBeInTheDocument();
            expect(screen.getByTestId('action-help')).toBeInTheDocument();
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

        test('chat action focuses the message input', async () => {
            const input = document.createElement('input');
            input.setAttribute('aria-label', 'Message');
            input.scrollIntoView = jest.fn();
            document.body.appendChild(input);
            const focusSpy = jest.spyOn(input, 'focus');
            await renderPanel('no_preferences', {
                statusOverrides: {
                    title: 'Tell us',
                    message: 'Test',
                    actions: ['chat'],
                },
            });
            fireEvent.click(screen.getByTestId('action-chat'));
            expect(focusSpy).toHaveBeenCalled();
            document.body.removeChild(input);
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

        test('complete_profile action focuses message input and scrolls', async () => {
            const input = document.createElement('input');
            input.setAttribute('aria-label', 'Message');
            input.scrollIntoView = jest.fn();
            document.body.appendChild(input);
            const focusSpy = jest.spyOn(input, 'focus');
            await renderPanel('no_preferences', {
                statusOverrides: {
                    title: 'Complete profile',
                    message: 'Tell us your preferences',
                    actions: ['complete_profile'],
                },
            });
            fireEvent.click(screen.getByTestId('action-complete_profile'));
            expect(focusSpy).toHaveBeenCalled();
            expect(input.scrollIntoView).toHaveBeenCalledWith({behavior: 'smooth', block: 'center'});
            document.body.removeChild(input);
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
        test('renders panel into #job-match-panel container on DOMContentLoaded', () => {
            const container = document.createElement('div');
            container.id = 'job-match-panel';
            document.body.appendChild(container);

            const mockRender = jest.fn();
            const mockCreateRoot = jest.fn(() => ({render: mockRender}));
            jest.doMock('react-dom/client', () => ({createRoot: mockCreateRoot}));

            jest.isolateModules(() => {
                require('./JobMatchPanel');
            });

            // The module-level DOMContentLoaded listener should have been registered.
            // Fire it to trigger the bootstrap.
            document.dispatchEvent(new Event('DOMContentLoaded'));

            expect(mockCreateRoot).toHaveBeenCalledWith(container);
            expect(mockRender).toHaveBeenCalled();

            document.body.removeChild(container);
            jest.dontMock('react-dom/client');
        });
    });
});
