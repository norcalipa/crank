// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, waitFor} from '@testing-library/react';
import * as React from 'react';

import JobMatchPanel from './JobMatchPanel';
import {closeAssistant, openAssistant, resetWorkspaceForTests} from './workspace/store';

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

// Helper: mock all three API calls and wait for ready phase.
// Hoisted to module scope so every describe block in this file can reuse it.
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
