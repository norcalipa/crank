// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {act, cleanup, fireEvent, render, renderHook, screen, waitFor, within} from '@testing-library/react';
import * as React from 'react';

import * as store from '../workspace/store';
import {EVIDENCE_STATUS_META, MATCH_TERMS, REQUIREMENT_MARKS} from '../labels';
import FitSection, {COMPANY_SCORE_TERM, CompanyFit, FitRequirement, PRIORITIES_MET_TERM, PRIORITIES_NONE_TEXT, fitGroupOf, observedText} from './FitSection';
import {retryFrom, useHoldReaderBelow, useOwnCompanyRead} from './useOwnCompanyRead';

const req = (overrides: Partial<FitRequirement> = {}): FitRequirement => ({
    path: 'funding_stage', status: 'match', hard: false, observed: 'a', scope_ok: true, scope: null,
    evidence_status: {state: 'verified', last_verified_at: '2026-01-01T00:00:00Z'}, counted: true, ...overrides,
});

const fit = (overrides: Partial<CompanyFit> = {}): CompanyFit => ({
    organization_id: 4, has_preferences: true, priorities_met: {met: 1, counted: 3}, company_score: 4.2, coverage: 0.6,
    requirements: [
        req(),
        req({path: 'work_location.max_in_office_days', status: 'mismatch', hard: true, observed: 5}),
        req({path: 'compensation.require_public_company', status: 'mismatch', hard: false, observed: false,
            evidence_status: {state: 'profile'}}),
        req({path: 'industry', status: 'unknown', observed: null, evidence_status: null, counted: false}),
    ],
    ...overrides,
});

const answer = (body: unknown, extra: Record<string, unknown> = {}) => ({
    ok: true, status: 200, redirected: false, json: () => Promise.resolve(body), ...extra,
});

const renderFit = (props: Partial<React.ComponentProps<typeof FitSection>> = {}) => render(
    <FitSection organizationId={4} name="Acme" isAuthenticated={true} signInUrl="/accounts/login/?next=%2F%3Fcompany%3D4"
                onEditPriorities={() => {}} {...props}/>);

describe('FitSection (issue #486, AC 15-16)', () => {
    beforeEach(() => {
        store.resetWorkspaceForTests();
        global.fetch = jest.fn().mockResolvedValue(answer(fit()));
    });
    afterEach(cleanup);

    test('signed out: a sign-in prompt and no request', () => {
        renderFit({isAuthenticated: false});
        expect(screen.getByTestId('fit-sign-in')).toHaveTextContent('Sign in to see how Acme fits your priorities.');
        expect(screen.getByRole('link')).toHaveAttribute('href', '/accounts/login/?next=%2F%3Fcompany%3D4');
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('signed out without a sign-in address keeps the sentence and no link', () => {
        renderFit({isAuthenticated: false, signInUrl: null, name: 'x'.repeat(60)});
        expect(screen.getByTestId('fit-sign-in')).toHaveTextContent('Sign in to see how this company fits');
        expect(screen.queryByRole('link')).toBeNull();
    });

    test('loading shows a skeleton while the request is out', async () => {
        global.fetch = jest.fn().mockReturnValue(new Promise(() => {}));
        renderFit();
        expect(screen.getByTestId('fit-loading')).toHaveAttribute('aria-busy', 'true');
        expect(global.fetch).toHaveBeenCalledWith('/api/organizations/4/fit/', expect.objectContaining({credentials: 'same-origin'}));
    });

    test('four groups, three separately labelled numbers, every requirement exactly once', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-priorities-met')).toHaveTextContent(/^1 of 3$/);
        expect(screen.getByTestId('fit-company-score')).toHaveTextContent('4.2 / 5');
        expect(screen.getByTestId('fit-coverage')).toHaveTextContent('60%');
        const labels = within(screen.getByRole('list', {name: 'Match figures'})).getAllByRole('listitem');
        expect(labels.map(item => item.querySelector('.job-match-figure-label')?.textContent)).toEqual(
            ['Priorities met', 'Average rating', 'Requirement coverage']);
        const groupOf = (path: string) => screen.getByTestId(`fit-row-${path}`).closest('[data-testid^="fit-group-"]')!.getAttribute('data-testid');
        expect(groupOf('funding_stage')).toBe('fit-group-meets');
        expect(groupOf('work_location.max_in_office_days')).toBe('fit-group-does-not-meet');
        expect(groupOf('compensation.require_public_company')).toBe('fit-group-tradeoffs');
        expect(groupOf('industry')).toBe('fit-group-unknown');
        expect(document.querySelectorAll('[data-testid^="fit-row-"]')).toHaveLength(4);
        expect(screen.getByRole('heading', {level: 4, name: /Meets/})).toBeInTheDocument();
        expect(screen.getByRole('heading', {level: 4, name: /Does not meet/})).toBeInTheDocument();
    });

    test('a row shows its label, observed value, evidence status and scope in words', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({requirements: [
            req({scope: {countries: ['US'], role_families: ['Engineering']}}),
            req({path: 'work_location.modes', observed: 'hybrid', evidence_status: {state: 'sourced'}}),
            req({path: 'industry', status: 'unknown', observed: null, evidence_status: {state: 'superseded'}}),
        ]})));
        renderFit();
        await screen.findByTestId('fit-ready');
        const scoped = screen.getByTestId('fit-row-funding_stage');
        expect(scoped).toHaveTextContent('Funding stage');
        expect(scoped).toHaveTextContent('Series A');
        expect(screen.getByTestId('fit-evidence-funding_stage')).toHaveTextContent('Verified');
        expect(screen.getByTestId('fit-scope-funding_stage')).toHaveTextContent('Location: US; Roles: Engineering');
        expect(screen.getByTestId('fit-row-work_location.modes')).toHaveTextContent('Hybrid');
        expect(screen.getByTestId('fit-evidence-work_location.modes')).toHaveTextContent('Sourced, not confirmed');
        expect(screen.getByTestId('fit-scope-work_location.modes')).toHaveTextContent('Company-wide');
        expect(screen.getByTestId('fit-evidence-industry')).toHaveTextContent('Evidence changed');
        expect(screen.getByTestId('fit-row-industry')).toHaveTextContent('Unknown');
        expect(document.body.textContent).not.toMatch(/countries|role_families/);
    });

    test('an evidence state the vocabulary does not know shows no status word', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({requirements: [
            req({evidence_status: {state: 'brand-new-state'}}),
        ]})));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.queryByTestId('fit-evidence-funding_stage')).toBeNull();
        expect(screen.getByTestId('fit-scope-funding_stage')).toHaveTextContent('Company-wide');
    });

    test('a fact that does not cover the reader says so and is never in Meets', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({requirements: [
            req({status: 'match', scope_ok: false, scope: {countries: ['US']}, observed: null}),
        ]})));
        renderFit();
        await screen.findByTestId('fit-ready');
        const row = screen.getByTestId('fit-row-funding_stage');
        expect(row.closest('[data-testid]')).not.toBeNull();
        expect(row.getAttribute('data-group')).toBe('unknown');
        expect(screen.getByTestId('fit-group-meets')).not.toContainElement(row);
        expect(screen.getByTestId('fit-out-of-scope-funding_stage'))
            .toHaveTextContent('Counted as not met: the fact on record covers Location: US only.');
        // One sentence: no value, no status word, and the scope is not said twice.
        expect(row).toHaveTextContent(/^\S* ?Funding stageCounted as not met: the fact on record covers Location: US only\.$/);
        expect(screen.queryByTestId('fit-scope-funding_stage')).toBeNull();
        expect(screen.queryByTestId('fit-evidence-funding_stage')).toBeNull();
        expect(row).not.toHaveTextContent('Unknown');
    });

    test('an out-of-scope fact with no scope on record still says so in one sentence', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({requirements: [
            req({status: 'unknown', scope_ok: false, scope: null, observed: null}),
        ]})));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-out-of-scope-funding_stage'))
            .toHaveTextContent('Counted as not met: the fact on record covers part of the company only.');
    });

    test('a requirement with no fact behind it says what is true of it, in the evidence list\'s words', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({requirements: [
            req({path: 'industry', status: 'unknown', observed: null, evidence_status: null, source_kind: null}),
            req({path: 'funding_stage', status: 'match', evidence_status: null, source_kind: 'field'}),
            req({path: 'compensation.minimum_salary', status: 'unknown', observed: null, evidence_status: null, source_kind: null, counted: false}),
            req({path: 'geography.remote_friendly', status: 'unknown', observed: null, evidence_status: null, source_kind: null, counted: false}),
            req({path: 'geography.regions', status: 'unknown', observed: null, evidence_status: null, source_kind: null, counted: false}),
        ]})));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-scope-industry')).toHaveTextContent(/^No accepted evidence$/);
        expect(screen.getByTestId('fit-row-industry')).not.toHaveTextContent('Company-wide');
        expect(document.body.textContent).not.toContain('No fact on record');
        // A field-sourced value is a fact on record, company-wide.
        expect(screen.getByTestId('fit-scope-funding_stage')).toHaveTextContent('Company-wide');
        // Decided from a job's own pay, place and remote flag: never for a company, and the row says where it is.
        for (const path of ['compensation.minimum_salary', 'geography.remote_friendly', 'geography.regions']) {
            expect(screen.getByTestId(`fit-scope-${path}`))
                .toHaveTextContent('Checked for each job, not for the company. See Open jobs.');
        }
    });

    test('the disclosure is short and explains the numbers, the marks and the two status words', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        const legend = screen.getByTestId('fit-legend');
        expect(legend.tagName).toBe('DETAILS');
        expect(legend).not.toHaveAttribute('open');
        const text = legend.textContent || '';
        for (const term of [COMPANY_SCORE_TERM, MATCH_TERMS.requirementCoverage, MATCH_TERMS.sourced]) {
            expect(text).toContain(term.label);
            expect(text).toContain(term.meaning);
        }
        for (const mark of Object.values(REQUIREMENT_MARKS)) expect(text).toContain(mark.meaning);
        expect(text).toContain(EVIDENCE_STATUS_META.verified.meaning);
        // The first figure says what it counts, and the old disagreement text is gone.
        expect(text).toContain(PRIORITIES_MET_TERM.label);
        expect(text).toContain(PRIORITIES_MET_TERM.meaning);
        expect(text).toContain('Unknown rows, and rows whose fact covers only part of the company, count as not met; priorities checked for each job are not counted.');
        expect(text).not.toContain('cannot reach 100');
        expect(text).not.toContain('Why the fit and the rows can disagree');
        expect(text).not.toContain('the fit reads the profile values');
        expect(text).toContain('Verified describes the fact in "Data Freshness & Sources"; Sourced, not confirmed describes how a row was decided from it.');
        // Cut to what a reader meets: no entry for a status this view cannot show, no run-together words.
        expect(text).not.toContain(MATCH_TERMS.changed.label);
        expect(text).not.toContain('Refresh matches');
        expect(text).not.toMatch(/confirmedeven|confirmed[a-z]/);
        expect(text.split(/\s+/).length).toBeLessThan(230);
        expect(within(legend).getByText('What these labels mean').tagName).toBe('SUMMARY');
    });

    test('the disagreement note is gone: the figure is a count of the rows below', async () => {
        renderFit();
        const ready = await screen.findByTestId('fit-ready');
        expect(within(ready).queryByTestId('fit-number-note')).toBeNull();
        expect(ready.textContent).not.toContain('the two can disagree');
        expect(within(ready).queryByTestId('fit-priorities-none')).toBeNull();
    });

    test('the figure reads "met of counted" and equals the number of rows under Meets', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({
            priorities_met: {met: 2, counted: 3},
            requirements: [
                req(),
                req({path: 'work_location.modes', observed: 'hybrid'}),
                req({path: 'work_location.max_in_office_days', status: 'mismatch', observed: 5}),
                req({path: 'compensation.minimum_salary', status: 'unknown', observed: null, evidence_status: null, counted: false}),
            ],
        })));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-priorities-met')).toHaveTextContent(/^2 of 3$/);
        expect(within(screen.getByTestId('fit-group-meets')).getAllByTestId(/^fit-row-/)).toHaveLength(2);
        const counted = screen.getAllByTestId(/^fit-row-/).filter(row => !within(row).queryByText(/Checked for each job/));
        expect(counted).toHaveLength(3);
        const label = document.querySelector('.job-match-figure-label')!;
        expect(label).toHaveTextContent('Priorities met');
        expect(label.getAttribute('title')).toBe(PRIORITIES_MET_TERM.meaning);
    });

    test('a line under the figures says how many rows are checked per job and left out of the count', async () => {
        const perJob = (path: string) => req({path, status: 'unknown', observed: null, evidence_status: null, counted: false});
        global.fetch = jest.fn().mockResolvedValue(answer(fit({
            priorities_met: {met: 1, counted: 1},
            requirements: [req(), perJob('compensation.minimum_salary'), perJob('job.titles')],
        })));
        const {unmount} = renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-priorities-per-job'))
            .toHaveTextContent('2 priorities below are checked for each job and are not counted.');
        unmount();
        global.fetch = jest.fn().mockResolvedValue(answer(fit({
            priorities_met: {met: 1, counted: 1},
            requirements: [req(), perJob('compensation.minimum_salary')],
        })));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-priorities-per-job'))
            .toHaveTextContent('One priority below is checked for each job and is not counted.');
    });

    test('the per-job line is hidden when every row is counted', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({priorities_met: {met: 1, counted: 1}, requirements: [req()]})));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.queryByTestId('fit-priorities-per-job')).toBeNull();
    });

    test('within a group the counted rows come before the per-job rows', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({
            priorities_met: {met: 0, counted: 1},
            requirements: [
                req({path: 'compensation.minimum_salary', status: 'unknown', observed: null, evidence_status: null, counted: false}),
                req({path: 'funding_stage', status: 'unknown', observed: null, evidence_status: null, scope_ok: false}),
            ],
        })));
        renderFit();
        await screen.findByTestId('fit-ready');
        const ids = within(screen.getByTestId('fit-group-unknown')).getAllByTestId(/^fit-row-/).map(row => row.getAttribute('data-testid'));
        expect(ids).toEqual(['fit-row-funding_stage', 'fit-row-compensation.minimum_salary']);
    });

    test('with nothing a company can decide the figure is a dash and one sentence says why', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({
            priorities_met: null,
            requirements: [req({path: 'compensation.minimum_salary', status: 'unknown', observed: null, evidence_status: null, counted: false})],
        })));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-priorities-met')).toHaveTextContent(/^—$/);
        const sentence = screen.getByTestId('fit-priorities-none');
        expect(sentence).toHaveTextContent(PRIORITIES_NONE_TEXT);
        // It speaks only for the rows under it: not for an exclusion, and not for priorities Job Search does not check.
        expect(sentence).toHaveTextContent('No priority in the rows below can be decided from a fact about a company; each is checked for each job.');
        expect(sentence).not.toHaveTextContent('None of your priorities');
        expect(sentence.closest('details')).toBeNull();
    });

    test('a company that was not excluded says nothing about exclusion', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.queryByTestId('fit-excluded')).toBeNull();
    });

    test('a reader whose only priority is an exclusion gets one sentence, no coverage and no empty groups', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({
            has_preferences: true, excluded: true, requirements: [], priorities_met: null, coverage: null,
        })));
        renderFit();
        const only = await screen.findByTestId('fit-exclusions-only');
        expect(only).toHaveTextContent('Your saved priorities are only exclusions, so there is no requirement to check Acme against.');
        expect(only).toHaveTextContent('Job Search listings');
        expect(screen.queryByTestId('fit-no-priorities')).toBeNull();
        expect(screen.queryByTestId('fit-ready')).toBeNull();
        expect(screen.queryByTestId('fit-coverage')).toBeNull();
        expect(screen.queryByTestId('fit-group-meets')).toBeNull();
        // There are no rows for "the rest" to refer to.
        expect(within(only).getByTestId('fit-excluded')).toHaveTextContent('Company rankings do not use your priorities.');
        expect(within(only).getByTestId('fit-excluded')).not.toHaveTextContent('still shows how it fits');
    });

    test('an exclusion-only reader whose company is not excluded gets the sentence without the exclusion note', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({
            has_preferences: true, excluded: false, requirements: [], priorities_met: null, coverage: null,
        })));
        renderFit();
        await screen.findByTestId('fit-exclusions-only');
        expect(screen.queryByTestId('fit-excluded')).toBeNull();
    });

    test('unsupported priorities plus an exclusion still read "cannot be checked", not the ready view', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({
            has_preferences: true, requirements: [], coverage: null, unsupported: ['roles.titles'],
        })));
        renderFit();
        const prompt = await screen.findByTestId('fit-unsupported-only');
        expect(prompt).toHaveTextContent('cannot be checked against a company yet: Role titles.');
        expect(screen.queryByTestId('fit-ready')).toBeNull();
        expect(screen.queryByTestId('fit-exclusions-only')).toBeNull();
    });

    test('the heading takes focus (a jump target) and the settle callback runs when the read lands', async () => {
        const onSettled = jest.fn();
        const ref = React.createRef<HTMLHeadingElement>();
        render(<FitSection ref={ref} organizationId={4} name="Acme" isAuthenticated={true} signInUrl={null}
                           onEditPriorities={() => {}} onSettled={onSettled}/>);
        expect(onSettled).not.toHaveBeenCalled();
        await screen.findByTestId('fit-ready');
        expect(onSettled).toHaveBeenCalledTimes(1);
        expect(ref.current).toBe(document.getElementById('company-fit-title'));
        expect(ref.current).toHaveAttribute('tabindex', '-1');
    });

    test('the failure alert names what Retry retries', async () => {
        global.fetch = jest.fn().mockRejectedValue(new Error('boom'));
        renderFit();
        const alert = await screen.findByTestId('fit-error');
        expect(within(alert).getByRole('button', {name: 'Retry loading fit'})).toBeInTheDocument();
    });

    test('empty groups say so', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({requirements: [req()]})));
        renderFit();
        await screen.findByTestId('fit-ready');
        for (const key of ['tradeoffs', 'does-not-meet', 'unknown']) {
            expect(screen.getByTestId(`fit-empty-${key}`)).toBeInTheDocument();
        }
        expect(screen.queryByTestId('fit-empty-meets')).toBeNull();
        expect(screen.getByTestId('fit-empty-tradeoffs')).toHaveTextContent('No preference is known to be unmet.');
        expect(screen.getByTestId('fit-empty-does-not-meet')).toHaveTextContent('No requirement is known to be unmet.');
        expect(screen.getByTestId('fit-empty-unknown')).toHaveTextContent('Nothing is undecided.');
        global.fetch = jest.fn().mockResolvedValue(answer(fit({requirements: [req({status: 'unknown', observed: null})]})));
        cleanup();
        renderFit();
        expect(await screen.findByTestId('fit-empty-meets')).toHaveTextContent('No priority is confirmed as met.');
    });

    test('figures show a dash when a number is not known', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({priorities_met: null, company_score: null, coverage: null})));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-priorities-met')).toHaveTextContent('—');
        expect(screen.getByTestId('fit-company-score')).toHaveTextContent('—');
        expect(screen.getByTestId('fit-coverage')).toHaveTextContent('—');
    });

    test('priorities the engine cannot check are named, never left out silently', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({unsupported: ['roles.titles', 'compensation.minimum_total_compensation']})));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-unsupported'))
            .toHaveTextContent('Not checked for a company: Role titles, Minimum total compensation.');
        cleanup();
        global.fetch = jest.fn().mockResolvedValue(answer(fit({unsupported: []})));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.queryByTestId('fit-unsupported')).toBeNull();
    });

    test('a reader whose saved priorities are all unsupported is not told to set priorities', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({
            has_preferences: false, requirements: [], unsupported: ['roles.titles', 'compensation.minimum_total_compensation'],
        })));
        const onEdit = jest.fn();
        renderFit({onEditPriorities: onEdit});
        const prompt = await screen.findByTestId('fit-unsupported-only');
        expect(prompt).toHaveTextContent('Your saved priorities cannot be checked against a company yet: Role titles, Minimum total compensation.');
        expect(prompt).toHaveTextContent('Add another priority to see how Acme fits.');
        expect(screen.queryByTestId('fit-no-priorities')).toBeNull();
        expect(prompt).not.toHaveTextContent('Set your priorities');
        fireEvent.click(screen.getByRole('button', {name: 'Edit your priorities'}));
        expect(onEdit).toHaveBeenCalledTimes(1);
    });

    test('Retry keeps the keyboard focus in the section, on its heading', async () => {
        global.fetch = jest.fn().mockResolvedValueOnce(answer({}, {ok: false, status: 500})).mockResolvedValue(answer(fit()));
        renderFit();
        const retry = await screen.findByTestId('fit-retry');
        retry.focus();
        fireEvent.click(retry);
        expect(document.activeElement).toBe(document.getElementById('company-fit-title'));
        await screen.findByTestId('fit-ready');
        expect(document.activeElement).toBe(document.getElementById('company-fit-title'));
    });

    test('no saved priorities: a sentence and a control that opens the editor', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({has_preferences: false, requirements: []})));
        const onEdit = jest.fn();
        renderFit({onEditPriorities: onEdit});
        const none = await screen.findByTestId('fit-no-priorities');
        expect(none).toHaveTextContent('Set your priorities to see how Acme fits.');
        fireEvent.click(screen.getByTestId('fit-set-priorities'));
        expect(onEdit).toHaveBeenCalledTimes(1);
    });

    test.each([
        ['a server failure', answer({}, {ok: false, status: 500})],
        ['a body that is not a fit', answer({nope: true})],
    ])('%s: a fixed sentence with Retry that clears, and no server text', async (_name, response) => {
        const fetchMock = jest.fn().mockResolvedValueOnce(response).mockResolvedValue(answer(fit()));
        global.fetch = fetchMock;
        renderFit();
        const error = await screen.findByTestId('fit-error');
        expect(error).toHaveTextContent('Fit could not be loaded.');
        fireEvent.click(screen.getByTestId('fit-retry'));
        await screen.findByTestId('fit-ready');
        expect(fetchMock).toHaveBeenCalledTimes(2);
        expect(screen.queryByTestId('fit-error')).toBeNull();
    });

    test('a thrown network error shows the fixed sentence, never its text', async () => {
        global.fetch = jest.fn().mockRejectedValue(new Error('secret socket detail'));
        renderFit();
        await screen.findByTestId('fit-error');
        expect(document.body.textContent).not.toContain('secret socket detail');
    });

    test.each([
        ['a redirect', answer('<html>', {redirected: true})],
        ['a 401', answer({}, {ok: false, status: 401})],
        ['a login page followed into HTML', {ok: true, status: 200, redirected: false, json: () => Promise.reject(new SyntaxError('bad'))}],
    ])('%s reads as sign in, never data or an error', async (_name, response) => {
        global.fetch = jest.fn().mockResolvedValue(response);
        renderFit();
        const prompt = await screen.findByTestId('fit-sign-in');
        expect(prompt).toHaveTextContent('Sign in to see how Acme fits your priorities.');
        expect(screen.queryByTestId('fit-error')).toBeNull();
        expect(screen.queryByTestId('fit-ready')).toBeNull();
    });

    test('an answer for company A never renders for company B', async () => {
        const waiting: ((value: unknown) => void)[] = [];
        global.fetch = jest.fn().mockImplementation(() => new Promise(resolve => waiting.push(resolve)));
        const view = renderFit({organizationId: 4, name: 'A Co'});
        view.rerender(<FitSection organizationId={5} name="B Co" isAuthenticated={true} signInUrl={null}
                                  onEditPriorities={() => {}}/>);
        expect(screen.getByTestId('fit-loading')).toHaveTextContent('B Co');
        await act(async () => {
            waiting[0](answer(fit({organization_id: 4})));
        });
        expect(screen.queryByTestId('fit-ready')).toBeNull();
        expect(screen.getByTestId('fit-loading')).toBeInTheDocument();
        await act(async () => {
            waiting[1](answer(fit({organization_id: 5, requirements: [req({path: 'industry', observed: 'Software'})]})));
        });
        expect(await screen.findByTestId('fit-row-industry')).toHaveTextContent('Software');
    });

    test('a purge empties the section, and a late answer after it is ignored', async () => {
        let late: (value: unknown) => void = () => {};
        global.fetch = jest.fn().mockImplementation(() => new Promise(resolve => {
            late = resolve;
        }));
        renderFit();
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
        });
        expect(await screen.findByTestId('fit-sign-in')).toBeInTheDocument();
        await act(async () => {
            late(answer(fit()));
        });
        expect(screen.queryByTestId('fit-ready')).toBeNull();
        expect(screen.getByTestId('fit-sign-in')).toBeInTheDocument();
    });

    test('data that was shown is gone after a purge', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
        });
        expect(screen.queryByTestId('fit-row-funding_stage')).toBeNull();
        expect(screen.getByTestId('fit-sign-in')).toBeInTheDocument();
    });

    test('an account change empties the section and drops a late answer', async () => {
        store.setWorkspaceAccount({status: 'authenticated', key: 'ann'});
        let late: (value: unknown) => void = () => {};
        global.fetch = jest.fn().mockImplementation(() => new Promise(resolve => {
            late = resolve;
        }));
        renderFit();
        act(() => store.setWorkspaceAccount({status: 'authenticated', key: 'bob'}));
        expect(await screen.findByTestId('fit-sign-in')).toBeInTheDocument();
        await act(async () => {
            late(answer(fit()));
        });
        expect(screen.queryByTestId('fit-ready')).toBeNull();
        // An unchanged account does not empty anything.
        cleanup();
        global.fetch = jest.fn().mockResolvedValue(answer(fit()));
        renderFit();
        await screen.findByTestId('fit-ready');
        act(() => store.setWorkspaceAccount({status: 'authenticated', key: 'bob'}));
        expect(screen.getByTestId('fit-ready')).toBeInTheDocument();
    });

    test('nothing is requested for an id that is not a company id', () => {
        renderFit({organizationId: -3});
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('nothing the section holds is kept outside it', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        cleanup();
        renderFit({isAuthenticated: false});
        expect(screen.queryByTestId('fit-row-funding_stage')).toBeNull();
        expect(Object.keys(window.__crankWorkspace__ ?? {}).join()).not.toMatch(/fit/i);
    });
});

describe('the fit follows a saved priorities change (issue #486, F4)', () => {
    const second = () => fit({priorities_met: {met: 1, counted: 1}, requirements: [req({path: 'funding_stage'})]});
    const calls = () => (global.fetch as jest.Mock).mock.calls.length;
    beforeEach(() => {
        store.resetWorkspaceForTests();
        global.fetch = jest.fn().mockResolvedValue(answer(fit()));
    });
    afterEach(cleanup);

    test('a new revision reads the fit again, keeps what is shown meanwhile and then replaces it', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        let release: (value: unknown) => void = () => {};
        (global.fetch as jest.Mock).mockImplementationOnce(() => new Promise(resolve => {
            release = resolve;
        }));
        act(() => store.setPrioritiesRevision(7));
        expect(calls()).toBe(2);
        expect(screen.getByTestId('fit-ready')).toHaveAttribute('aria-busy', 'true');
        expect(screen.getByTestId('fit-priorities-met')).toHaveTextContent('1 of 3');
        expect(screen.queryByTestId('fit-loading')).toBeNull();
        await act(async () => {
            release(answer(second()));
        });
        expect(screen.getByTestId('fit-priorities-met')).toHaveTextContent('1 of 1');
        expect(screen.getByTestId('fit-ready')).not.toHaveAttribute('aria-busy');
        expect(calls()).toBe(2);
    });

    test('the same revision again, and a revision of null, read nothing', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        act(() => store.setPrioritiesRevision(7));
        await waitFor(() => expect(screen.getByTestId('fit-ready')).not.toHaveAttribute('aria-busy'));
        expect(calls()).toBe(2);
        act(() => store.resetPriorities());
        expect(calls()).toBe(2);
        act(() => store.setPrioritiesRevision(7));
        await waitFor(() => expect(calls()).toBe(3));
    });

    test('a revision that was already set when the section mounted is not a change', async () => {
        act(() => store.setPrioritiesRevision(4));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(calls()).toBe(1);
    });

    test('an answer started before a change is never shown after it', async () => {
        const waiting: ((value: unknown) => void)[] = [];
        global.fetch = jest.fn().mockImplementation(() => new Promise(resolve => waiting.push(resolve)));
        renderFit();
        act(() => store.setPrioritiesRevision(8));
        expect(waiting).toHaveLength(2);
        await act(async () => {
            waiting[0](answer(fit()));
        });
        expect(screen.queryByTestId('fit-ready')).toBeNull();
        await act(async () => {
            waiting[1](answer(second()));
        });
        expect(screen.getByTestId('fit-priorities-met')).toHaveTextContent('1 of 1');
    });

    test('a second change while one read is out keeps only the last answer', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        const waiting: ((value: unknown) => void)[] = [];
        (global.fetch as jest.Mock).mockImplementation(() => new Promise(resolve => waiting.push(resolve)));
        act(() => store.setPrioritiesRevision(8));
        act(() => store.setPrioritiesRevision(9));
        await act(async () => {
            waiting[0](answer(fit({priorities_met: {met: 0, counted: 9}})));
        });
        expect(screen.getByTestId('fit-priorities-met')).toHaveTextContent('1 of 3');
        await act(async () => {
            waiting[1](answer(second()));
        });
        expect(screen.getByTestId('fit-priorities-met')).toHaveTextContent('1 of 1');
    });

    test('a purge during a re-read leaves nothing, and its late answer is dropped', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        let late: (value: unknown) => void = () => {};
        (global.fetch as jest.Mock).mockImplementationOnce(() => new Promise(resolve => {
            late = resolve;
        }));
        act(() => store.setPrioritiesRevision(8));
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
        });
        expect(screen.queryByTestId('fit-row-funding_stage')).toBeNull();
        await act(async () => {
            late(answer(second()));
        });
        expect(screen.queryByTestId('fit-ready')).toBeNull();
        expect(screen.getByTestId('fit-sign-in')).toBeInTheDocument();
        // A number after the purge reads nothing for an account that is gone.
        act(() => store.setPrioritiesRevision(9));
        expect(calls()).toBe(2);
    });

    test('a failed re-read shows the error with Retry, never the old numbers', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        (global.fetch as jest.Mock).mockResolvedValueOnce(answer({}, {ok: false, status: 500}));
        act(() => store.setPrioritiesRevision(8));
        await screen.findByTestId('fit-error');
        expect(screen.queryByTestId('fit-priorities-met')).toBeNull();
        fireEvent.click(screen.getByTestId('fit-retry'));
        await screen.findByTestId('fit-ready');
    });

    test('a change while the first read is out starts it again', async () => {
        const waiting: ((value: unknown) => void)[] = [];
        global.fetch = jest.fn().mockImplementation(() => new Promise(resolve => waiting.push(resolve)));
        renderFit();
        act(() => store.setPrioritiesRevision(3));
        expect(screen.getByTestId('fit-loading')).toBeInTheDocument();
        expect(waiting).toHaveLength(2);
    });

    test('the "Set your priorities" prompt becomes the numbers after a first save', async () => {
        global.fetch = jest.fn().mockResolvedValueOnce(answer(fit({has_preferences: false, requirements: []})))
            .mockResolvedValue(answer(fit()));
        renderFit();
        await screen.findByTestId('fit-no-priorities');
        act(() => store.setPrioritiesRevision(1));
        expect(screen.getByTestId('fit-no-priorities')).toHaveAttribute('aria-busy', 'true');
        await screen.findByTestId('fit-ready');
        expect(screen.queryByTestId('fit-no-priorities')).toBeNull();
    });
});

describe('useOwnCompanyRead tags what it holds with the company (issue #486, rule 4)', () => {
    beforeEach(() => {
        store.resetWorkspaceForTests();
        global.fetch = jest.fn().mockResolvedValue(answer(fit()));
    });

    test('no render for company B, not even the first one, returns company A\'s answer', async () => {
        const isFit = (data: unknown): data is CompanyFit => true;
        const renders: {id: number; kind: string}[] = [];
        const {rerender} = renderHook(({id}) => {
            const {state} = useOwnCompanyRead<CompanyFit>('fit', id, true, isFit);
            renders.push({id, kind: state.kind});
            return state;
        }, {initialProps: {id: 4}});
        await waitFor(() => expect(renders[renders.length - 1]).toEqual({id: 4, kind: 'ready'}));
        const before = renders.length;
        rerender({id: 5});
        const after = renders.slice(before).filter(entry => entry.id === 5);
        expect(after.length).toBeGreaterThan(0);
        expect(after[0].kind).toBe('loading');
        for (const entry of after) expect(entry.kind).not.toBe('ready');
        await waitFor(() => expect(renders[renders.length - 1]).toEqual({id: 5, kind: 'ready'}));
    });
});

describe('account hydration while the read is out (issue #486, review round 1)', () => {
    beforeEach(() => {
        store.resetWorkspaceForTests();
    });
    afterEach(cleanup);

    test('the account becoming known mid-read does not empty the section or drop the answer', async () => {
        // `/?company=<id>` while signed in: the read starts before the workspace
        // knows the account. Hydrating "unknown" -> "authenticated" is not a sign-out.
        expect(store.getWorkspaceSnapshot().account.status).toBe('unknown');
        let resolve: (value: unknown) => void = () => {};
        global.fetch = jest.fn().mockReturnValue(new Promise(r => {
            resolve = r;
        }));
        renderFit();
        act(() => store.setWorkspaceAccount({status: 'authenticated', key: 'ann'}));
        expect(screen.queryByTestId('fit-sign-in')).toBeNull();
        await act(async () => {
            resolve(answer(fit()));
        });
        expect(await screen.findByTestId('fit-ready')).toBeInTheDocument();
        expect(screen.queryByTestId('fit-sign-in')).toBeNull();
    });
});

describe('the reader\'s place when the fit arrives (issue #486, review round 1)', () => {
    let sectionBottom = -100;
    let rects: jest.SpyInstance;
    const scrollBy = jest.fn();
    const Page = () => (
        <div>
            <FitSection organizationId={4} name="Acme" isAuthenticated={true} signInUrl={null} onEditPriorities={() => {}}/>
            <p data-testid="after">after</p>
        </div>
    );
    beforeEach(() => {
        store.resetWorkspaceForTests();
        scrollBy.mockReset();
        window.scrollBy = scrollBy as unknown as typeof window.scrollBy;
        rects = jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
            const ready = !!document.querySelector('[data-testid="fit-ready"]');
            const top = this.tagName === 'P' ? (ready ? 700 : 50) : 0;
            const bottom = this.tagName === 'SECTION' ? sectionBottom : this.tagName === 'HEADER' ? 49 : top;
            const height = this.tagName === 'HEADER' ? 49 : 0;
            return {top, bottom, left: 0, right: 0, width: 0, height, x: 0, y: top, toJSON: () => ({})} as DOMRect;
        });
    });
    afterEach(() => {
        rects.mockRestore();
        document.querySelector('header')?.remove();
        cleanup();
    });

    const arrives = async () => {
        let resolve: (value: unknown) => void = () => {};
        global.fetch = jest.fn().mockReturnValue(new Promise(r => {
            resolve = r;
        }));
        render(<Page/>);
        await act(async () => {
            resolve(answer(fit()));
        });
        expect(await screen.findByTestId('fit-ready')).toBeInTheDocument();
    };

    test('a reader below the Fit section is held where they are', async () => {
        sectionBottom = -100;
        await arrives();
        expect(scrollBy).toHaveBeenCalledWith({top: 650, behavior: 'instant'});
    });

    test('a reader who can see the section is not moved', async () => {
        sectionBottom = 300;
        await arrives();
        expect(scrollBy).not.toHaveBeenCalled();
    });

    test('a section that ends under the phone top bar is held too; a bar that is not sticky covers nothing', async () => {
        const bar = document.createElement('header');
        bar.className = 'app-mobile-topbar';
        bar.style.position = 'sticky';
        document.body.appendChild(bar);
        sectionBottom = 30;
        await arrives();
        expect(scrollBy).toHaveBeenCalledWith({top: 650, behavior: 'instant'});
        cleanup();
        scrollBy.mockReset();
        bar.style.position = 'static'; // not shown as a bar at 768 px and wider
        await arrives();
        expect(scrollBy).not.toHaveBeenCalled();
        cleanup();
        scrollBy.mockReset();
        bar.style.position = 'fixed';
        document.body.appendChild(bar);
        await arrives();
        expect(scrollBy).toHaveBeenCalled();
    });
});

describe('fitGroupOf and observedText', () => {
    test('group rules', () => {
        expect(fitGroupOf(req({status: 'match'}))).toBe('meets');
        expect(fitGroupOf(req({status: 'match', scope_ok: false}))).toBe('unknown');
        expect(fitGroupOf(req({status: 'mismatch', hard: true}))).toBe('does-not-meet');
        expect(fitGroupOf(req({status: 'mismatch', hard: false}))).toBe('tradeoffs');
        expect(fitGroupOf(req({status: 'unknown'}))).toBe('unknown');
    });

    test('observed values read as words', () => {
        expect(observedText(req({observed: null}))).toBe('Unknown');
        expect(observedText(req({observed: ''}))).toBe('Unknown');
        expect(observedText(req({observed: true}))).toBe('Yes');
        expect(observedText(req({observed: false}))).toBe('No');
        expect(observedText(req({observed: ['US', 'CA']}))).toBe('US, CA');
        expect(observedText(req({observed: []}))).toBe('Unknown');
        expect(observedText(req({observed: {a: 1}}))).toBe('Unknown');
        expect(observedText(req({observed: 'p'}))).toBe('Public');
    });

    // Every requirement path of the matching engine (crank/agents/jobs/matching.py,
    // `_requirement_set_paths`) and every shape of `observed` it can send for it
    // (`_eval_requirement`), each as the words a reader would write. The table in
    // the PR body lists the same values; a path or shape added to the engine is
    // added here.
    test.each([
        // compensation.minimum_salary: a listing's own pay (an integer), or its currency when it differs.
        ['compensation.minimum_salary', 120000, '120,000 per year'],
        ['compensation.minimum_salary', 'EUR', 'Pay is published in EUR'],
        ['compensation.minimum_salary', 'usd', 'Pay is published in USD'],
        // compensation.equity_minimum_percent: a float.
        ['compensation.equity_minimum_percent', 0.5, '0.5%'],
        ['compensation.equity_minimum_percent', 1, '1%'],
        // compensation.require_public_company: the evidence's own prose, or the funding letter.
        ['compensation.require_public_company', 'P', 'Public'],
        ['compensation.require_public_company', 'B', 'Series B'],
        ['compensation.require_public_company', 'Private company', 'Private company'],
        ['compensation.require_public_company', 'Public company', 'Public company'],
        // work_location.modes: "remote" | "hybrid" | "in-office".
        ['work_location.modes', 'remote', 'Remote'],
        ['work_location.modes', 'hybrid', 'Hybrid'],
        ['work_location.modes', 'in-office', 'In-Office'],
        ['work_location.modes', 'R', 'Remote'],
        ['work_location.modes', 'flexible', 'flexible'],
        // work_location.countries: the reader's own lower-case country, or the evidence's prose.
        ['work_location.countries', 'us', 'US'],
        ['work_location.countries', 'usa', 'USA'],
        ['work_location.countries', 'uk', 'UK'],
        ['work_location.countries', 'ca', 'CA'],
        ['work_location.countries', 'de', 'DE'],
        ['work_location.countries', 'gbr', 'GBR'],
        ['work_location.countries', 'canada', 'Canada'],
        ['work_location.countries', 'united states', 'United States'],
        ['work_location.countries', 'United States', 'United States'],
        ['work_location.countries', 'Austin, TX, USA', 'Austin, TX, USA'],
        // work_location.max_in_office_days: a number of days.
        ['work_location.max_in_office_days', 0, '0 days a week in the office'],
        ['work_location.max_in_office_days', 1, '1 day a week in the office'],
        ['work_location.max_in_office_days', 3, '3 days a week in the office'],
        ['work_location.max_in_office_days', 5, '5 days a week in the office'],
        // geography.regions: the reader's lower-case region.
        ['geography.regions', 'san francisco', 'San Francisco'],
        ['geography.regions', 'emea', 'Emea'],
        // geography.remote_friendly: a boolean.
        ['geography.remote_friendly', true, 'Yes'],
        ['geography.remote_friendly', false, 'No'],
        // industry: the lower-case words of the profile or the reader.
        ['industry', 'software', 'Software'],
        ['industry', 'fintech', 'Fintech'],
        ['industry', 'health care', 'Health Care'],
        ['industry', 'ai ml', 'AI ML'],
        ['industry', 'saas', 'SAAS'],
        // funding_stage: one lower-case letter, or the words of a stage the engine does not alias.
        ['funding_stage', 's', 'Seed'],
        ['funding_stage', 'a', 'Series A'],
        ['funding_stage', 'b', 'Series B'],
        ['funding_stage', 'c', 'Series C'],
        ['funding_stage', 'd', 'Series D'],
        ['funding_stage', 'e', 'Series E'],
        ['funding_stage', 'f', 'Series F'],
        ['funding_stage', 'x', 'Series G or Later'],
        ['funding_stage', 'o', 'Other Private'],
        ['funding_stage', 'p', 'Public'],
        ['funding_stage', 'series h', 'Series H'],
        ['funding_stage', 'growth', 'Growth'],
        // culture: the matched or the profile's lower-case tag.
        ['culture', 'collaborative', 'Collaborative'],
        ['culture', 'fast paced', 'Fast Paced'],
        // vesting.*: months, and a boolean.
        ['vesting.max_cliff_months', 12, '12 months'],
        ['vesting.max_cliff_months', 0, '0 months'],
        ['vesting.max_vesting_months', 1, '1 month'],
        ['vesting.max_vesting_months', 48, '48 months'],
        ['vesting.prefer_accelerated', true, 'Yes'],
        ['vesting.prefer_accelerated', false, 'No'],
        // A path this does not know prints its value unchanged.
        ['something.new', 'raw-value', 'raw-value'],
    ])('%s: %p reads %p', (path, observed, words) => {
        expect(observedText(req({path, observed}))).toBe(words);
    });

    test('no value of any requirement path prints as a bare lower-case code or a bare number', () => {
        const values: [string, unknown][] = [
            ['funding_stage', 'b'], ['work_location.modes', 'remote'], ['work_location.max_in_office_days', 0],
            ['work_location.countries', 'us'], ['industry', 'software'], ['culture', 'collaborative'],
            ['geography.regions', 'emea'], ['compensation.minimum_salary', 100000],
            ['vesting.max_cliff_months', 12], ['compensation.equity_minimum_percent', 2],
        ];
        for (const [path, observed] of values) {
            const text = observedText(req({path, observed}));
            expect(text).not.toBe(String(observed));
            expect(text).not.toMatch(/^[a-z]{1,3}$/);
            expect(text).not.toMatch(/^\d+(\.\d+)?$/);
        }
    });
});

describe('Retry and the reader\'s place', () => {
    test('retryFrom without a heading still retries', () => {
        const retry = jest.fn();
        retryFrom('no-such-heading', retry);
        expect(retry).toHaveBeenCalledTimes(1);
    });

    // jsdom lays nothing out: each probe reports the rectangles the browser would.
    // `next` is where what follows the section sits once the new content is in
    // the page: it is applied in a layout effect that runs before the hook's.
    const Probe = ({value, next}: {value: number; next: number}) => {
        const ref = React.useRef<HTMLElement>(null);
        React.useLayoutEffect(() => {
            if (value > 1) afterTop = next;
        }, [value, next]);
        useHoldReaderBelow(ref, value);
        return (
            <div>
                <section ref={ref} data-testid="held">{value}</section>
                <p data-testid="after">after</p>
            </div>
        );
    };
    const scrollBy = jest.fn();
    let sectionBottom = -100;
    let afterTop = 50;
    let rects: jest.SpyInstance;
    beforeEach(() => {
        scrollBy.mockReset();
        window.scrollBy = scrollBy as unknown as typeof window.scrollBy;
        rects = jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
            const top = this.tagName === 'P' ? afterTop : 0;
            const bottom = this.tagName === 'SECTION' ? sectionBottom : top;
            return {top, bottom, left: 0, right: 0, width: 0, height: 0, x: 0, y: top, toJSON: () => ({})} as DOMRect;
        });
    });
    afterEach(() => {
        rects.mockRestore();
        cleanup();
    });

    test('a section above the top of the screen that grows pushes the page back by the growth', () => {
        sectionBottom = -100;
        afterTop = 50;
        const view = render(<Probe value={1} next={50}/>);
        view.rerender(<Probe value={2} next={400}/>);
        expect(scrollBy).toHaveBeenCalledWith({top: 350, behavior: 'instant'});
    });

    test('nothing moves when the browser kept the reader\'s place, or the section was on screen', () => {
        sectionBottom = -100;
        afterTop = 50;
        const view = render(<Probe value={1} next={50}/>);
        view.rerender(<Probe value={2} next={50}/>);
        expect(scrollBy).not.toHaveBeenCalled();
        sectionBottom = 200;
        afterTop = 300;
        view.rerender(<Probe value={3} next={700}/>);
        expect(scrollBy).not.toHaveBeenCalled();
    });

    test('a section that ends the page is measured by its own bottom', () => {
        const Last = ({value}: {value: number}) => {
            const ref = React.useRef<HTMLElement>(null);
            useHoldReaderBelow(ref, value);
            return <section ref={ref}>{value}</section>;
        };
        sectionBottom = -10;
        const view = render(<Last value={1}/>);
        sectionBottom = -10;
        view.rerender(<Last value={2}/>);
        expect(scrollBy).not.toHaveBeenCalled();
    });
});
