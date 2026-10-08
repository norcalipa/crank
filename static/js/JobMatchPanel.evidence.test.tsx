// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import * as React from 'react';

import JobMatchPanel from './JobMatchPanel';
import {EVIDENCE_CHANGED_MARKER, EVIDENCE_STATUS_META, MATCH_TERMS, REQUIREMENT_MARKS} from './labels';
import {closeAssistant, resetWorkspaceForTests} from './workspace/store';

// Requirement chips carry the read-time status of the fact behind each
// outcome (issue #473): a stale- or prose-backed match is never the bare
// verified style, and replaced evidence asks for a refresh.

const json = (payload: unknown): Response => new Response(JSON.stringify(payload), {
    status: 200, headers: {'Content-Type': 'application/json'},
});

const status = (state: string, lastVerifiedAt: string | null = null, sourceDomain: string | null = null) =>
    ({state, last_verified_at: lastVerifiedAt, source_domain: sourceDomain});

const requirement = (path: string, outcome: string, evidence?: unknown) => ({
    path, status: outcome, source_kind: 'evidence', source_id: 9,
    ...(evidence === undefined ? {} : {evidence_status: evidence}),
});

const job = (requirements: unknown[]) => ({
    listing_id: 42, title: 'Senior Engineer', employer_name: 'Acme Corp', organization_id: 7,
    organization_name: 'Acme Corp', canonical_url: 'https://jobs.example.test/42', location_text: 'Remote',
    is_remote: true, score: 85.5, fit_score: 85.5, company_score: 4.2, coverage: 0.75,
    reasons: ['Remote'], requirements, unsupported: [], evidence_ids: [9],
    revision: {stale: false, generated_at: '2026-09-22T08:00:00Z'},
});

const org = (overrides: Record<string, unknown> = {}) => ({
    organization_id: 7, name: 'Acme Corp', url: '', funding_round: 'X', rto_policy: 'O', score: 70,
    fit_score: 70, company_score: 4.2, coverage: 1, reasons: [], requirements: [], unsupported: [],
    evidence_ids: [], revision: {stale: false}, ...overrides,
});

async function renderRanked(jobs: unknown[], orgs: unknown[] = []) {
    const fetchMock = jest.fn((url: string) => {
        if (url.includes('/api/job-matches/status/')) {
            return Promise.resolve(json({state: 'ok', title: 'Matches', message: '', actions: []}));
        }
        if (url.includes('/api/job-matches/ranked/')) {
            return Promise.resolve(json({job_matches: jobs, organization_matches: orgs}));
        }
        return Promise.resolve(json({count: jobs.length, next: null, previous: null, results: []}));
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    render(<JobMatchPanel/>);
    await waitFor(() => expect(screen.getByTestId('job-match-panel')).not.toHaveTextContent('Loading'));
    await screen.findByTestId(jobs.length ? 'ranked-job-matches' : 'ranked-org-matches');
    return fetchMock;
}

const chip = (path: string) => screen.getByTestId(`requirement-${path}`);

describe('JobMatchPanel evidence qualifiers (issue #473)', () => {
    afterEach(() => {
        jest.restoreAllMocks();
        resetWorkspaceForTests();
        closeAssistant();
    });

    test('a verified fact qualifies the chip through the shared badge', async () => {
        await renderRanked([job([requirement('work_location.modes', 'match', status('verified', '2026-09-01T00:00:00Z', 'acme.example'))])]);
        const el = chip('work_location.modes');
        expect(el).toHaveClass('job-match-chip--match');
        expect(el).not.toHaveClass('job-match-chip--unverified');
        expect(el).toHaveAttribute('data-evidence-state', 'verified');
        expect(el).toHaveTextContent('✓ Work mode · ✓ Verified');
        expect(el.querySelector('.evidence-badge-verified')).toBeInTheDocument();
        expect(el).toHaveAttribute('aria-label', 'Work mode: match, Verified');
        expect(screen.queryByTestId('evidence-changed-notice')).not.toBeInTheDocument();
    });

    test('a stale-backed match is stale-qualified with its date, never the bare verified style', async () => {
        await renderRanked([job([
            requirement('work_location.modes', 'match', status('stale', '2024-08-08T00:00:00Z', 'acme.example')),
            requirement('funding_stage', 'mismatch', status('stale')),
            requirement('work_location.max_in_office_days', 'unknown', status('stale', '2024-08-08T00:00:00Z')),
        ])]);
        const el = chip('work_location.modes');
        expect(el).toHaveTextContent('✓ Work mode · ! Stale (last verified Aug 8, 2024)');
        expect(el).toHaveClass('job-match-chip--match', 'job-match-chip--unverified');
        expect(el).toHaveAttribute('data-evidence-state', 'stale');
        expect(el.querySelector('.evidence-badge-stale')).toBeInTheDocument();
        expect(el.querySelector('.evidence-badge-verified')).toBeNull();
        expect(el).toHaveAttribute('aria-label', 'Work mode: match, Stale, last verified August 8, 2024');
        // An accepted fact that was never re-verified says so.
        expect(chip('funding_stage')).toHaveTextContent('✗ Funding stage · ! Stale (never verified)');
        expect(chip('funding_stage')).toHaveAttribute('aria-label', 'Funding stage: mismatch, Stale, never verified');
        // Staleness is shown even when the outcome itself is unknown.
        expect(chip('work_location.max_in_office_days')).toHaveTextContent('? In-office days · ! Stale');
    });

    test('profile data and unconfirmed prose are labelled and lose the solid style', async () => {
        await renderRanked([job([
            requirement('work_location.modes', 'match', status('profile')),
            requirement('compensation.require_public_company', 'match', status('sourced', '2026-09-01T00:00:00Z')),
        ])]);
        const profile = chip('work_location.modes');
        expect(profile).toHaveTextContent('✓ Work mode · • Profile data');
        expect(profile).toHaveClass('job-match-chip--unverified');
        expect(profile).toHaveAttribute('aria-label', 'Work mode: match, Profile data');
        const sourced = chip('compensation.require_public_company');
        expect(sourced).toHaveTextContent('✓ Public company · Sourced, not confirmed');
        expect(sourced).toHaveClass('job-match-chip--unverified');
        expect(sourced.querySelector('.evidence-badge')).toBeNull();
        expect(sourced).toHaveAttribute('aria-label', 'Public company: match, Sourced, not confirmed');
    });

    test('an undecided requirement gets no verified, profile or sourced qualifier', async () => {
        await renderRanked([job([
            requirement('work_location.modes', 'unknown', status('verified', '2026-09-01T00:00:00Z')),
            requirement('funding_stage', 'unknown', status('sourced')),
            requirement('industry', 'unknown', status('profile')),
        ])]);
        for (const path of ['work_location.modes', 'funding_stage', 'industry']) {
            expect(chip(path)).toHaveClass('job-match-chip--unknown');
            expect(chip(path)).not.toHaveClass('job-match-chip--unverified');
            expect(chip(path).querySelector('.job-match-chip-qualifier')).toBeNull();
        }
        expect(chip('work_location.modes')).toHaveAttribute('aria-label', 'Work mode: unknown');
    });

    test('listing data and payloads without a status keep the plain chip', async () => {
        await renderRanked([job([
            requirement('compensation.minimum_salary', 'match', null),
            requirement('industry', 'mismatch'),
        ])]);
        expect(chip('compensation.minimum_salary')).toHaveAttribute('aria-label', 'Minimum salary: match');
        expect(chip('compensation.minimum_salary')).not.toHaveAttribute('data-evidence-state');
        expect(chip('compensation.minimum_salary').querySelector('.job-match-chip-qualifier')).toBeNull();
        expect(chip('industry')).toHaveClass('job-match-chip--mismatch');
        expect(chip('industry')).not.toHaveClass('job-match-chip--unverified');
    });

    test('superseded or deleted evidence reads "Evidence changed — refresh" and offers one refresh', async () => {
        const fetchMock = await renderRanked([job([
            requirement('work_location.modes', 'match', status('superseded')),
            requirement('funding_stage', 'mismatch', status('missing')),
        ])], [org({requirements: [requirement('work_location.modes', 'match', status('superseded'))]})]);
        const jobCard = screen.getByTestId('ranked-job-42');
        const changed = within(jobCard).getByTestId('requirement-work_location.modes');
        expect(changed).toHaveTextContent('↻ Work mode · Evidence changed — refresh');
        expect(changed).toHaveClass('job-match-chip--changed');
        // Neither outcome style survives: the stored result no longer has its fact.
        expect(changed).not.toHaveClass('job-match-chip--match');
        expect(changed).toHaveAttribute('data-evidence-state', 'superseded');
        expect(changed).toHaveAttribute('aria-label', 'Work mode: evidence changed, refresh matches');
        const missing = within(jobCard).getByTestId('requirement-funding_stage');
        expect(missing).toHaveAttribute('data-evidence-state', 'missing');
        expect(missing).not.toHaveClass('job-match-chip--mismatch');
        expect(within(screen.getByTestId('ranked-org-7')).getByTestId('requirement-work_location.modes'))
            .toHaveClass('job-match-chip--changed');

        // One notice for the whole result set, with a labelled refresh button.
        const notices = screen.getAllByTestId('evidence-changed-notice');
        expect(notices).toHaveLength(1);
        expect(notices[0]).toHaveAttribute('role', 'status');
        expect(notices[0]).toHaveTextContent('Refresh to re-check them.');
        expect(changed).toHaveTextContent(MATCH_TERMS.changed.label);
        expect(changed.querySelector('.job-match-chip-qualifier')).toHaveAttribute('title', MATCH_TERMS.changed.meaning);
        expect(changed.querySelector('.job-match-chip-sep')).toHaveAttribute('aria-hidden', 'true');
        expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/refresh/'))).toBe(false);
    });

    const changedJob = () => job([requirement('work_location.modes', 'match', status('superseded'))]);
    const settledJob = () => job([requirement('work_location.modes', 'match', status('verified', '2026-09-01T00:00:00Z'))]);

    /** Serves `first` until the refresh POST has been answered, then `second`. */
    function mockRefresh(first: unknown, second: unknown, refresh: () => Promise<Response>) {
        let refreshed = false;
        const calls: {url: string; method: string}[] = [];
        global.fetch = jest.fn((url: string, init: RequestInit = {}) => {
            calls.push({url, method: init.method || 'GET'});
            if (url.includes('/api/job-matches/refresh/')) {
                return refresh().finally(() => { refreshed = true; });
            }
            if (url.includes('/api/job-matches/status/')) {
                return Promise.resolve(json({state: 'ok', title: 'Matches', message: '', actions: []}));
            }
            if (url.includes('/api/job-matches/ranked/')) {
                return Promise.resolve(json({job_matches: [refreshed ? second : first], organization_matches: []}));
            }
            return Promise.resolve(json({count: 1, next: null, previous: null, results: []}));
        }) as unknown as typeof fetch;
        return calls;
    }

    const notice = () => screen.getByTestId('evidence-changed-notice');
    const refreshButton = () => within(notice()).getByRole('button');
    const posts = (calls: {url: string; method: string}[]) => calls.filter((call) => call.method === 'POST');
    const rankedReads = (calls: {url: string; method: string}[]) => calls.filter((call) => call.url.includes('/ranked/'));
    const heading = () => screen.getByRole('heading', {name: 'Your Job Matches'});

    test('a refresh that published clears the notice, re-reads once, says so and moves focus to the heading', async () => {
        document.cookie = 'csrftoken=tok473';
        const calls = mockRefresh(changedJob(), settledJob(), () => Promise.resolve(json({status: 'published'})));
        render(<JobMatchPanel/>);
        const button = within(await screen.findByTestId('evidence-changed-notice')).getByRole('button', {name: 'Refresh matches'});
        expect(heading()).toHaveAttribute('aria-describedby', 'job-match-notice');
        expect(notice()).toHaveAttribute('id', 'job-match-notice');
        button.focus();
        const before = calls.length;
        fireEvent.click(button);

        await waitFor(() => expect(screen.queryByTestId('evidence-changed-notice')).not.toBeInTheDocument());
        await waitFor(() => expect(chip('work_location.modes')).toHaveTextContent('✓ Work mode · ✓ Verified'));
        const after = calls.slice(before);
        // The recompute is requested first; the three reads follow it.
        expect(after[0]).toEqual({url: '/api/job-matches/refresh/', method: 'POST'});
        expect(after.slice(1).every((call) => call.method === 'GET')).toBe(true);
        expect(rankedReads(after)).toHaveLength(1);
        const post = (global.fetch as jest.Mock).mock.calls.find(([url]) => String(url).includes('/refresh/'));
        expect(post[1].headers['X-CSRFToken']).toBe('tok473');
        // The button went with the notice: focus goes to the heading, not <body>.
        await waitFor(() => expect(heading()).toHaveFocus());
        expect(heading()).toHaveAttribute('tabindex', '-1');
        expect(heading()).not.toHaveAttribute('aria-describedby');
        // Success is announced; nothing visible would say it otherwise.
        const announcement = screen.getByTestId('recheck-announcement');
        expect(announcement).toHaveAttribute('aria-atomic', 'true');
        expect(announcement).toHaveAttribute('aria-live', 'polite');
        expect(announcement).toHaveTextContent('Matches re-checked.');
    });

    test('while a refresh runs the list stays, the button is busy, and a second press sends nothing', async () => {
        let answer: (response: Response) => void = () => undefined;
        const calls = mockRefresh(changedJob(), settledJob(),
            () => new Promise<Response>((resolve) => { answer = resolve; }));
        render(<JobMatchPanel/>);
        const button = within(await screen.findByTestId('evidence-changed-notice')).getByRole('button', {name: 'Refresh matches'});
        button.focus();
        fireEvent.click(button);

        await waitFor(() => expect(notice()).toHaveAttribute('data-recheck', 'running'));
        expect(notice()).toHaveAttribute('aria-busy', 'true');
        expect(notice()).toHaveAttribute('aria-live', 'polite');
        expect(notice()).toHaveTextContent('Re-checking your matches…');
        // Same button, still focused, marked unavailable rather than removed.
        expect(refreshButton()).toBe(button);
        expect(button).toHaveAttribute('aria-disabled', 'true');
        expect(button).not.toBeDisabled();
        expect(button).toHaveTextContent('Re-checking…');
        expect(button).toHaveFocus();
        expect(screen.getByTestId('job-match-refresh')).toHaveAttribute('aria-disabled', 'true');
        // No skeleton: the results are still on screen.
        expect(screen.queryByTestId('job-match-loading')).not.toBeInTheDocument();
        expect(screen.getByTestId('ranked-job-42')).toBeInTheDocument();
        // Double click, and the header control, while it runs: still one POST.
        fireEvent.click(button);
        fireEvent.click(button);
        fireEvent.click(screen.getByTestId('job-match-refresh'));
        expect(posts(calls)).toHaveLength(1);

        answer(json({status: 'current'}));
        await waitFor(() => expect(screen.queryByTestId('evidence-changed-notice')).not.toBeInTheDocument());
        expect(posts(calls)).toHaveLength(1);
        expect(screen.getByTestId('job-match-refresh')).not.toHaveAttribute('aria-disabled');
        expect(screen.getByTestId('recheck-announcement')).toHaveTextContent('Matches re-checked.');
    });

    test('nothing to refresh re-reads and says the matches were already up to date', async () => {
        const calls = mockRefresh(changedJob(), settledJob(), () => Promise.resolve(json({status: 'not_needed'})));
        render(<JobMatchPanel/>);
        fireEvent.click(within(await screen.findByTestId('evidence-changed-notice')).getByRole('button'));
        await waitFor(() => expect(screen.queryByTestId('evidence-changed-notice')).not.toBeInTheDocument());
        expect(rankedReads(calls)).toHaveLength(2);
        expect(screen.getByTestId('recheck-announcement')).toHaveTextContent('Your matches were already up to date.');
        await waitFor(() => expect(heading()).toHaveFocus());
    });

    test('paused by the operator says so, removes the button and does not invite a retry', async () => {
        const calls = mockRefresh(changedJob(), changedJob(), () => Promise.resolve(json({status: 'disabled'})));
        render(<JobMatchPanel/>);
        const button = within(await screen.findByTestId('evidence-changed-notice')).getByRole('button', {name: 'Refresh matches'});
        button.focus();
        fireEvent.click(button);

        await waitFor(() => expect(notice()).toHaveAttribute('data-recheck', 'paused'));
        expect(notice()).toHaveTextContent(
            'Re-checking is paused right now, so results marked “Evidence changed” may be out of date.');
        expect(notice()).not.toHaveTextContent(/try again|Refresh to re-check/i);
        expect(within(notice()).queryByRole('button')).not.toBeInTheDocument();
        expect(screen.getAllByTestId('evidence-changed-notice')).toHaveLength(1);
        // Nothing changed on the server, so nothing is re-read.
        expect(rankedReads(calls)).toHaveLength(1);
        // The button is gone; focus lands on the heading, which is described by the notice.
        await waitFor(() => expect(heading()).toHaveFocus());
        expect(heading()).toHaveAttribute('aria-describedby', 'job-match-notice');
        expect(screen.getByTestId('recheck-announcement')).toBeEmptyDOMElement();

        // The header refresh asks again without a notice button flashing back,
        // and keeps its own focus.
        const header = screen.getByTestId('job-match-refresh');
        header.focus();
        fireEvent.click(header);
        expect(within(notice()).queryByRole('button')).not.toBeInTheDocument();
        await waitFor(() => expect(posts(calls)).toHaveLength(2));
        await waitFor(() => expect(notice()).toHaveAttribute('data-recheck', 'paused'));
        expect(header).toHaveFocus();
    });

    test('a refresh asked for too soon says when to try again and keeps the button and its focus', async () => {
        const limited = (body: unknown, headers: Record<string, string> = {}) => () => Promise.resolve(
            new Response(JSON.stringify(body), {status: 429, headers}));
        const calls = mockRefresh(changedJob(), changedJob(), limited({status: 'rate_limited', retry_after: 30}));
        render(<JobMatchPanel/>);
        const button = within(await screen.findByTestId('evidence-changed-notice')).getByRole('button', {name: 'Refresh matches'});
        button.focus();
        fireEvent.click(button);
        await waitFor(() => expect(notice()).toHaveAttribute('data-recheck', 'limited'));
        expect(notice()).toHaveTextContent('A re-check was just requested. Try again in about 30 seconds.');
        expect(refreshButton()).toHaveTextContent('Refresh matches');
        expect(refreshButton()).not.toHaveAttribute('aria-disabled');
        expect(refreshButton()).toHaveFocus();
        expect(rankedReads(calls)).toHaveLength(1);
    });

    test.each([
        ['the Retry-After header', '', {'Retry-After': '12'}, 'Try again in about 12 seconds.'],
        ['no usable delay', 'not json', {}, 'Try again shortly.'],
    ])('a 429 with %s still reads as asked too soon', async (_name, body, headers, sentence) => {
        mockRefresh(changedJob(), changedJob(),
            () => Promise.resolve(new Response(body, {status: 429, headers: headers as Record<string, string>})));
        render(<JobMatchPanel/>);
        fireEvent.click(within(await screen.findByTestId('evidence-changed-notice')).getByRole('button'));
        await waitFor(() => expect(notice()).toHaveTextContent(`A re-check was just requested. ${sentence}`));
    });

    test.each([
        ['the request cannot be sent', () => Promise.reject(new Error('offline'))],
        ['the server errors', () => Promise.resolve(new Response('<h1>Server Error</h1>', {status: 500}))],
        ['the recompute reports failure', () => Promise.resolve(json({status: 'failed'}))],
        ['the answer is unreadable', () => Promise.resolve(new Response('', {status: 200}))],
    ])('when %s the notice offers Try again, keeps the list and focus, and a retry can succeed', async (_name, refresh) => {
        let attempts = 0;
        const calls = mockRefresh(changedJob(), settledJob(), () => {
            attempts += 1;
            return attempts === 1 ? (refresh as () => Promise<Response>)() : Promise.resolve(json({status: 'published'}));
        });
        // mockRefresh serves the settled list after any answered POST; the
        // failed attempt must not have re-read at all.
        render(<JobMatchPanel/>);
        const button = within(await screen.findByTestId('evidence-changed-notice')).getByRole('button', {name: 'Refresh matches'});
        button.focus();
        fireEvent.click(button);

        await waitFor(() => expect(notice()).toHaveAttribute('data-recheck', 'failed'));
        expect(notice()).toHaveTextContent('The re-check did not finish. Try again in a moment.');
        expect(screen.getAllByTestId('evidence-changed-notice')).toHaveLength(1);
        expect(rankedReads(calls)).toHaveLength(1);
        expect(screen.getByTestId('ranked-job-42')).toBeInTheDocument();
        expect(screen.queryByTestId('job-match-error')).not.toBeInTheDocument();
        const retry = within(notice()).getByRole('button', {name: 'Try again'});
        expect(retry).toHaveFocus();

        fireEvent.click(retry);
        await waitFor(() => expect(screen.queryByTestId('evidence-changed-notice')).not.toBeInTheDocument());
        expect(posts(calls)).toHaveLength(2);
    });

    test('an answer that is neither success nor failure just shows the state as it now is', async () => {
        const calls = mockRefresh(changedJob(), changedJob(), () => Promise.resolve(json({status: 'discarded_stale'})));
        render(<JobMatchPanel/>);
        const button = within(await screen.findByTestId('evidence-changed-notice')).getByRole('button');
        button.focus();
        fireEvent.click(button);
        await waitFor(() => expect(rankedReads(calls)).toHaveLength(2));
        await waitFor(() => expect(notice()).toHaveAttribute('data-recheck', 'idle'));
        expect(notice()).toHaveTextContent('Refresh to re-check them.');
        expect(screen.getByTestId('recheck-announcement')).toBeEmptyDOMElement();
        expect(refreshButton()).toHaveFocus();
    });

    test('a later evidence change starts from the first sentence, not the last outcome', async () => {
        let ranked: unknown = changedJob();
        let refresh = () => Promise.resolve(json({status: 'failed'}));
        global.fetch = jest.fn((url: string) => {
            if (url.includes('/api/job-matches/refresh/')) return refresh();
            if (url.includes('/api/job-matches/status/')) {
                return Promise.resolve(json({state: 'ok', title: 'Matches', message: '', actions: []}));
            }
            if (url.includes('/api/job-matches/ranked/')) {
                return Promise.resolve(json({job_matches: [ranked], organization_matches: []}));
            }
            return Promise.resolve(json({count: 1, next: null, previous: null, results: []}));
        }) as unknown as typeof fetch;
        render(<JobMatchPanel/>);
        fireEvent.click(within(await screen.findByTestId('evidence-changed-notice')).getByRole('button'));
        await waitFor(() => expect(notice()).toHaveAttribute('data-recheck', 'failed'));
        // The results settle by another route (a plain reload)...
        ranked = settledJob();
        refresh = () => Promise.resolve(json({status: 'not_needed'}));
        fireEvent.click(within(notice()).getByRole('button', {name: 'Try again'}));
        await waitFor(() => expect(screen.queryByTestId('evidence-changed-notice')).not.toBeInTheDocument());
        // ...and when evidence changes again the notice is a fresh one.
        ranked = changedJob();
        fireEvent.click(screen.getByTestId('job-match-refresh'));
        await waitFor(() => expect(notice()).toHaveTextContent('Refresh to re-check them.'));
        expect(notice()).toHaveAttribute('data-recheck', 'idle');
    });

    test('a re-read that fails after a successful re-check shows the load error with focus on the heading', async () => {
        let refreshed = false;
        global.fetch = jest.fn((url: string) => {
            if (url.includes('/api/job-matches/refresh/')) {
                refreshed = true;
                return Promise.resolve(json({status: 'published'}));
            }
            if (url.includes('/api/job-matches/status/')) {
                return refreshed
                    ? Promise.resolve(new Response('', {status: 503}))
                    : Promise.resolve(json({state: 'ok', title: 'Matches', message: '', actions: []}));
            }
            if (url.includes('/api/job-matches/ranked/')) {
                return Promise.resolve(json({job_matches: [changedJob()], organization_matches: []}));
            }
            return Promise.resolve(json({count: 1, next: null, previous: null, results: []}));
        }) as unknown as typeof fetch;
        render(<JobMatchPanel/>);
        const button = within(await screen.findByTestId('evidence-changed-notice')).getByRole('button');
        button.focus();
        fireEvent.click(button);
        expect(await screen.findByTestId('job-match-error')).toHaveTextContent('We couldn’t load your job matches.');
        await waitFor(() => expect(heading()).toHaveFocus());
    });

    test('the header refresh re-checks when the notice is showing and keeps its focus', async () => {
        const calls = mockRefresh(changedJob(), settledJob(), () => Promise.resolve(json({status: 'published'})));
        render(<JobMatchPanel/>);
        await screen.findByTestId('evidence-changed-notice');
        const header = screen.getByTestId('job-match-refresh');
        header.focus();
        fireEvent.click(header);
        await waitFor(() => expect(screen.queryByTestId('evidence-changed-notice')).not.toBeInTheDocument());
        expect(posts(calls)).toEqual([{url: '/api/job-matches/refresh/', method: 'POST'}]);
        expect(header).toHaveFocus();
    });

    test('changed evidence replaces the stale banner instead of adding a second one', async () => {
        const stale = {...changedJob(), revision: {stale: true, pending: true, generated_at: '2026-09-22T08:00:00Z'}};
        await renderRanked([stale]);
        expect(screen.getAllByTestId('evidence-changed-notice')).toHaveLength(1);
        expect(screen.queryByTestId('stale-notice')).not.toBeInTheDocument();
        expect(screen.getAllByRole('button', {name: 'Refresh matches'})).toHaveLength(1);
    });

    test('the stale banner alone still reloads without a recompute request and keeps focus', async () => {
        const stale = {...settledJob(), revision: {stale: true, generated_at: '2026-09-22T08:00:00Z'}};
        const fetchMock = await renderRanked([stale]);
        expect(screen.queryByTestId('evidence-changed-notice')).not.toBeInTheDocument();
        const before = fetchMock.mock.calls.length;
        fireEvent.click(within(screen.getByTestId('stale-notice')).getByRole('button', {name: 'Refresh matches'}));
        await waitFor(() => expect(fetchMock.mock.calls.length).toBe(before + 3));
        expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/refresh/'))).toBe(false);
        await waitFor(() => expect(screen.getByRole('heading', {name: 'Your Job Matches'})).toHaveFocus());
    });

    test('the header refresh does not move focus to the heading', async () => {
        await renderRanked([job([])]);
        fireEvent.click(screen.getByTestId('job-match-refresh'));
        await screen.findByTestId('ranked-job-matches');
        expect(screen.getByRole('heading', {name: 'Your Job Matches'})).not.toHaveFocus();
    });

    test('the legend defines every job-card term from the shared vocabulary', async () => {
        await renderRanked([job([
            requirement('compensation.require_public_company', 'match', status('sourced', '2026-09-01T00:00:00Z')),
        ])]);
        const legend = screen.getByTestId('job-match-legend');
        expect(legend.tagName).toBe('DETAILS');
        expect(within(legend).getByText('What these labels mean')).toBeInTheDocument();
        for (const term of Object.values(MATCH_TERMS)) {
            expect(legend).toHaveTextContent(term.label);
            expect(legend).toHaveTextContent(term.meaning);
        }
        for (const key of ['verified', 'stale', 'profile'] as const) {
            expect(legend).toHaveTextContent(EVIDENCE_STATUS_META[key].meaning);
        }
        // The three outcome marks are defined once, each with its word; the
        // glyph itself is hidden from assistive technology.
        const marks = within(legend).getByTestId('requirement-mark-legend');
        expect(Array.from(marks.querySelectorAll('li')).map((li) => li.textContent)).toEqual([
            '✓ Match: The requirement is met.',
            '✗ Mismatch: The requirement is not met.',
            '? Unknown: The requirement could not be decided either way, so it does not count toward requirement coverage.',
        ]);
        for (const key of ['match', 'mismatch', 'unknown'] as const) {
            expect(legend).toHaveTextContent(`${REQUIREMENT_MARKS[key].marker} ${REQUIREMENT_MARKS[key].label}: ${REQUIREMENT_MARKS[key].meaning}`);
        }
        marks.querySelectorAll('.evidence-badge-marker').forEach((mark) => expect(mark).toHaveAttribute('aria-hidden', 'true'));
        expect(legend).toHaveTextContent(`${EVIDENCE_CHANGED_MARKER} Evidence changed: `);
        // True for every chip: an undecided "?" chip is not claimed to be decided.
        expect(legend).toHaveTextContent('No qualifier: A match or mismatch with no qualifier was decided from the job listing itself.');
        expect(legend).not.toHaveTextContent('A requirement with no qualifier');
        // The chip and the figures point at the same definitions.
        expect(chip('compensation.require_public_company').querySelector('.job-match-chip-qualifier-text'))
            .toHaveAttribute('title', MATCH_TERMS.sourced.meaning);
        const titles = Array.from(screen.getByTestId('ranked-job-42').querySelectorAll('.job-match-figure-label'))
            .map((el) => el.getAttribute('title'));
        expect(titles).toEqual([MATCH_TERMS.companyScore.meaning, MATCH_TERMS.fit.meaning, MATCH_TERMS.requirementCoverage.meaning]);
    });

    test('figures are labelled as preset score, fit and requirement coverage', async () => {
        await renderRanked([job([])]);
        const labels = Array.from(screen.getByTestId('ranked-job-42').querySelectorAll('.job-match-figure-label'))
            .map((el) => el.textContent);
        expect(labels).toEqual(['Company score (preset)', 'Fit', 'Requirement coverage']);
        expect(screen.getByTestId('job-42-company-score')).toHaveTextContent('4.2 / 5');
        expect(screen.getByTestId('job-42-fit-score')).toHaveTextContent('85.5 / 100');
        expect(screen.getByTestId('job-42-coverage')).toHaveTextContent('75%');
    });

    test('organization rows use the shared funding and RTO words', async () => {
        await renderRanked([], [org(), org({organization_id: 8, name: 'Blank Co', funding_round: '', rto_policy: 'Z'})]);
        expect(screen.getByTestId('ranked-org-7')).toHaveTextContent('Series G or Later · In-Office');
        // An empty code reads Unknown; an unrecognized one is shown as-is.
        expect(screen.getByTestId('ranked-org-8')).toHaveTextContent('Unknown · Z');
    });
});
