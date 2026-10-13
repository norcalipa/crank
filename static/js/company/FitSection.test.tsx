// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {act, cleanup, fireEvent, render, renderHook, screen, waitFor, within} from '@testing-library/react';
import * as React from 'react';

import * as store from '../workspace/store';
import {MATCH_TERMS, REQUIREMENT_MARKS} from '../labels';
import FitSection, {CompanyFit, FitRequirement, fitGroupOf, observedText} from './FitSection';
import {useOwnCompanyRead} from './useOwnCompanyRead';

const req = (overrides: Partial<FitRequirement> = {}): FitRequirement => ({
    path: 'funding_stage', status: 'match', hard: false, observed: 'a', scope_ok: true, scope: null,
    evidence_status: {state: 'verified', last_verified_at: '2026-01-01T00:00:00Z'}, ...overrides,
});

const fit = (overrides: Partial<CompanyFit> = {}): CompanyFit => ({
    organization_id: 4, has_preferences: true, fit_score: 71.25, company_score: 4.2, coverage: 0.6,
    requirements: [
        req(),
        req({path: 'work_location.max_in_office_days', status: 'mismatch', hard: true, observed: 5}),
        req({path: 'compensation.require_public_company', status: 'mismatch', hard: false, observed: false,
            evidence_status: {state: 'profile'}}),
        req({path: 'industry', status: 'unknown', observed: null, evidence_status: null}),
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
        expect(screen.getByTestId('fit-score')).toHaveTextContent('71.3 / 100');
        expect(screen.getByTestId('fit-company-score')).toHaveTextContent('4.2 / 5');
        expect(screen.getByTestId('fit-coverage')).toHaveTextContent('60%');
        const labels = within(screen.getByRole('list', {name: 'Match figures'})).getAllByRole('listitem');
        expect(labels.map(item => item.querySelector('.job-match-figure-label')?.textContent)).toEqual(
            ['Fit', 'Company score (preset)', 'Requirement coverage']);
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
            .toHaveTextContent('Not counted for you: the fact on record covers Location: US only.');
        // One sentence: no value, no status word, and the scope is not said twice.
        expect(row).toHaveTextContent(/^\S* ?Funding stageNot counted for you: the fact on record covers Location: US only\.$/);
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
            .toHaveTextContent('Not counted for you: the fact on record covers part of the company only.');
    });

    test('a requirement with no fact behind it reads "No fact on record", not "Company-wide"', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({requirements: [
            req({path: 'industry', status: 'unknown', observed: null, evidence_status: null, source_kind: null}),
            req({path: 'funding_stage', status: 'match', evidence_status: null, source_kind: 'field'}),
        ]})));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-scope-industry')).toHaveTextContent('No fact on record');
        expect(screen.getByTestId('fit-row-industry')).not.toHaveTextContent('Company-wide');
        // A field-sourced value is a fact on record, company-wide.
        expect(screen.getByTestId('fit-scope-funding_stage')).toHaveTextContent('Company-wide');
    });

    test('the disclosure explains the numbers, the marks and the status words from the shared constants', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        const legend = screen.getByTestId('fit-legend');
        expect(legend.tagName).toBe('DETAILS');
        expect(legend).not.toHaveAttribute('open');
        const text = legend.textContent || '';
        for (const term of [MATCH_TERMS.fit, MATCH_TERMS.companyScore, MATCH_TERMS.requirementCoverage,
            MATCH_TERMS.sourced, MATCH_TERMS.changed]) {
            expect(text).toContain(term.label);
            expect(text).toContain(term.meaning);
        }
        for (const mark of Object.values(REQUIREMENT_MARKS)) expect(text).toContain(mark.meaning);
        expect(text).toContain('Fit is a weighted score out of 100');
        expect(text).toContain('Coverage only says how many of your priorities could be decided');
        expect(text).toContain('A requirement is a priority you marked as a requirement');
        expect(text).toContain('Why a fact is Verified below and Sourced, not confirmed here');
        expect(within(legend).getByText('What these labels mean').tagName).toBe('SUMMARY');
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
        expect(screen.getByTestId('fit-empty-tradeoffs')).toHaveTextContent('No preference is unmet.');
        expect(screen.getByTestId('fit-empty-does-not-meet')).toHaveTextContent('No requirement is unmet.');
        expect(screen.getByTestId('fit-empty-unknown')).toHaveTextContent('Nothing is undecided.');
        global.fetch = jest.fn().mockResolvedValue(answer(fit({requirements: [req({status: 'unknown', observed: null})]})));
        cleanup();
        renderFit();
        expect(await screen.findByTestId('fit-empty-meets')).toHaveTextContent('No priority is confirmed as met.');
    });

    test('figures show a dash when a number is not known', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(fit({fit_score: null, company_score: null, coverage: null})));
        renderFit();
        await screen.findByTestId('fit-ready');
        expect(screen.getByTestId('fit-score')).toHaveTextContent('—');
        expect(screen.getByTestId('fit-company-score')).toHaveTextContent('—');
        expect(screen.getByTestId('fit-coverage')).toHaveTextContent('—');
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
    const second = () => fit({fit_score: 33.5, requirements: [
        req({path: 'industry', status: 'match', observed: 'software'})]});
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
        expect(screen.getByTestId('fit-score')).toHaveTextContent('71.3 / 100');
        expect(screen.queryByTestId('fit-loading')).toBeNull();
        await act(async () => {
            release(answer(second()));
        });
        expect(screen.getByTestId('fit-score')).toHaveTextContent('33.5 / 100');
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
        expect(screen.getByTestId('fit-score')).toHaveTextContent('33.5 / 100');
    });

    test('a second change while one read is out keeps only the last answer', async () => {
        renderFit();
        await screen.findByTestId('fit-ready');
        const waiting: ((value: unknown) => void)[] = [];
        (global.fetch as jest.Mock).mockImplementation(() => new Promise(resolve => waiting.push(resolve)));
        act(() => store.setPrioritiesRevision(8));
        act(() => store.setPrioritiesRevision(9));
        await act(async () => {
            waiting[0](answer(fit({fit_score: 1})));
        });
        expect(screen.getByTestId('fit-score')).toHaveTextContent('71.3 / 100');
        await act(async () => {
            waiting[1](answer(second()));
        });
        expect(screen.getByTestId('fit-score')).toHaveTextContent('33.5 / 100');
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
        expect(screen.queryByTestId('fit-score')).toBeNull();
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
        expect(observedText(req({path: 'industry', observed: 'Software'}))).toBe('Software');
        expect(observedText(req({path: 'compensation.minimum_salary', observed: 120000}))).toBe('120000');
    });

    // The values the matching engine sends (crank/agents/jobs/matching.py), per path.
    test.each([
        ['funding_stage', 'b', 'Series B'],
        ['funding_stage', 's', 'Seed'],
        ['funding_stage', 'x', 'Series G or Later'],
        ['funding_stage', 'o', 'Other Private'],
        ['funding_stage', 'p', 'Public'],
        ['funding_stage', 'series h', 'series h'],
        ['compensation.require_public_company', 'P', 'Public'],
        ['compensation.require_public_company', 'B', 'Series B'],
        ['compensation.require_public_company', 'Private company', 'Private company'],
        ['work_location.modes', 'remote', 'Remote'],
        ['work_location.modes', 'hybrid', 'Hybrid'],
        ['work_location.modes', 'in-office', 'In-Office'],
        ['work_location.modes', 'R', 'Remote'],
        ['work_location.modes', 'flexible', 'flexible'],
        ['work_location.max_in_office_days', 0, '0 days a week in the office'],
        ['work_location.max_in_office_days', 1, '1 day a week in the office'],
        ['work_location.max_in_office_days', 3, '3 days a week in the office'],
        ['vesting.max_cliff_months', 12, '12 months'],
        ['vesting.max_vesting_months', 1, '1 month'],
        ['compensation.equity_minimum_percent', 0.5, '0.5%'],
        ['work_location.countries', 'United States', 'United States'],
        ['industry', 'software', 'software'],
    ])('%s: %p reads %p', (path, observed, words) => {
        expect(observedText(req({path, observed}))).toBe(words);
    });
});
