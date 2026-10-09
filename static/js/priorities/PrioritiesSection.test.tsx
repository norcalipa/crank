// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import * as React from 'react';
import {act, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import PrioritiesSection, {CHAT_BARS_FALLBACK_PX, PIN_TRANSCRIPT_PX, SidebarPriorities} from './PrioritiesSection';
import {prioritiesSurface, subscribeDesktop} from './surface';
import {
    getWorkspaceSnapshot, resetWorkspaceForTests, setPrioritiesEditorOpen, setPrioritiesRevision,
    setWorkspaceAccount,
} from '../workspace/store';

const field = (over: Record<string, unknown>) => ({
    path: 'compensation.minimum_salary', label: 'Minimum base salary', type: 'int', value: 0, set: false,
    supported: true, hard: false, hard_locked: false, editable: true, ...over,
});

function snapshotBody(revision: number, chips: unknown[] = [], extra: Record<string, unknown> = {}) {
    return {
        exists: chips.length > 0, revision, schema_version: 1, preferences: {},
        fields: [
            field({}),
            field({path: 'compensation.equity_liquidity_required', label: 'Equity liquidity required',
                   type: 'bool', value: false, supported: false}),
            field({path: 'work_location.modes', label: 'Work arrangement', type: 'str_list', value: [],
                   choices: ['remote', 'hybrid']}),
            field({path: 'compensation.currency', label: 'Currency', type: 'str', value: 'USD',
                   choices: ['USD', 'EUR']}),
            field({path: 'culture', label: 'Culture', type: 'str_list', value: ['kind']}),
            field({path: 'compensation.equity_minimum_percent', label: 'Minimum equity', type: 'float',
                   value: 1.5, set: true}),
            field({path: 'roles.titles', label: 'Titles', type: 'str_list', value: ['SRE'], set: true, editable: false}),
            field({path: 'priorities', label: 'P', type: 'float_map', value: {}}),
            field({path: 'work_location.max_in_office_days', label: 'In-office days', hard_locked: true, hard: true}),
        ],
        chips, unsupported_criteria: [], ...extra,
    };
}

const chip = {path: 'compensation.minimum_salary', label: 'Minimum base salary', display: '150000', hard: true, supported: true};
const chip2 = {path: 'compensation.equity_liquidity_required', label: 'Liquidity', display: 'Yes', hard: false, supported: false};

const scrollIntoView = jest.fn();
beforeAll(() => { Element.prototype.scrollIntoView = scrollIntoView; });

function json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
}

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;
function mockFetch(handlers: Record<string, Handler>) {
    const fn = jest.fn((url: string, init?: RequestInit) => {
        const key = Object.keys(handlers).find((k) => url.includes(k));
        if (!key) throw new Error(`unexpected ${url}`);
        return Promise.resolve(handlers[key](url, init));
    });
    (global as any).fetch = fn;
    return fn;
}

const proposal = {
    id: 'p1', scope: 'account', change_count: 1, base_revision: 2, unsupported_criteria: [],
    changes: [{path: 'compensation.minimum_salary', old: 0, new: 150000}],
    token: {patch: {set: {'compensation.minimum_salary': 150000}}, scope: 'account', base_revision: 2},
};

beforeEach(() => {
    resetWorkspaceForTests();
    document.cookie = 'csrftoken=tok';
});

async function openEditor(variant: 'main' | 'sidebar' = 'main') {
    fireEvent.click(await screen.findByRole('button', {name: /Edit priorities|Add priorities/}));
    expect(getWorkspaceSnapshot().prioritiesEditorOpenIn).toBe(variant);
    return screen.findByRole('form', {name: 'Edit priorities'});
}

describe('PrioritiesSection', () => {
    test('signed out says why to sign in (main) or renders nothing (sidebar), and fetches nothing', () => {
        const f = mockFetch({});
        const {container, rerender} = render(<PrioritiesSection variant="main" authenticated={false}/>);
        expect(screen.getByTestId('priorities-main')).toHaveTextContent('Sign in to save your priorities.');
        expect(screen.queryByRole('button')).not.toBeInTheDocument();
        rerender(<PrioritiesSection variant="sidebar" authenticated={false}/>);
        expect(container).toBeEmptyDOMElement();
        expect(f).not.toHaveBeenCalled();
    });

    test('loading skeleton, then populated chips with non-color cues', async () => {
        let release: (r: Response) => void = () => undefined;
        mockFetch({'/api/agent/preferences/': () => new Promise<Response>((r) => { release = r; }) as any});
        render(<PrioritiesSection variant="main" authenticated/>);
        expect(screen.getByTestId('priority-chips-skeleton')).toBeInTheDocument();
        await act(async () => { release(json(snapshotBody(2, [chip, chip2]))); });
        const chips = await screen.findAllByTestId('priority-chip');
        expect(chips).toHaveLength(2);
        expect(chips[0]).toHaveTextContent('$150,000');
        expect(chips[0]).toHaveTextContent('requirement');
        expect(chips[1]).toHaveTextContent('preference, not used for matching yet');
        expect(screen.getByRole('button', {name: 'Reset priorities'})).toBeInTheDocument();
        // The accessible name carries the verb, the value and the status.
        const edit = screen.getByRole('button', {name: 'Edit Minimum base salary: $150,000 , requirement'});
        expect(screen.getByRole('button', {name: 'Edit Liquidity: Yes , preference, not used for matching yet'})).toBeInTheDocument();
        fireEvent.click(edit);
        expect(getWorkspaceSnapshot().prioritiesEditorOpenIn).toBe('main');
    });

    test('empty state offers a primary action', async () => {
        mockFetch({'/api/agent/preferences/': () => json(snapshotBody(0))});
        render(<PrioritiesSection variant="main" authenticated/>);
        expect(await screen.findByTestId('priorities-empty')).toBeInTheDocument();
        expect(screen.getByRole('button', {name: 'Add priorities'})).toHaveClass('btn-outline-primary');
        expect(screen.queryByRole('button', {name: 'Reset priorities'})).not.toBeInTheDocument();
    });

    test('load error offers Try again, then recovers', async () => {
        let ok = false;
        mockFetch({'/api/agent/preferences/': () => (ok ? json(snapshotBody(1, [chip])) : json({error: {message: 'boom'}}, 500))});
        render(<PrioritiesSection variant="main" authenticated/>);
        expect(await screen.findByTestId('priorities-load-error')).toHaveTextContent('boom');
        expect(screen.getByTestId('priorities-load-error')).toHaveTextContent('Couldn’t load your priorities.');
        expect(screen.getByTestId('priorities-load-error').textContent).not.toMatch(/\\u[0-9a-f]{4}/i);
        ok = true;
        fireEvent.click(screen.getByRole('button', {name: 'Try again'}));
        expect(await screen.findAllByTestId('priority-chip')).toHaveLength(1);
    });

    test('network failure and non-JSON error body use generic copy', async () => {
        (global as any).fetch = jest.fn(() => Promise.reject(new Error('down')));
        const {unmount} = render(<PrioritiesSection variant="main" authenticated/>);
        expect(await screen.findByTestId('priorities-load-error')).toHaveTextContent('Could not reach the server');
        unmount();
        (global as any).fetch = jest.fn(() => Promise.resolve(new Response('<html>', {status: 502})));
        render(<PrioritiesSection variant="main" authenticated/>);
        expect(await screen.findByTestId('priorities-load-error')).toHaveTextContent('Couldn’t load your priorities.');
        expect(screen.getByTestId('priorities-load-error')).not.toHaveTextContent('Something went wrong');
    });

    test('a read that settles after unmount is ignored (success and failure)', async () => {
        let resolveOk!: (r: Response) => void;
        let rejectLater!: (e: Error) => void;
        const pending = [
            new Promise<Response>((res) => { resolveOk = res; }),
            new Promise<Response>((_res, rej) => { rejectLater = rej; }),
        ];
        let call = 0;
        (global as any).fetch = jest.fn(() => pending[call++]);
        const first = render(<PrioritiesSection variant="main" authenticated/>);
        first.unmount();
        const second = render(<PrioritiesSection variant="main" authenticated/>);
        second.unmount();
        await act(async () => {
            resolveOk(json(snapshotBody(1, [chip])));
            rejectLater(new Error('late'));
        });
        expect(screen.queryByRole('alert')).toBeNull();
    });

    test('nothing takes focus on mount', async () => {
        mockFetch({'/api/agent/preferences/': () => json(snapshotBody(1, [chip]))});
        render(<PrioritiesSection variant="main" authenticated/>);
        await screen.findAllByTestId('priority-chip');
        expect(document.body).toHaveFocus();
    });

    test('edit → review → apply → undo happy path signals the store revision', async () => {
        let revision = 2;
        const fetchFn = mockFetch({
            '/api/agent/preferences/propose/': () => json(proposal),
            '/api/agent/preferences/apply/': () => { revision = 3; return json({scope: 'account', revision: 3,
                changes: proposal.changes, undo: {expected_revision: 3, document: {}}}); },
            '/api/agent/preferences/undo/': () => { revision = 4; return json({revision: 4}); },
            '/api/agent/preferences/': () => json(snapshotBody(revision, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        expect(screen.getByRole('button', {name: 'Review changes'})).toBeDisabled();
        fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
        expect(within(form).getByText('Not used for matching yet (', {exact: false})).toBeInTheDocument();
        expect(within(form).getByText('Always a requirement')).toBeInTheDocument();
        fireEvent.click(within(form).getAllByRole('button', {name: 'Requirement'})[0]);
        fireEvent.click(within(form).getAllByRole('button', {name: 'Preference'})[0]);
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        const heading = await screen.findByRole('heading', {name: 'Review your changes'});
        expect(heading).toHaveFocus();
        fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
        const summary = await screen.findByRole('heading', {name: /Priorities saved/});
        expect(summary).toHaveFocus();
        expect(getWorkspaceSnapshot().prioritiesRevision).toBe(3);
        expect(getWorkspaceSnapshot().prioritiesEditorOpenIn).toBeNull();
        const applyCall = fetchFn.mock.calls.find((c) => String(c[0]).includes('/apply/'))!;
        expect((applyCall[1]!.headers as Record<string, string>)['X-CSRFToken']).toBe('tok');
        expect(JSON.parse(applyCall[1]!.body as string)).toEqual({proposal: proposal.token, decision: 'apply'});
        fireEvent.click(screen.getByRole('button', {name: 'Undo'}));
        expect(await screen.findByRole('heading', {name: 'Change undone.'})).toBeInTheDocument();
        expect(getWorkspaceSnapshot().prioritiesRevision).toBe(4);
        fireEvent.click(screen.getByRole('button', {name: 'Done'}));
        expect(await screen.findByRole('button', {name: 'Edit priorities'})).toBeInTheDocument();
    });

    test('invalid value keeps the edits and shows inline errors; Cancel closes the editor', async () => {
        mockFetch({
            '/api/agent/preferences/propose/': () => json({error: {type: 'invalid_request', message: 'bad',
                field_errors: {'compensation.minimum_salary': ['Must be a whole number.']}}}, 400),
            '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        const input = within(form).getByLabelText('Minimum base salary');
        fireEvent.change(input, {target: {value: '12'}});
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        expect(await screen.findByText('Must be a whole number.')).toBeInTheDocument();
        expect(screen.getByTestId('priorities-form-error')).toHaveTextContent('Fix 1 field: Minimum base salary');
        await waitFor(() => expect(screen.getByLabelText('Minimum base salary')).toHaveFocus());
        expect(scrollIntoView).toHaveBeenCalled();
        expect(screen.getByLabelText('Minimum base salary')).toHaveValue(12);
        expect(screen.getByLabelText('Minimum base salary')).toHaveAttribute('aria-invalid', 'true');
        fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
        expect(await screen.findByRole('button', {name: 'Edit priorities'})).toBeInTheDocument();
        expect(getWorkspaceSnapshot().prioritiesEditorOpenIn).toBeNull();
    });

    test('a field error names the field in the footer summary', async () => {
        mockFetch({
            '/api/agent/preferences/propose/': () => json({error: {type: 'invalid_request', message: 'bad',
                field_errors: {'compensation.minimum_salary': ['Nope.']}}}, 400),
            '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '5'}});
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        expect(await screen.findByTestId('priorities-form-error')).toHaveTextContent('Fix 1 field: Minimum base salary');
    });

    test('review step announces its change count and uses editor labels', async () => {
        mockFetch({
            '/api/agent/preferences/propose/': () => json(proposal),
            '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        await screen.findByRole('heading', {name: 'Review your changes'});
        expect(screen.getByText('1 change to review')).toHaveAttribute('role', 'status');
        expect(screen.getByText('Compensation › Minimum base salary', {exact: false})).toBeInTheDocument();
    });

    test('a propose failure never shows the server wording for a 400; a 500 message and a network error do', async () => {
        let mode: 'msg' | 'srv' | 'net' = 'msg';
        mockFetch({
            '/api/agent/preferences/propose/': () => {
                if (mode === 'net') throw new Error('x');
                if (mode === 'srv') return json({error: {message: 'Busy right now'}}, 500);
                return json({error: {message: "'remove' for list field 'exclusions.locations' must list items"}}, 400);
            },
            '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
        });
        (global as any).fetch = ((orig) => (url: string, init?: RequestInit) => {
            if (mode === 'net' && url.includes('/propose/')) return Promise.reject(new Error('x'));
            return orig(url, init);
        })((global as any).fetch);
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '5'}});
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        expect(await screen.findByText(/These changes could not be checked/)).toBeInTheDocument();
        expect(screen.queryByText(/must list items/)).not.toBeInTheDocument();
        mode = 'srv';
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        expect(await screen.findByText('Busy right now')).toBeInTheDocument();
        mode = 'net';
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        expect(await screen.findByText(/Could not reach the server/)).toBeInTheDocument();
    });

    test('choices, selects, lists, booleans and read-only fields render and edit', async () => {
        const fetchFn = mockFetch({
            '/api/agent/preferences/propose/': () => json(proposal),
            '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
        });
        render(<PrioritiesSection variant="sidebar" authenticated/>);
        const form = await openEditor('sidebar');
        fireEvent.click(within(form).getByLabelText('Hybrid'));
        fireEvent.click(within(form).getByLabelText('Hybrid'));
        fireEvent.click(within(form).getByLabelText('Remote'));
        fireEvent.change(within(form).getByLabelText('Currency'), {target: {value: 'EUR'}});
        const culture = within(form).getByLabelText('Culture');
        fireEvent.change(culture, {target: {value: 'curious, open'}});
        fireEvent.keyDown(culture, {key: 'Enter'});
        fireEvent.change(within(form).getByLabelText('Minimum equity'), {target: {value: '2.5'}});
        expect(within(form).getByText('curious, open')).toBeInTheDocument();
        fireEvent.submit(form);
        await screen.findByRole('heading', {name: 'Review your changes'});
        const body = JSON.parse(fetchFn.mock.calls.find((c) => String(c[0]).includes('/propose/'))![1]!.body as string);
        expect(body.scope).toBe('account');
        expect(body.patch.set).toMatchObject({
            'work_location.modes': ['remote'], 'compensation.currency': 'EUR',
            culture: ['kind', 'curious, open'], 'compensation.equity_minimum_percent': 2.5,
        });
    });

    test('Edit from review returns to the editor with edits intact', async () => {
        mockFetch({
            '/api/agent/preferences/propose/': () => json(proposal),
            '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        await screen.findByRole('heading', {name: 'Review your changes'});
        fireEvent.click(screen.getByRole('button', {name: 'Edit'}));
        expect(await screen.findByLabelText('Minimum base salary')).toHaveValue(150000);
    });

    test('stale apply (409) says so and Review latest re-proposes against the latest revision', async () => {
        let applies = 0;
        let proposals = 0;
        mockFetch({
            '/api/agent/preferences/propose/': () => { proposals += 1; return json(proposal); },
            '/api/agent/preferences/apply/': () => { applies += 1; return json({error: {type: 'preference_stale',
                message: 'stale', current_revision: 5}}, 409); },
            '/api/agent/preferences/': () => json(snapshotBody(5, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        await screen.findByRole('heading', {name: 'Review your changes'});
        fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
        expect(await screen.findByTestId('priorities-review-error')).toHaveTextContent('Your priorities changed elsewhere');
        expect(applies).toBe(1);
        expect(getWorkspaceSnapshot().prioritiesRevision).toBeNull();
        fireEvent.click(screen.getByRole('button', {name: 'Review latest'}));
        await waitFor(() => expect(proposals).toBe(2));
        expect(await screen.findByRole('button', {name: 'Apply to account'})).toBeInTheDocument();
    });

    test('forbidden apply (403) is treated as expired: review latest, no retry loop', async () => {
        let proposals = 0;
        mockFetch({
            '/api/agent/preferences/propose/': () => { proposals += 1; return json(proposal); },
            '/api/agent/preferences/apply/': () => json({error: {type: 'forbidden',
                message: 'This change expired. Reload to review it again.'}}, 403),
            '/api/agent/preferences/': () => json(snapshotBody(5, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        await screen.findByRole('heading', {name: 'Review your changes'});
        fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
        expect(await screen.findByTestId('priorities-review-error')).toHaveTextContent('Review the latest before applying');
        fireEvent.click(screen.getByRole('button', {name: 'Review latest'}));
        await waitFor(() => expect(proposals).toBe(2));
    });

    test('apply network error is retryable; This search only applies without saving', async () => {
        let attempt = 0;
        const fetchFn = mockFetch({
            '/api/agent/preferences/propose/': () => json(proposal),
            '/api/agent/preferences/apply/': (_u, init) => {
                attempt += 1;
                const body = JSON.parse(init!.body as string);
                if (attempt === 1) return json({error: {message: 'try later'}}, 503);
                return json({scope: body.proposal.scope, matches: {job_matches: [1, 2], organization_matches: [3, 4, 5]}});
            },
            '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        await screen.findByRole('heading', {name: 'Review your changes'});
        fireEvent.click(screen.getByRole('button', {name: 'This search only'}));
        expect(await screen.findByText('try later')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', {name: 'This search only'}));
        expect(await screen.findByRole('heading', {name: /this search only \u2014 not saved\. 2 jobs match these priorities\. The list below still reflects your saved priorities\./i})).toBeInTheDocument();
        expect(getWorkspaceSnapshot().prioritiesRevision).toBeNull();
        expect(screen.queryByRole('button', {name: 'Undo'})).not.toBeInTheDocument();
        expect(fetchFn).toHaveBeenCalled();
    });

    test('undo failures: stale is explained, other errors are retryable', async () => {
        let undos = 0;
        mockFetch({
            '/api/agent/preferences/propose/': () => json(proposal),
            '/api/agent/preferences/apply/': () => json({scope: 'account', revision: 3, changes: proposal.changes,
                undo: {expected_revision: 3, document: {}}}),
            '/api/agent/preferences/undo/': () => {
                undos += 1;
                return undos === 1
                    ? json({error: {type: 'preference_stale', message: 's'}}, 409)
                    : json({error: {message: 'nope'}}, 500);
            },
            '/api/agent/preferences/': () => json(snapshotBody(3, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        await screen.findByRole('heading', {name: 'Review your changes'});
        fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
        await screen.findByRole('heading', {name: /Priorities saved/});
        fireEvent.click(screen.getByRole('button', {name: 'Undo'}));
        expect(await screen.findByTestId('priorities-undo-error')).toHaveTextContent('can no longer be undone');
        fireEvent.click(screen.getByRole('button', {name: 'Undo'}));
        await waitFor(() => expect(screen.getByTestId('priorities-undo-error')).toHaveTextContent('nope'));
    });

    test('Reset priorities confirms with the contract copy, resets, and offers Undo', async () => {
        const fetchFn = mockFetch({
            '/api/agent/preferences/reset/': () => json({reset: true, revision: 6,
                changes: [{path: 'compensation.minimum_salary', old: 150000, new: 0}],
                undo: {expected_revision: 6, document: {}}}),
            '/api/agent/preferences/': () => json(snapshotBody(5, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        fireEvent.click(await screen.findByRole('button', {name: 'Reset priorities'}));
        expect(screen.getByText(
            'Reset all saved priorities to their defaults? Your conversations are not changed. Job matches will update.',
        )).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', {name: 'Keep priorities'}));
        expect(await screen.findByRole('button', {name: 'Edit priorities'})).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', {name: 'Reset priorities'}));
        fireEvent.click(screen.getByRole('button', {name: 'Reset priorities'}));
        expect(await screen.findByRole('heading', {name: /Priorities reset to defaults/})).toBeInTheDocument();
        const call = fetchFn.mock.calls.find((c) => String(c[0]).includes('/reset/'))!;
        expect(JSON.parse(call[1]!.body as string)).toEqual({expected_revision: 5});
        expect(getWorkspaceSnapshot().prioritiesRevision).toBe(6);
        expect(screen.getByRole('button', {name: 'Undo'})).toBeInTheDocument();
    });

    test('Reset conflict (409) and failure surface inline errors', async () => {
        let code = 409;
        mockFetch({
            '/api/agent/preferences/reset/': () => json({error: {type: code === 409 ? 'preference_stale' : 'server', message: 's'}}, code),
            '/api/agent/preferences/': () => json(snapshotBody(5, [chip])),
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        fireEvent.click(await screen.findByRole('button', {name: 'Reset priorities'}));
        fireEvent.click(screen.getByRole('button', {name: 'Reset priorities'}));
        expect(await screen.findByTestId('priorities-view-error')).toHaveTextContent('changed elsewhere');
        code = 500;
        fireEvent.click(screen.getByRole('button', {name: 'Reset priorities'}));
        fireEvent.click(screen.getByRole('button', {name: 'Reset priorities'}));
        await waitFor(() => expect(screen.getByTestId('priorities-view-error')).not.toHaveTextContent('changed elsewhere'));
    });

    test('an external revision refetches once; opening the editor elsewhere closes this one', async () => {
        let revision = 1;
        const fetchFn = mockFetch({'/api/agent/preferences/': () => json(snapshotBody(revision, [chip]))});
        render(<PrioritiesSection variant="main" authenticated/>);
        await screen.findAllByTestId('priority-chip');
        const loads = () => fetchFn.mock.calls.length;
        const before = loads();
        revision = 7;
        act(() => setPrioritiesRevision(7));
        await waitFor(() => expect(loads()).toBe(before + 1));
        act(() => setPrioritiesRevision(7));
        expect(loads()).toBe(before + 1);
        act(() => setPrioritiesEditorOpen('main'));
        await screen.findByRole('form', {name: 'Edit priorities'});
        act(() => setPrioritiesEditorOpen('sidebar'));
        await waitFor(() => expect(screen.queryByRole('form', {name: 'Edit priorities'})).not.toBeInTheDocument());
    });

    test('a supported yes/no field shows no importance toggle until it is set to Yes', async () => {
        mockFetch({
            '/api/agent/preferences/': () => {
                const body = snapshotBody(2, [chip]);
                body.fields.push(field({path: 'compensation.accelerated_vesting', label: 'Prefer accelerated vesting',
                                        type: 'bool', value: false}));
                return json(body);
            },
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        const row = within(form).getByLabelText('Prefer accelerated vesting').closest('[data-testid="priorities-field"]') as HTMLElement;
        expect(within(row).queryByRole('group', {name: /importance/})).not.toBeInTheDocument();
        fireEvent.change(within(form).getByLabelText('Prefer accelerated vesting'), {target: {value: 'false'}});
        expect(within(row).queryByRole('group', {name: /importance/})).not.toBeInTheDocument();
        fireEvent.change(within(form).getByLabelText('Prefer accelerated vesting'), {target: {value: 'true'}});
        expect(within(row).getByRole('group', {name: /importance/})).toBeInTheDocument();
    });

    test('editor maps server messages to field labels, hides importance on empty fields, edits unsupported fields and clears read-only ones', async () => {
        mockFetch({
            '/api/agent/preferences/propose/': () => json({error: {type: 'invalid_request', message: 'bad',
                field_errors: {
                    'compensation.minimum_salary': ["Field 'compensation.minimum_salary' must be non-negative"],
                    'compensation.equity_minimum_percent': ["Field 'compensation.equity_minimum_percent' is required"],
                }}}, 400),
            '/api/agent/preferences/': () => {
                const body = snapshotBody(2, [chip]);
                body.fields.push(field({path: 'compensation.minimum_total_compensation', label: 'Minimum total compensation',
                                        type: 'int', value: 250000, set: true, supported: false}));
                return json(body);
            },
        });
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        const salaryRow = within(form).getByLabelText('Minimum base salary').closest('[data-testid="priorities-field"]') as HTMLElement;
        expect(within(salaryRow).queryByRole('group', {name: /importance/})).not.toBeInTheDocument();
        fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '-5'}});
        expect(within(salaryRow).getByRole('group', {name: /importance/})).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
        expect(await screen.findByText('Minimum base salary can\u2019t be negative.')).toBeInTheDocument();
        expect(screen.getByText('Minimum equity is required')).toBeInTheDocument();
        const group = within(form).getByText('Not used for matching yet (', {exact: false}).closest('details') as HTMLElement;
        // Unsupported criteria the server marks editable are edited here too, with no chat dependency;
        // the rest are read-only but can be cleared.
        expect(within(group).getByLabelText('Minimum total compensation')).toHaveValue(250000);
        expect(within(group).getByLabelText('Equity liquidity required')).toHaveValue('');
        expect(group.querySelector('#priority-main-roles-titles')).toHaveTextContent('SRE');
        expect(group.querySelector('#priority-main-roles-titles')).toHaveClass('is-set');
        fireEvent.click(within(group).getByRole('button', {name: 'Clear Titles'}));
        expect(group.querySelector('#priority-main-roles-titles')).toHaveTextContent('Will be cleared');
        fireEvent.click(within(group).getByRole('button', {name: 'Keep Titles'}));
        expect(group.querySelector('#priority-main-roles-titles')).toHaveTextContent('SRE');
    });

    test('scrolling the editor marks the scroll region', async () => {
        mockFetch({'/api/agent/preferences/': () => json(snapshotBody(2, [chip]))});
        render(<PrioritiesSection variant="main" authenticated/>);
        const form = await openEditor();
        const region = form.closest('.priorities-scroll') as HTMLElement;
        expect(region).not.toHaveClass('is-scrolled');
        region.scrollTop = 20;
        fireEvent.scroll(region);
        expect(region).toHaveClass('is-scrolled');
        region.scrollTop = 0;
        fireEvent.scroll(region);
        expect(region).not.toHaveClass('is-scrolled');
    });

    test('reset confirmation has its own heading', async () => {
        mockFetch({'/api/agent/preferences/': () => json(snapshotBody(2, [chip]))});
        render(<PrioritiesSection variant="main" authenticated/>);
        fireEvent.click(await screen.findByRole('button', {name: 'Reset priorities'}));
        expect(screen.getByText('Reset priorities?')).toBeInTheDocument();
    });

    test('account switch and private-state purge drop the shown priorities', async () => {
        let body = snapshotBody(1, [chip]);
        mockFetch({'/api/agent/preferences/': () => json(body)});
        render(<PrioritiesSection variant="main" authenticated/>);
        await screen.findAllByTestId('priority-chip');
        body = snapshotBody(1, []);
        act(() => setWorkspaceAccount({status: 'authenticated', key: 'someone-else'}));
        await screen.findByTestId('priorities-empty');
        expect(screen.queryByTestId('priority-chip')).not.toBeInTheDocument();
        let release: (r: Response) => void = () => undefined;
        (global as any).fetch = jest.fn(() => new Promise((r) => { release = r; }));
        act(() => { document.dispatchEvent(new Event('crank:private-state-purged')); });
        expect(screen.getByTestId('priority-chips-skeleton')).toBeInTheDocument();
        release(json(snapshotBody(1, [chip])));
    });

    test('SidebarPriorities yields to the main block on desktop', async () => {
        mockFetch({'/api/agent/preferences/': () => json(snapshotBody(1, [chip]))});
        const main = document.createElement('div');
        main.id = 'priorities-main';
        document.body.appendChild(main);
        let matches = true;
        const listeners = new Set<() => void>();
        window.matchMedia = jest.fn(() => ({
            get matches() { return matches; },
            addEventListener: (_: string, fn: () => void) => listeners.add(fn),
            removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
        })) as unknown as typeof window.matchMedia;
        try {
            act(() => setWorkspaceAccount({status: 'authenticated', key: 'u'}));
            const {container, unmount} = render(<SidebarPriorities/>);
            expect(container).toBeEmptyDOMElement();
            expect(prioritiesSurface()).toBe('main');
            matches = false;
            act(() => listeners.forEach((fn) => fn()));
            expect(await screen.findByTestId('priorities-summary')).toHaveTextContent('Requires: $150,000');
            expect(prioritiesSurface()).toBe('sidebar');
            unmount();
            expect(listeners.size).toBe(0);
        } finally {
            main.remove();
            delete (window as {matchMedia?: unknown}).matchMedia;
        }
    });

    test('surface falls back to the sidebar without matchMedia or a main block', () => {
        expect(prioritiesSurface()).toBe('sidebar');
        const main = document.createElement('div');
        main.id = 'priorities-main';
        document.body.appendChild(main);
        expect(prioritiesSurface()).toBe('sidebar');
        const stop = subscribeDesktop(() => undefined);
        stop();
        main.remove();
    });

    test('SidebarPriorities follows the store account and stays hidden until authenticated', async () => {
        mockFetch({'/api/agent/preferences/': () => json(snapshotBody(1, [chip]))});
        const {container} = render(<SidebarPriorities/>);
        expect(container).toBeEmptyDOMElement();
        act(() => setWorkspaceAccount({status: 'authenticated', key: 'u'}));
        expect(await screen.findByTestId('priorities-summary')).toHaveTextContent('Requires: $150,000');
        act(() => setWorkspaceAccount({status: 'anonymous', key: ''}));
        await waitFor(() => expect(container).toBeEmptyDOMElement());
    });

    describe('review round 1 (issue #480)', () => {
        const applyOk = (changes = proposal.changes) => json({scope: 'account', revision: 3, changes,
            undo: {expected_revision: 3, document: {}}});

        test('an in-flight apply is dropped on an account switch: no diff, no Undo, no revision signal', async () => {
            let release: (r: Response) => void = () => undefined;
            mockFetch({
                '/api/agent/preferences/propose/': () => json(proposal),
                '/api/agent/preferences/apply/': () => new Promise<Response>((r) => { release = r; }) as any,
                '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
            });
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await screen.findByRole('heading', {name: 'Review your changes'});
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            act(() => setWorkspaceAccount({status: 'authenticated', key: 'someone-else'}));
            await act(async () => { release(applyOk()); });
            expect(screen.queryByRole('button', {name: 'Undo'})).not.toBeInTheDocument();
            expect(screen.queryByRole('heading', {name: /Priorities saved/})).not.toBeInTheDocument();
            expect(getWorkspaceSnapshot().prioritiesRevision).toBeNull();
        });

        test('Review latest rebases onto the fresh document and names what changed elsewhere', async () => {
            const bodies: any[] = [];
            let current = snapshotBody(2, [chip]);
            mockFetch({
                '/api/agent/preferences/propose/': (_u, init) => { bodies.push(JSON.parse(init!.body as string)); return json(proposal); },
                '/api/agent/preferences/apply/': () => json({error: {type: 'preference_stale', message: 's', current_revision: 5}}, 409),
                '/api/agent/preferences/': () => json(current),
            });
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
            const salaryRow = within(form).getByLabelText('Minimum base salary').closest('[data-testid="priorities-field"]') as HTMLElement;
            fireEvent.click(within(salaryRow).getByRole('button', {name: 'Requirement'}));
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await screen.findByRole('heading', {name: 'Review your changes'});
            const first = bodies.length;
            current = snapshotBody(5, [chip], {preferences: {importance: {culture: 1}}});
            current.fields[0] = field({value: 200000, set: true});
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            await screen.findByTestId('priorities-review-error');
            fireEvent.click(screen.getByRole('button', {name: 'Review latest'}));
            await waitFor(() => expect(bodies.length).toBe(first + 1));
            // The rebased patch is built against the fresh document: the other tab's requirement is kept.
            expect(bodies[first].patch.set.importance).toEqual({culture: 1, 'compensation.minimum_salary': 1});
            expect(bodies[first].patch.set['compensation.minimum_salary']).toBe(150000);
            expect(await screen.findByTestId('priorities-review-conflicts')).toHaveTextContent('Minimum base salary');
        });

        test('the list editor keeps commas inside one entry and saves exactly what the review shows', async () => {
            const bodies: any[] = [];
            mockFetch({
                '/api/agent/preferences/propose/': (_u, init) => { bodies.push(JSON.parse(init!.body as string)); return json(proposal); },
                '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
            });
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            const culture = within(form).getByLabelText('Culture');
            fireEvent.change(culture, {target: {value: 'San Francisco, CA'}});
            fireEvent.click(within(form).getByRole('button', {name: /^Add/}));
            expect(within(form).getByText('San Francisco, CA')).toBeInTheDocument();
            expect(within(form).queryByText('CA')).not.toBeInTheDocument();
            fireEvent.click(within(form).getByRole('button', {name: /Remove kind/}));
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await waitFor(() => expect(bodies.length).toBe(1));
            expect(JSON.stringify(bodies[0])).toContain('"San Francisco, CA"');
        });

        test('chip names carry value and status; float_map chips are not buttons; currency shows', async () => {
            mockFetch({'/api/agent/preferences/': () => json(snapshotBody(2, [
                {...chip, display: '150000'},
                {path: 'priorities', label: 'Priority weights', display: '2 set', hard: false, supported: true},
            ], {preferences: {compensation: {currency: 'EUR'}}}))});
            render(<PrioritiesSection variant="main" authenticated/>);
            await screen.findAllByTestId('priority-chip');
            expect(screen.queryByRole('button', {name: /Priority weights/})).not.toBeInTheDocument();
            expect(screen.getByText(/Priority weights/)).toBeInTheDocument();
        });

        test('focus moves sensibly after Edit, Cancel, Reset and Keep', async () => {
            mockFetch({'/api/agent/preferences/': () => json(snapshotBody(2, [chip]))});
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            expect(form).toHaveFocus();
            fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
            const edit = await screen.findByRole('button', {name: 'Edit priorities'});
            await waitFor(() => expect(edit).toHaveFocus());
            fireEvent.click(screen.getByRole('button', {name: 'Reset priorities'}));
            expect(screen.getByText('Reset priorities?')).toHaveFocus();
            fireEvent.click(screen.getByRole('button', {name: 'Keep priorities'}));
            const reset = await screen.findByRole('button', {name: 'Reset priorities'});
            await waitFor(() => expect(reset).toHaveFocus());
        });

        test('an expired session offers sign-in instead of a generic retry', async () => {
            const redirected = new Response('<html>login</html>', {status: 200, headers: {'Content-Type': 'text/html'}});
            Object.defineProperty(redirected, 'redirected', {value: true});
            mockFetch({'/api/agent/preferences/': () => redirected});
            render(<PrioritiesSection variant="main" authenticated/>);
            const banner = await screen.findByTestId('priorities-session-expired');
            expect(within(banner).getByRole('link', {name: /sign in/i})).toBeInTheDocument();
            expect(screen.queryByRole('button', {name: /try again/i})).not.toBeInTheDocument();
        });

        test('This search only counts jobs only and marks a capped count', async () => {
            mockFetch({
                '/api/agent/preferences/propose/': () => json(proposal),
                '/api/agent/preferences/apply/': () => json({scope: 'search', matches: {
                    job_matches: new Array(25).fill(1), organization_matches: [1, 2, 3]}}),
                '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
            });
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await screen.findByRole('heading', {name: 'Review your changes'});
            fireEvent.click(screen.getByRole('button', {name: 'This search only'}));
            expect(await screen.findByText(/25\+ jobs match these priorities/)).toBeInTheDocument();
        });

        test('Add button commits on mouse down without stealing focus', async () => {
            mockFetch({'/api/agent/preferences/': () => json(snapshotBody(2, [chip]))});
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Culture'), {target: {value: 'open'}});
            const add = within(form).getByRole('button', {name: /^Add/});
            expect(fireEvent.mouseDown(add)).toBe(false);
            fireEvent.click(add);
            expect(within(form).getByText('open')).toBeInTheDocument();
        });

        test('a 200 that is not JSON is treated as an expired session', async () => {
            mockFetch({'/api/agent/preferences/': () => new Response('<html>login</html>', {status: 200})});
            render(<PrioritiesSection variant="main" authenticated/>);
            expect(await screen.findByTestId('priorities-session-expired')).toBeInTheDocument();
        });

        test('Review latest says so when the latest document already holds the edit', async () => {
            let current = snapshotBody(2, [chip]);
            mockFetch({
                '/api/agent/preferences/propose/': () => json(proposal),
                '/api/agent/preferences/apply/': () => json({error: {type: 'preference_stale', message: 's', current_revision: 5}}, 409),
                '/api/agent/preferences/': () => json(current),
            });
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await screen.findByRole('heading', {name: 'Review your changes'});
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            await screen.findByTestId('priorities-review-error');
            current = snapshotBody(5, [chip]);
            current.fields[0] = field({value: 150000, set: true});
            fireEvent.click(screen.getByRole('button', {name: 'Review latest'}));
            expect(await screen.findByText(/Nothing left to change/)).toBeInTheDocument();
        });

        test('Review latest keeps the review and says so when the reload fails', async () => {
            let reload = false;
            mockFetch({
                '/api/agent/preferences/propose/': () => json(proposal),
                '/api/agent/preferences/apply/': () => json({error: {type: 'preference_stale', message: 's', current_revision: 5}}, 409),
                '/api/agent/preferences/': () => (reload ? json({error: {message: 'down'}}, 500) : json(snapshotBody(2, [chip]))),
            });
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await screen.findByRole('heading', {name: 'Review your changes'});
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            await screen.findByTestId('priorities-review-error');
            reload = true;
            fireEvent.click(screen.getByRole('button', {name: 'Review latest'}));
            expect(await screen.findByText(/Could not load your latest priorities/)).toBeInTheDocument();
        });
    });

    describe('review round 2 (issue #480)', () => {
        const sentBodies = () => {
            const bodies: any[] = [];
            const fn = mockFetch({
                '/api/agent/preferences/propose/': (_u, init) => { bodies.push(JSON.parse(init!.body as string)); return json(proposal); },
                '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
            });
            return {bodies, fn};
        };

        test('emptying a list, or clearing a read-only list, is sent as a set of [] the server accepts', async () => {
            const {bodies} = sentBodies();
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.click(within(form).getByRole('button', {name: /Remove kind/}));
            const group = within(form).getByText('Not used for matching yet (', {exact: false}).closest('details') as HTMLElement;
            fireEvent.click(within(group).getByRole('button', {name: 'Clear Titles'}));
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await waitFor(() => expect(bodies.length).toBe(1));
            expect(bodies[0].patch).toEqual({set: {culture: [], 'roles.titles': []}});
        });

        test('removing an entry hands focus to the next entry, then the input; Add stays focusable', async () => {
            sentBodies();
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            const culture = within(form).getByLabelText('Culture');
            fireEvent.change(culture, {target: {value: 'open'}});
            fireEvent.click(within(form).getByRole('button', {name: /^Add/}));
            const add = within(form).getByRole('button', {name: /^Add/});
            expect(add).toHaveAttribute('aria-disabled', 'true');
            expect(add).not.toBeDisabled();
            fireEvent.click(within(form).getByRole('button', {name: /Remove kind/}));
            await waitFor(() => expect(within(form).getByRole('button', {name: /Remove open/})).toHaveFocus());
            fireEvent.click(within(form).getByRole('button', {name: /Remove open/}));
            await waitFor(() => expect(within(form).getByLabelText('Culture')).toHaveFocus());
        });

        test('Keep after Clear leaves the group open and keeps focus on the button', async () => {
            sentBodies();
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            const group = within(form).getByText('Not used for matching yet (', {exact: false}).closest('details') as HTMLDetailsElement;
            group.open = true;
            const clear = within(group).getByRole('button', {name: 'Clear Titles'});
            clear.focus();
            fireEvent.click(clear);
            expect(group.open).toBe(true);
            const keep = within(group).getByRole('button', {name: 'Keep Titles'});
            keep.focus();
            fireEvent.click(keep);
            expect(group.open).toBe(true);
            expect(within(group).getByRole('button', {name: 'Clear Titles'})).toBeInTheDocument();
            expect(group.contains(document.activeElement)).toBe(true);
        });

        test('an Edit seed shows a one-item removal as the rest of the list and a proposed read-only value as the new value', async () => {
            const {bodies} = sentBodies();
            render(<PrioritiesSection variant="main" authenticated/>);
            await screen.findByRole('button', {name: /Edit priorities/});
            act(() => setPrioritiesEditorOpen('main', {
                remove: {culture: ['kind']},
                set: {'roles.titles': ['Staff Engineer'], culture2: 1},
            }));
            const form = await screen.findByRole('form', {name: 'Edit priorities'});
            const group = within(form).getByText('Not used for matching yet (', {exact: false}).closest('details') as HTMLDetailsElement;
            expect(group.open).toBe(true);
            expect(group.querySelector('#priority-main-roles-titles')).toHaveTextContent('Staff Engineer');
            expect(group.querySelector('#priority-main-roles-titles')).not.toHaveTextContent('Will be cleared');
            fireEvent.click(within(group).getByRole('button', {name: 'Discard the proposed Titles'}));
            expect(group.querySelector('#priority-main-roles-titles')).toHaveTextContent('SRE');
            expect(within(form).queryByRole('button', {name: /Remove kind/})).not.toBeInTheDocument();
            expect(bodies).toHaveLength(0);
        });

        test('Edit your priorities from the applied summary opens the editor, and Done does not pop it open later', async () => {
            mockFetch({
                '/api/agent/preferences/propose/': () => json(proposal),
                '/api/agent/preferences/apply/': () => json({scope: 'account', revision: 3, changes: proposal.changes, undo: null}),
                '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
            });
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await screen.findByRole('heading', {name: 'Review your changes'});
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            await screen.findByTestId('priorities-applied');
            act(() => setPrioritiesEditorOpen('main'));
            expect(await screen.findByRole('form', {name: 'Edit priorities'})).toBeInTheDocument();
            expect(screen.queryByTestId('priorities-applied')).not.toBeInTheDocument();
        });

        test('an open request is honoured from the reset prompt, and once closed nothing reopens', async () => {
            mockFetch({'/api/agent/preferences/': () => json(snapshotBody(2, [chip]))});
            render(<PrioritiesSection variant="main" authenticated/>);
            fireEvent.click(await screen.findByRole('button', {name: 'Reset priorities'}));
            act(() => setPrioritiesEditorOpen('main'));
            expect(await screen.findByRole('form', {name: 'Edit priorities'})).toBeInTheDocument();
            fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
            await screen.findByRole('button', {name: 'Edit priorities'});
            expect(screen.queryByRole('form', {name: 'Edit priorities'})).not.toBeInTheDocument();
        });

        test('a seed arriving while editing merges into the draft instead of replacing typed values', async () => {
            const {bodies} = sentBodies();
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '123456'}});
            act(() => setPrioritiesEditorOpen('main', {set: {'compensation.currency': 'EUR'}}));
            await waitFor(() => expect(within(screen.getByRole('form', {name: 'Edit priorities'})).getByLabelText('Currency')).toHaveValue('EUR'));
            expect(within(screen.getByRole('form', {name: 'Edit priorities'})).getByLabelText('Minimum base salary')).toHaveValue(123456);
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await waitFor(() => expect(bodies.length).toBe(1));
            expect(bodies[0].patch.set).toEqual({'compensation.minimum_salary': 123456, 'compensation.currency': 'EUR'});
        });

        test('opening the editor with the same seed object again shows the proposal, not the saved values', async () => {
            mockFetch({'/api/agent/preferences/': () => json(snapshotBody(2, [chip]))});
            render(<PrioritiesSection variant="main" authenticated/>);
            await screen.findByRole('button', {name: /Edit priorities/});
            const seed = {set: {'compensation.minimum_salary': 175000}};
            act(() => setPrioritiesEditorOpen('main', seed));
            let form = await screen.findByRole('form', {name: 'Edit priorities'});
            expect(within(form).getByLabelText('Minimum base salary')).toHaveValue(175000);
            fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
            await screen.findByRole('button', {name: 'Edit priorities'});
            act(() => setPrioritiesEditorOpen('main', seed));
            form = await screen.findByRole('form', {name: 'Edit priorities'});
            expect(within(form).getByLabelText('Minimum base salary')).toHaveValue(175000);
        });

        test('Edit then Cancel from the applied summary brings the summary and its Undo back until Done', async () => {
            mockFetch({
                '/api/agent/preferences/propose/': () => json(proposal),
                '/api/agent/preferences/apply/': () => json({scope: 'account', revision: 3, changes: proposal.changes,
                    undo: {expected_revision: 3, document: {}}}),
                '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
            });
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await screen.findByRole('heading', {name: 'Review your changes'});
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            await screen.findByTestId('priorities-applied');
            act(() => setPrioritiesEditorOpen('main'));
            await screen.findByRole('form', {name: 'Edit priorities'});
            fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
            expect(await screen.findByRole('button', {name: 'Undo'})).toBeInTheDocument();
            act(() => setPrioritiesEditorOpen('main'));
            await screen.findByRole('form', {name: 'Edit priorities'});
            act(() => setPrioritiesEditorOpen(null));
            expect(await screen.findByRole('button', {name: 'Undo'})).toBeInTheDocument();
            fireEvent.click(screen.getByRole('button', {name: 'Done'}));
            await screen.findByRole('button', {name: 'Edit priorities'});
            act(() => setPrioritiesEditorOpen('main'));
            await screen.findByRole('form', {name: 'Edit priorities'});
            fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
            expect(await screen.findByRole('button', {name: 'Edit priorities'})).toBeInTheDocument();
            expect(screen.queryByRole('button', {name: 'Undo'})).not.toBeInTheDocument();
        });

        test('a seed that removes a list entry keeps what was typed into that list', async () => {
            mockFetch({'/api/agent/preferences/': () => json(snapshotBody(2, [chip]))});
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Culture'), {target: {value: 'open'}});
            fireEvent.click(within(form).getByRole('button', {name: /^Add/}));
            act(() => setPrioritiesEditorOpen('main', {remove: {culture: ['kind']}}));
            await waitFor(() => expect(within(screen.getByRole('form', {name: 'Edit priorities'})).queryByRole('button', {name: /Remove kind/})).toBeNull());
            expect(within(screen.getByRole('form', {name: 'Edit priorities'})).getByRole('button', {name: /Remove open/})).toBeInTheDocument();
        });

        test('the expired-session link returns to the current page when the page renders no sign-in link', async () => {
            const redirected = new Response('<html>login</html>', {status: 200, headers: {'Content-Type': 'text/html'}});
            Object.defineProperty(redirected, 'redirected', {value: true});
            mockFetch({'/api/agent/preferences/': () => redirected});
            window.history.pushState({}, '', '/jobs/?q=sre');
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            const banner = await screen.findByTestId('priorities-session-expired');
            expect(within(banner).getByRole('link', {name: /sign in/i}))
                .toHaveAttribute('href', '/accounts/login/?next=%2Fjobs%2F%3Fq%3Dsre');
            window.history.pushState({}, '', '/');
        });

        test('Review latest shows a busy state, ignores a second press and announces the new proposal', async () => {
            let release: (r: Response) => void = () => undefined;
            let slow = false;
            const bodies: any[] = [];
            mockFetch({
                '/api/agent/preferences/propose/': (_u, init) => { bodies.push(init); return json(proposal); },
                '/api/agent/preferences/apply/': () => json({error: {type: 'preference_stale', message: 's', current_revision: 5}}, 409),
                '/api/agent/preferences/': () => (slow ? new Promise<Response>((r) => { release = r; }) as any : json(snapshotBody(2, [chip]))),
            });
            render(<PrioritiesSection variant="main" authenticated/>);
            const form = await openEditor();
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await screen.findByRole('heading', {name: 'Review your changes'});
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            await screen.findByTestId('priorities-review-error');
            slow = true;
            fireEvent.click(screen.getByRole('button', {name: 'Review latest'}));
            const busy = await screen.findByRole('button', {name: 'Checking…'});
            expect(busy).toHaveAttribute('aria-disabled', 'true');
            fireEvent.click(busy);
            const first = bodies.length;
            const changed = snapshotBody(5, [chip]);
            changed.fields[0] = field({value: 90000, set: true});
            await act(async () => { release(json(changed)); });
            await waitFor(() => expect(bodies.length).toBe(first + 1));
            const heading = await screen.findByRole('heading', {name: 'Review your changes'});
            await waitFor(() => expect(heading).toHaveFocus());
            expect(screen.getByTestId('priorities-announcement')).toHaveTextContent('Updated against your latest priorities.');
        });
    });

    describe('collapsed sidebar row (issue #480)', () => {
        const pref = (n: number) => ({path: `p${n}`, label: `Pref ${n}`, display: `v${n}`, hard: false, supported: true});
        const many = [chip, chip2, ...Array.from({length: 8}, (_, i) => pref(i))];
        const serve = (chips: unknown[] = many) => mockFetch({
            '/api/agent/preferences/propose/': () => json(proposal),
            '/api/agent/preferences/apply/': () => json({revision: 3, changes: proposal.changes, undo: {expected_revision: 3, document: {}}}),
            '/api/agent/preferences/reset/': () => json({error: {type: 'server', message: 'nope'}}, 500),
            '/api/agent/preferences/': () => json(snapshotBody(2, chips)),
        });

        test('is collapsed by default: one row with the summary and Edit, and no chip in the DOM', async () => {
            serve();
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            const toggle = await screen.findByTestId('priorities-summary-toggle');
            expect(toggle).toHaveAttribute('aria-expanded', 'false');
            expect(toggle).toHaveAccessibleName('Your priorities Requires: $150,000 \u00b7 9 preferences');
            expect(screen.getByTestId('priorities-summary')).toHaveTextContent('Requires: $150,000 \u00b7 9 preferences');
            expect(screen.queryByTestId('priority-chip')).not.toBeInTheDocument();
            expect(screen.queryByRole('button', {name: 'Reset priorities'})).not.toBeInTheDocument();
            expect(screen.getByRole('button', {name: 'Edit priorities'})).toBeInTheDocument();
            expect(screen.getByRole('region', {name: 'Your priorities'})).toBe(screen.getByTestId('priorities-sidebar'));
            expect(toggle.closest('h2')).not.toBeNull();
            expect(document.querySelector('.priorities-title')).toBeNull();
        });

        test('the toggle expands to every chip, the legend and Reset, then collapses; focus stays on it', async () => {
            serve();
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            const toggle = await screen.findByTestId('priorities-summary-toggle');
            const details = document.getElementById(toggle.getAttribute('aria-controls')!)!;
            expect(details).not.toBeVisible();
            toggle.focus();
            fireEvent.click(toggle);
            expect(toggle).toHaveAttribute('aria-expanded', 'true');
            expect(details).toBeVisible();
            expect(within(details).getAllByTestId('priority-chip')).toHaveLength(10);
            expect(within(details).queryByRole('button', {name: /more/})).not.toBeInTheDocument();
            // The key to the marks takes the summary's line in the row, before the chips; it is not read twice.
            const legend = within(toggle).getByTestId('priority-chip-legend');
            expect(legend).toHaveAttribute('aria-hidden', 'true');
            expect(legend).toHaveTextContent('RequirementNot used yet');
            expect(within(details).queryByTestId('priority-chip-legend')).not.toBeInTheDocument();
            expect(within(details).getByRole('button', {name: 'Reset priorities'})).toBeInTheDocument();
            expect(details.querySelector('.priorities-scroll .priority-chips')).not.toBeNull();
            expect(toggle).toHaveFocus();
            fireEvent.click(toggle);
            expect(toggle).toHaveAttribute('aria-expanded', 'false');
            expect(screen.queryByTestId('priority-chip')).not.toBeInTheDocument();
            expect(screen.queryByTestId('priority-chip-legend')).not.toBeInTheDocument();
            expect(toggle).toHaveFocus();
        });

        test('Edit on the collapsed row opens the shared editor; Cancel returns focus to it', async () => {
            serve();
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            const form = await openEditor('sidebar');
            expect(form.closest('.priorities-scroll')).not.toBeNull();
            expect(screen.queryByTestId('priorities-summary-toggle')).not.toBeInTheDocument();
            expect(screen.getByRole('heading', {name: 'Your priorities'})).toHaveClass('priorities-title');
            fireEvent.click(within(form).getByRole('button', {name: 'Cancel'}));
            await waitFor(() => expect(screen.getByRole('button', {name: 'Edit priorities'})).toHaveFocus());
            expect(screen.getByTestId('priorities-summary-toggle')).toHaveAttribute('aria-expanded', 'false');
        });

        test('a chip in the expanded list opens the editor', async () => {
            serve();
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            fireEvent.click(await screen.findByTestId('priorities-summary-toggle'));
            fireEvent.click(screen.getByRole('button', {name: /^Edit Minimum base salary/}));
            expect(getWorkspaceSnapshot().prioritiesEditorOpenIn).toBe('sidebar');
            expect(await screen.findByRole('form', {name: 'Edit priorities'})).toBeInTheDocument();
        });

        test('the applied summary scrolls inside the section and Done returns focus to the row', async () => {
            serve();
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            const form = await openEditor('sidebar');
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            fireEvent.click(await screen.findByRole('button', {name: 'Apply to account'}));
            const applied = await screen.findByTestId('priorities-applied');
            expect(applied.closest('.priorities-scroll')).not.toBeNull();
            fireEvent.click(within(applied).getByRole('button', {name: 'Done'}));
            await waitFor(() => expect(screen.getByRole('button', {name: 'Edit priorities'})).toHaveFocus());
        });

        test('Keep priorities returns focus to Reset and the list stays expanded; a failed reset says so in the row', async () => {
            serve();
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            fireEvent.click(await screen.findByTestId('priorities-summary-toggle'));
            fireEvent.click(screen.getByRole('button', {name: 'Reset priorities'}));
            const confirm = screen.getByRole('group', {name: 'Confirm reset'});
            expect(confirm.closest('.priorities-scroll')).not.toBeNull();
            fireEvent.click(within(confirm).getByRole('button', {name: 'Keep priorities'}));
            await waitFor(() => expect(screen.getByRole('button', {name: 'Reset priorities'})).toHaveFocus());
            expect(screen.getByTestId('priorities-summary-toggle')).toHaveAttribute('aria-expanded', 'true');
            fireEvent.click(screen.getByRole('button', {name: 'Reset priorities'}));
            fireEvent.click(within(screen.getByRole('group', {name: 'Confirm reset'})).getByRole('button', {name: 'Reset priorities'}));
            expect(await screen.findByTestId('priorities-view-error')).toHaveTextContent('nope');
            expect(screen.getByTestId('priorities-summary-toggle')).toBeInTheDocument();
            await waitFor(() => expect(screen.getByRole('button', {name: 'Reset priorities'})).toHaveFocus());
        });

        test('the summary follows a save made elsewhere on the page', async () => {
            let chips: unknown[] = [chip];
            mockFetch({'/api/agent/preferences/': () => json(snapshotBody(chips.length + 1, chips))});
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            expect(await screen.findByTestId('priorities-summary')).toHaveTextContent('Requires: $150,000');
            chips = [chip, chip2];
            act(() => setPrioritiesRevision(3));
            await waitFor(() => expect(screen.getByTestId('priorities-summary')).toHaveTextContent('Requires: $150,000 \u00b7 1 preference'));
        });

        test('nothing saved: a plain title, "None saved yet" and Add, with no disclosure', async () => {
            serve([]);
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            expect(await screen.findByTestId('priorities-empty')).toHaveTextContent('None saved yet');
            expect(screen.getByRole('heading', {name: /Your priorities/})).toBeInTheDocument();
            expect(screen.queryByTestId('priorities-summary-toggle')).not.toBeInTheDocument();
            expect(screen.queryByRole('button', {name: 'Reset priorities'})).not.toBeInTheDocument();
            fireEvent.click(screen.getByRole('button', {name: 'Add priorities'}));
            expect(getWorkspaceSnapshot().prioritiesEditorOpenIn).toBe('sidebar');
        });

        test('an expanded list that empties falls back to the plain row', async () => {
            let chips: unknown[] = [chip];
            mockFetch({'/api/agent/preferences/': () => json(snapshotBody(chips.length + 1, chips))});
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            fireEvent.click(await screen.findByTestId('priorities-summary-toggle'));
            expect(screen.getAllByTestId('priority-chip')).toHaveLength(1);
            chips = [];
            act(() => setPrioritiesRevision(9));
            expect(await screen.findByTestId('priorities-empty')).toBeInTheDocument();
            expect(screen.queryByTestId('priority-chip')).not.toBeInTheDocument();
            expect(document.getElementById('priorities-details-sidebar')).not.toBeVisible();
        });

        test('loading, load error and an expired session show outside the disclosure', async () => {
            let release: (r: Response) => void = () => undefined;
            mockFetch({'/api/agent/preferences/': () => new Promise<Response>((r) => { release = r; }) as any});
            const {unmount} = render(<PrioritiesSection variant="sidebar" authenticated/>);
            // The placeholder has the row's shape (title, summary, Edit), not the chips'.
            const skeleton = screen.getByTestId('priorities-summary-skeleton');
            expect(skeleton).toHaveAttribute('aria-hidden', 'true');
            expect(skeleton.querySelectorAll('.priorities-skeleton-bar')).toHaveLength(3);
            expect(screen.queryByTestId('priority-chips-skeleton')).not.toBeInTheDocument();
            expect(screen.getByText('Loading your priorities')).toHaveAttribute('role', 'status');
            expect(screen.queryByTestId('priorities-summary-toggle')).not.toBeInTheDocument();
            await act(async () => { release(json({error: {message: 'boom'}}, 500)); });
            expect(await screen.findByTestId('priorities-load-error')).toBeVisible();
            expect(screen.getByRole('button', {name: 'Try again'})).toBeInTheDocument();
            expect(screen.queryByTestId('priorities-summary-toggle')).not.toBeInTheDocument();
            unmount();

            mockFetch({'/api/agent/preferences/': () => json({error: {type: 'auth_required', message: 'x'}}, 401)});
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            expect(await screen.findByTestId('priorities-session-expired')).toBeVisible();
            expect(screen.getByRole('link', {name: 'Sign in'})).toBeInTheDocument();
            expect(screen.queryByRole('button', {name: 'Try again'})).not.toBeInTheDocument();
            // The expired session is said once: no second alert repeats it.
            expect(screen.queryByTestId('priorities-load-error')).not.toBeInTheDocument();
            expect(screen.getAllByRole('alert')).toHaveLength(1);
        });

        test('an account switch collapses the row again', async () => {
            serve();
            act(() => setWorkspaceAccount({status: 'authenticated', key: 'a'}));
            render(<SidebarPriorities/>);
            fireEvent.click(await screen.findByTestId('priorities-summary-toggle'));
            expect(screen.getAllByTestId('priority-chip')).toHaveLength(10);
            act(() => setWorkspaceAccount({status: 'authenticated', key: 'b'}));
            expect(await screen.findByTestId('priorities-summary-toggle')).toHaveAttribute('aria-expanded', 'false');
        });

        test('the main block has no disclosure', async () => {
            serve();
            render(<PrioritiesSection variant="main" authenticated/>);
            expect(await screen.findAllByTestId('priority-chip')).toHaveLength(5);
            expect(screen.queryByTestId('priorities-summary-toggle')).not.toBeInTheDocument();
            expect(screen.queryByTestId('priorities-summary')).not.toBeInTheDocument();
            expect(screen.getByRole('button', {name: 'Edit priorities'})).toHaveTextContent(/^Edit priorities$/);
            expect(screen.getByRole('button', {name: 'Reset priorities'})).toBeInTheDocument();
            expect(document.querySelector('.priorities-scroll')).toBeNull();
        });
    });

    describe('visual round 2 (issue #480)', () => {
        const pref = (n: number) => ({path: `p${n}`, label: `Pref ${n}`, display: `v${n}`, hard: false, supported: true});
        const hard = (n: number) => ({path: `h${n}`, label: `Hard ${n}`, display: `Need${n}`, hard: true, supported: true});
        const serve = (chips: unknown[]) => mockFetch({
            '/api/agent/preferences/propose/': () => json(proposal),
            '/api/agent/preferences/': () => json(snapshotBody(2, chips)),
        });

        describe('pinning the open block', () => {
            // jsdom lays nothing out: the heights are given, and the observer is driven by hand.
            const heights = {section: 0, body: 0, header: 0, footer: 0, pill: 0};
            const observers: Array<{run: () => void; observed: Element[]; disconnected: boolean}> = [];
            const realOffset = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight');
            const realClient = Object.getOwnPropertyDescriptor(Element.prototype, 'clientHeight');
            const offsetOf = (el: HTMLElement) => (el.matches('.priorities-sidebar') ? heights.section
                : el.matches('.card-header') ? heights.header
                    : el.matches('.chat-footer') ? heights.footer
                        : el.matches('.chat-jump-row') ? heights.pill : 0);

            beforeEach(() => {
                observers.length = 0;
                Object.assign(heights, {section: 0, body: 0, header: 0, footer: 0, pill: 0});
                (global as any).ResizeObserver = class {
                    private entry: {run: () => void; observed: Element[]; disconnected: boolean};
                    constructor(run: () => void) { this.entry = {run, observed: [], disconnected: false}; observers.push(this.entry); }
                    observe(el: Element) { this.entry.observed.push(el); }
                    unobserve(el: Element) { this.entry.observed = this.entry.observed.filter((seen) => seen !== el); }
                    disconnect() { this.entry.disconnected = true; }
                };
                Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
                    configurable: true, get() { return offsetOf(this as HTMLElement); },
                });
                Object.defineProperty(Element.prototype, 'clientHeight', {
                    configurable: true, get() { return (this as Element).matches('.assistant-panel-body') ? heights.body : 0; },
                });
            });
            afterEach(() => {
                delete (global as any).ResizeObserver;
                Object.defineProperty(HTMLElement.prototype, 'offsetHeight', realOffset!);
                Object.defineProperty(Element.prototype, 'clientHeight', realClient!);
            });
            const live = () => observers.filter((o) => !o.disconnected);
            const panel = (node: React.ReactNode, chat: React.ReactNode = null) => (
                <div className="assistant-panel-body">{node}<div id="job-search-chat">{chat}</div></div>
            );
            const chatCard = (footerKey: string, pill: 'static' | 'absolute' | null = null) => (
                <section data-testid="job-search-chat">
                    <div className="card-header"></div>
                    <div className="chat-footer" key={footerKey}>
                        {pill && <div className="chat-jump-row" style={{position: pill}}></div>}
                    </div>
                </section>
            );

            test('the open block is pinned only while the chat keeps its room under it, and says how tall it is', async () => {
                serve([chip, pref(1)]);
                heights.section = 61;
                heights.body = 800;
                const {unmount} = render(panel(<PrioritiesSection variant="sidebar" authenticated/>));
                const section = await screen.findByTestId('priorities-sidebar');
                const body = section.parentElement!;
                const toggle = await screen.findByTestId('priorities-summary-toggle');
                // Collapsed and without the focus: nothing is pinned and the chat's header keeps its own offset.
                expect(section).not.toHaveAttribute('data-pinned');
                expect(body.style.getPropertyValue('--priorities-open-h')).toBe('0px');
                expect(live()).toHaveLength(1);
                expect(live()[0].observed).toEqual([section, body]);

                // Expanded with room to spare: pinned, and the panel knows the height to offset the chat header by.
                heights.section = 400;
                fireEvent.click(toggle);
                expect(section).toHaveAttribute('data-pinned');
                expect(body.style.getPropertyValue('--priorities-open-h')).toBe('400px');

                // Before the chat has mounted its bars are assumed. Exactly the room: still pinned.
                // One pixel short (the panel shrank): it scrolls with the panel instead.
                heights.body = 400 + CHAT_BARS_FALLBACK_PX + PIN_TRANSCRIPT_PX;
                act(() => live()[0].run());
                expect(section).toHaveAttribute('data-pinned');
                heights.body -= 1;
                act(() => live()[0].run());
                expect(section).not.toHaveAttribute('data-pinned');
                expect(body.style.getPropertyValue('--priorities-open-h')).toBe('400px');

                // The editor is an open step too. Cancel returns the focus to Edit, and the row that holds it stays pinned.
                heights.body = 800;
                await openEditor('sidebar');
                expect(section).toHaveAttribute('data-pinned');
                fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
                const edit = await screen.findByRole('button', {name: 'Edit priorities'});
                await waitFor(() => expect(edit).toHaveFocus());
                heights.section = 61;
                fireEvent.click(screen.getByTestId('priorities-summary-toggle'));
                expect(screen.queryByTestId('priority-chip')).not.toBeInTheDocument();
                expect(section).toHaveAttribute('data-pinned');
                expect(body.style.getPropertyValue('--priorities-open-h')).toBe('61px');
                // Focus moving to the row's other button never lets go of it; focus leaving the row does.
                act(() => screen.getByTestId('priorities-summary-toggle').focus());
                expect(section).toHaveAttribute('data-pinned');
                fireEvent.focusOut(screen.getByTestId('priorities-summary-toggle'), {relatedTarget: edit});
                expect(section).toHaveAttribute('data-pinned');
                act(() => screen.getByTestId('priorities-summary-toggle').blur());
                expect(section).not.toHaveAttribute('data-pinned');
                expect(body.style.getPropertyValue('--priorities-open-h')).toBe('0px');
                // A row that holds the focus is not pinned where even it leaves the chat no room.
                heights.body = 61 + CHAT_BARS_FALLBACK_PX + PIN_TRANSCRIPT_PX - 1;
                act(() => edit.focus());
                expect(section).not.toHaveAttribute('data-pinned');
                expect(body.style.getPropertyValue('--priorities-open-h')).toBe('61px');

                unmount();
                expect(live()).toHaveLength(0);
                expect(body.style.getPropertyValue('--priorities-open-h')).toBe('');
            });

            test('a pinned block that grows past its room is brought back on screen; one the composer pushed out is left alone', async () => {
                serve([chip, pref(1)]);
                heights.section = 0;
                heights.body = 241 + CHAT_BARS_FALLBACK_PX + PIN_TRANSCRIPT_PX;
                render(panel(<PrioritiesSection variant="sidebar" authenticated/>));
                const section = await screen.findByTestId('priorities-sidebar');
                const body = section.parentElement!;
                const at = {section: 0, body: 0};
                jest.spyOn(section, 'getBoundingClientRect').mockImplementation(() => ({top: at.section}) as DOMRect);
                jest.spyOn(body, 'getBoundingClientRect').mockImplementation(() => ({top: at.body}) as DOMRect);
                const grow = (height: number) => { heights.section = height; act(() => live()[0].run()); };
                const toggle = await screen.findByTestId('priorities-summary-toggle');

                // A block not yet laid out (no height) has nothing to have grown from.
                fireEvent.click(toggle);
                expect(section).toHaveAttribute('data-pinned');
                body.scrollTop = 2500;
                at.section = -2439;
                grow(299);
                expect(section).not.toHaveAttribute('data-pinned');
                expect(body.scrollTop).toBe(2500);

                // Pinned at 241px with the reader at the end. An error makes it 299px: it no longer fits pinned and
                // is back at the top of the panel's content, 2,439px above the reader. It is brought to them.
                grow(241);
                expect(section).toHaveAttribute('data-pinned');
                grow(299);
                expect(section).not.toHaveAttribute('data-pinned');
                expect(body.scrollTop).toBe(61);

                // Already let go: growing again moves nothing.
                body.scrollTop = 2500;
                grow(320);
                expect(body.scrollTop).toBe(2500);

                // It grew, but its top is still on screen: nothing to bring back.
                grow(241);
                at.section = 12;
                grow(299);
                expect(section).not.toHaveAttribute('data-pinned');
                expect(body.scrollTop).toBe(2500);

                // The composer grew instead (a draft): the block lets go, and a reader who is typing is not pulled away.
                at.section = -2439;
                grow(241);
                heights.body -= 40;
                act(() => live()[0].run());
                expect(section).not.toHaveAttribute('data-pinned');
                expect(body.scrollTop).toBe(2500);

                // Nor is the collapsed row, pinned only because it holds the focus.
                heights.body += 40;
                heights.section = 61;
                fireEvent.click(toggle);
                act(() => toggle.focus());
                expect(screen.queryByTestId('priority-chip')).not.toBeInTheDocument();
                expect(section).toHaveAttribute('data-pinned');
                grow(400);
                expect(section).not.toHaveAttribute('data-pinned');
                expect(body.scrollTop).toBe(2500);
            });

            test('the room is measured from the chat\'s own header and composer band, whenever they mount or change', async () => {
                serve([chip, pref(1)]);
                heights.section = 400;
                heights.body = 400 + 50 + 100 + PIN_TRANSCRIPT_PX;
                const node = <PrioritiesSection variant="sidebar" authenticated/>;
                const {rerender} = render(panel(node));
                const section = await screen.findByTestId('priorities-sidebar');
                const body = section.parentElement!;
                fireEvent.click(await screen.findByTestId('priorities-summary-toggle'));
                // No chat yet: the assumed bars are taller than this panel leaves room for.
                expect(section).not.toHaveAttribute('data-pinned');

                // The chat mounts after the block: its header (50) and composer band (100) are what must fit.
                heights.header = 50;
                heights.footer = 100;
                rerender(panel(node, chatCard('first')));
                await waitFor(() => expect(section).toHaveAttribute('data-pinned'));
                const card = screen.getByTestId('job-search-chat');
                const footer = card.querySelector('.chat-footer')!;
                expect(live()[0].observed).toEqual([section, body, card, footer]);

                // The band grows by a line (a draft, a notice): one pixel short, so the block lets go.
                heights.footer = 101;
                act(() => live()[0].run());
                expect(section).not.toHaveAttribute('data-pinned');

                // The chat replaces its footer (a new conversation): the new one is observed, the old one dropped.
                heights.footer = 100;
                rerender(panel(node, chatCard('second')));
                await waitFor(() => expect(section).toHaveAttribute('data-pinned'));
                const next = card.querySelector('.chat-footer')!;
                expect(next).not.toBe(footer);
                expect(live()[0].observed).toEqual([section, body, card, next]);

                // The jump pill takes a row of the band while the panel scrolls; it comes and goes with the
                // reader's scrolling, so it does not unpin the block. Floating over the transcript it takes none.
                heights.footer = 144;
                heights.pill = 44;
                rerender(panel(node, chatCard('second', 'static')));
                await waitFor(() => expect(card.querySelector('.chat-jump-row')).not.toBeNull());
                act(() => live()[0].run());
                expect(section).toHaveAttribute('data-pinned');
                rerender(panel(node, chatCard('second', 'absolute')));
                await waitFor(() => expect(card.querySelector<HTMLElement>('.chat-jump-row')!.style.position).toBe('absolute'));
                act(() => live()[0].run());
                expect(section).not.toHaveAttribute('data-pinned');

                // The chat goes away (signed out mid-session): back to the assumed bars, nothing left observed of it.
                rerender(panel(node));
                await waitFor(() => expect(live()[0].observed).toEqual([section, body]));
                expect(section).not.toHaveAttribute('data-pinned');
            });

            test('nothing is observed outside the panel, in the main block, or signed out', async () => {
                serve([chip]);
                heights.section = 400;
                heights.body = 800;
                const first = render(<PrioritiesSection variant="sidebar" authenticated/>);
                fireEvent.click(await screen.findByTestId('priorities-summary-toggle'));
                expect(screen.getByTestId('priorities-sidebar')).not.toHaveAttribute('data-pinned');
                first.unmount();
                const second = render(panel(<PrioritiesSection variant="main" authenticated/>));
                await openEditor('main');
                expect(screen.getByTestId('priorities-main')).not.toHaveAttribute('data-pinned');
                second.unmount();
                const {container} = render(panel(<PrioritiesSection variant="sidebar" authenticated={false}/>));
                expect(container.querySelector('.priorities-section')).toBeNull();
                expect(observers).toHaveLength(0);
            });
        });

        test('without ResizeObserver the open block is simply not pinned', async () => {
            serve([chip]);
            render(<div className="assistant-panel-body"><PrioritiesSection variant="sidebar" authenticated/></div>);
            fireEvent.click(await screen.findByTestId('priorities-summary-toggle'));
            expect(screen.getByTestId('priorities-sidebar')).not.toHaveAttribute('data-pinned');
            expect(screen.getByTestId('priorities-sidebar').parentElement!.style.getPropertyValue('--priorities-open-h')).toBe('');
        });

        test('the summary is laid out as a lead that may truncate and counts that never do; the text is one string', async () => {
            serve([hard(1), hard(2), hard(3), pref(1), pref(2)]);
            const {unmount} = render(<PrioritiesSection variant="sidebar" authenticated/>);
            const summary = await screen.findByTestId('priorities-summary');
            expect(summary).toHaveTextContent(/^Requires: Need1, Need2, \+1 \u00b7 2 preferences$/);
            expect(summary.querySelector('.priorities-summary-lead')).toHaveTextContent(/^Requires: Need1, Need2,$/);
            expect(summary.querySelector('.priorities-summary-keep')).toBeNull();
            expect(summary.querySelector('.priorities-summary-sep')!.textContent).toBe(' ');
            expect(summary.querySelector('.priorities-summary-tail')).toHaveTextContent(/^\+1 \u00b7 2 preferences$/);
            // (jsdom's name computation puts a space between inline spans; browsers do not.)
            expect(screen.getByTestId('priorities-summary-toggle'))
                .toHaveAccessibleName(/^Your priorities Requires: Need1, Need2, ?\+1 \u00b7 2 preferences$/);
            unmount();

            // A value named by its field is kept whole beside the lead that may be cut.
            serve([{path: 'work_location.max_in_office_days', label: 'Maximum office days per week', display: '2', hard: true, supported: true}, pref(1)]);
            const named = render(<PrioritiesSection variant="sidebar" authenticated/>);
            const kept = await screen.findByTestId('priorities-summary');
            expect(kept).toHaveTextContent(/^Requires: Maximum office days per week: 2 \u00b7 1 preference$/);
            const first = kept.querySelector('.priorities-summary-first')!;
            expect(Array.from(first.children).map((el) => [el.className, el.textContent])).toEqual([
                ['priorities-summary-lead', 'Requires: Maximum office days per week'], ['priorities-summary-keep', ': 2'],
            ]);
            named.unmount();

            serve([pref(1), pref(2)]);
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            const only = await screen.findByTestId('priorities-summary');
            expect(only).toHaveTextContent(/^2 preferences$/);
            expect(only.children).toHaveLength(1);
            expect(only.querySelector('.priorities-summary-tail')).toBeNull();
        });

        test('the key names only the marks the chips carry, and is left out when none is marked', async () => {
            serve([chip, pref(1)]);
            const first = render(<PrioritiesSection variant="sidebar" authenticated/>);
            fireEvent.click(await screen.findByTestId('priorities-summary-toggle'));
            expect(screen.getByTestId('priority-chip-legend')).toHaveTextContent(/^Requirement$/);
            first.unmount();

            serve([chip2]);
            const second = render(<PrioritiesSection variant="sidebar" authenticated/>);
            fireEvent.click(await screen.findByTestId('priorities-summary-toggle'));
            expect(screen.getByTestId('priority-chip-legend')).toHaveTextContent(/^Not used yet$/);
            second.unmount();

            serve([pref(1), pref(2)]);
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            fireEvent.click(await screen.findByTestId('priorities-summary-toggle'));
            expect(screen.getAllByTestId('priority-chip')).toHaveLength(2);
            expect(screen.queryByTestId('priority-chip-legend')).not.toBeInTheDocument();
        });

        test('the sidebar editor is named by a visible heading outside its scroll region; the main editor keeps its title', async () => {
            serve([chip]);
            const first = render(<PrioritiesSection variant="sidebar" authenticated/>);
            await openEditor('sidebar');
            const title = screen.getByTestId('priorities-step-title');
            expect(title).toHaveTextContent('Edit priorities');
            expect(title.tagName).toBe('H3');
            expect(title.closest('.priorities-scroll')).toBeNull();
            expect(title.nextElementSibling).toHaveClass('priorities-scroll');
            first.unmount();
            act(() => resetWorkspaceForTests());
            render(<PrioritiesSection variant="main" authenticated/>);
            await openEditor('main');
            expect(screen.queryByTestId('priorities-step-title')).not.toBeInTheDocument();
        });

        test('the sidebar review shows the change before the note and puts Cancel beside Apply; the main review is unchanged', async () => {
            const names = (group: HTMLElement) => within(group).getAllByRole('button').map((b) => b.textContent);
            serve([chip]);
            const first = render(<PrioritiesSection variant="sidebar" authenticated/>);
            const form = await openEditor('sidebar');
            fireEvent.change(within(form).getByRole('spinbutton', {name: /Minimum base salary/}), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            const review = await screen.findByTestId('priorities-review');
            expect(review).toHaveClass('priorities-review-compact');
            expect(names(within(review).getByRole('group', {name: 'Review actions'})))
                .toEqual(['Apply to account', 'Cancel', 'Edit', 'This search only']);
            const list = within(review).getByRole('list', {name: 'Proposed changes'});
            const note = review.querySelector('.priorities-scope-note')!;
            expect(list.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
            first.unmount();

            act(() => resetWorkspaceForTests());
            serve([chip]);
            render(<PrioritiesSection variant="main" authenticated/>);
            const mainForm = await openEditor('main');
            fireEvent.change(within(mainForm).getByRole('spinbutton', {name: /Minimum base salary/}), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            const mainReview = await screen.findByTestId('priorities-review');
            expect(mainReview).not.toHaveClass('priorities-review-compact');
            expect(names(within(mainReview).getByRole('group', {name: 'Review actions'})))
                .toEqual(['Apply to account', 'Edit', 'This search only', 'Cancel']);
            const mainNote = mainReview.querySelector('.priorities-scope-note')!;
            expect(mainNote.compareDocumentPosition(within(mainReview).getByRole('list', {name: 'Proposed changes'}))
                & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        });
    });

    describe('visual round 3 (issue #480)', () => {
        const pref = (n: number) => ({path: `p${n}`, label: `Pref ${n}`, display: `v${n}`, hard: false, supported: true});
        const serve = (apply: () => Response = () => json({revision: 3, changes: proposal.changes, undo: null})) => mockFetch({
            '/api/agent/preferences/propose/': () => json(proposal),
            '/api/agent/preferences/apply/': apply,
            '/api/agent/preferences/reset/': () => json({error: {type: 'server', message: 'nope'}}, 500),
            '/api/agent/preferences/': () => json(snapshotBody(2, [chip, pref(1)])),
        });

        describe('keeping the reader\'s place when a step changes', () => {
            // jsdom lays nothing out and never scrolls: positions are given and frames are run by hand.
            let queued = new Map<number, FrameRequestCallback>();
            let nextId = 0;
            const tops = {section: 100, body: 100};
            const frame = () => act(() => {
                const run = Array.from(queued.values());
                queued = new Map();
                run.forEach((callback) => callback(0));
            });
            const settle = async () => { for (let i = 0; i < 6; i += 1) await frame(); };

            beforeEach(() => {
                queued = new Map();
                Object.assign(tops, {section: 100, body: 100});
                jest.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
                    nextId += 1;
                    queued.set(nextId, callback);
                    return nextId;
                });
                jest.spyOn(window, 'cancelAnimationFrame').mockImplementation((id) => { queued.delete(id); });
            });
            afterEach(() => jest.restoreAllMocks());

            const Chat = ({owner, pill}: {owner: 'panel' | 'transcript'; pill: boolean}) => (
                <div id="job-search-chat">
                    <section data-testid="job-search-chat" data-scroll-owner={owner}>
                        <div role="log"></div>
                        <div className="chat-footer">{pill && <button type="button" data-testid="jump-to-latest">Jump</button>}</div>
                    </section>
                </div>
            );
            async function mount(owner: 'panel' | 'transcript', pill = false) {
                serve();
                const ui = (shown: boolean) => (
                    <div className="assistant-panel-body">
                        <PrioritiesSection variant="sidebar" authenticated/>
                        <Chat owner={owner} pill={shown}/>
                    </div>
                );
                const view = render(ui(pill));
                const section = await screen.findByTestId('priorities-sidebar');
                const panel = section.parentElement!;
                const log = panel.querySelector<HTMLElement>('[role="log"]')!;
                jest.spyOn(section, 'getBoundingClientRect').mockImplementation(() => ({top: tops.section}) as DOMRect);
                jest.spyOn(panel, 'getBoundingClientRect').mockImplementation(() => ({top: tops.body}) as DOMRect);
                Object.defineProperty(panel, 'scrollHeight', {configurable: true, value: 2000});
                Object.defineProperty(log, 'scrollHeight', {configurable: true, value: 3000});
                const toggle = await screen.findByTestId('priorities-summary-toggle');
                await settle();
                return {section, panel, log, toggle, showPill: (shown: boolean) => view.rerender(ui(shown)), unmount: view.unmount};
            }

            test('an open block that does not pin is brought back on screen, and the reader returns to the end when it closes', async () => {
                const {section, panel, toggle, showPill} = await mount('panel');
                panel.scrollTop = 900;
                act(() => toggle.focus());
                fireEvent.click(toggle);
                // The chat followed the conversation: the block's top is 400px above the panel's.
                tops.section = -300;
                await frame();
                expect(panel.scrollTop).toBe(500);
                tops.section = 100;
                // The reader is no longer at the end, so the chat shows its pill; nothing else moves.
                showPill(true);
                await settle();
                expect(panel.scrollTop).toBe(500);
                expect(queued.size).toBe(0);

                // Closed again with the row pinned (it holds the focus): back to the end of the conversation.
                fireEvent.click(toggle);
                section.setAttribute('data-pinned', '');
                await frame();
                expect(panel.scrollTop).toBe(2000);
                showPill(false);
                await settle();
                expect(queued.size).toBe(0);
            });

            test('a reader who was reading older messages is left where they are', async () => {
                const {section, panel, toggle} = await mount('panel', true);
                panel.scrollTop = 300;
                fireEvent.click(toggle);
                section.setAttribute('data-pinned', '');
                await settle();
                expect(panel.scrollTop).toBe(300);
                // Opened where it does not pin and scrolled away by the reader: brought back, but not sent to the end on closing.
                section.removeAttribute('data-pinned');
                await openEditor('sidebar');
                tops.section = 40;
                await frame();
                expect(panel.scrollTop).toBe(240);
                tops.section = 100;
                await settle();
                act(() => screen.getByRole('button', {name: 'Cancel'}).click());
                section.setAttribute('data-pinned', '');
                await settle();
                expect(panel.scrollTop).toBe(240);
            });

            test('while the chat scrolls its own transcript a reader at the end stays there; a pinned block keeps them there in the panel', async () => {
                const first = await mount('transcript');
                fireEvent.click(first.toggle);
                await frame();
                expect(first.log.scrollTop).toBe(3000);
                expect(first.panel.scrollTop).toBe(0);
                first.unmount();
                act(() => resetWorkspaceForTests());

                const second = await mount('panel');
                fireEvent.click(second.toggle);
                // Not pinned and still on screen: nothing to do.
                await frame();
                expect(second.panel.scrollTop).toBe(0);
                second.section.setAttribute('data-pinned', '');
                await frame();
                expect(second.panel.scrollTop).toBe(2000);
                expect(second.log.scrollTop).toBe(0);
            });

            test('the row that took the focus is brought on screen where it cannot pin, and a pending frame is dropped on unmount', async () => {
                const {panel, unmount} = await mount('panel', true);
                panel.scrollTop = 700;
                await openEditor('sidebar');
                await settle();
                // Cancel returns the focus to Edit without scrolling; the row is 150px above the panel's top.
                fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
                await waitFor(() => expect(screen.getByRole('button', {name: 'Edit priorities'})).toHaveFocus());
                tops.section = -50;
                await frame();
                expect(panel.scrollTop).toBe(550);
                expect(queued.size).toBe(1);
                unmount();
                expect(queued.size).toBe(0);
            });
        });

        test('closing returns the focus to the row without scrolling the panel; the main block scrolls to its button as before', async () => {
            const focus = jest.spyOn(HTMLElement.prototype, 'focus');
            serve();
            const first = render(<PrioritiesSection variant="sidebar" authenticated/>);
            await openEditor('sidebar');
            fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
            const edit = await screen.findByRole('button', {name: 'Edit priorities'});
            await waitFor(() => expect(edit).toHaveFocus());
            expect(focus.mock.contexts[focus.mock.calls.length - 1]).toBe(edit);
            expect(focus).toHaveBeenLastCalledWith({preventScroll: true});
            first.unmount();
            act(() => resetWorkspaceForTests());

            serve();
            render(<PrioritiesSection variant="main" authenticated/>);
            await openEditor('main');
            fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
            await waitFor(() => expect(screen.getByRole('button', {name: 'Edit priorities'})).toHaveFocus());
            expect(focus).toHaveBeenLastCalledWith({preventScroll: false});
            focus.mockRestore();
        });

        test('an expired session on Apply is said once in the sidebar, by the sign-in line', async () => {
            const expired = () => json({error: {type: 'auth_required'}}, 401);
            async function applyExpired(variant: 'main' | 'sidebar') {
                serve(expired);
                const view = render(<PrioritiesSection variant={variant} authenticated/>);
                const form = await openEditor(variant);
                fireEvent.change(within(form).getByRole('spinbutton', {name: /Minimum base salary/}), {target: {value: '150000'}});
                fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
                fireEvent.click(await screen.findByRole('button', {name: 'Apply to account'}));
                expect(await screen.findByTestId('priorities-session-expired')).toBeInTheDocument();
                return view;
            }
            const sidebar = await applyExpired('sidebar');
            expect(screen.getByTestId('priorities-review')).toBeInTheDocument();
            expect(screen.queryByTestId('priorities-review-error')).not.toBeInTheDocument();
            expect(screen.getAllByRole('alert')).toHaveLength(1);
            sidebar.unmount();
            act(() => resetWorkspaceForTests());
            // The main block is unchanged: the review keeps its own message.
            await applyExpired('main');
            expect(screen.getByTestId('priorities-review-error')).toBeInTheDocument();
        });

        test('a failed Apply in the sidebar review says so in the review, with the actions still there', async () => {
            serve(() => json({error: {type: 'server', message: 'Could not save right now.'}}, 500));
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            const form = await openEditor('sidebar');
            fireEvent.change(within(form).getByRole('spinbutton', {name: /Minimum base salary/}), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            fireEvent.click(await screen.findByRole('button', {name: 'Apply to account'}));
            const error = await screen.findByTestId('priorities-review-error');
            expect(error).toHaveAttribute('role', 'alert');
            expect(error).toHaveTextContent('Could not save right now.');
            // The message sits between the change and the actions, where the stylesheet pins it above the action row.
            const review = screen.getByTestId('priorities-review');
            expect(error.parentElement).toBe(review);
            expect(error.nextElementSibling).toBe(within(review).getByRole('group', {name: 'Review actions'}));
            expect(screen.getByRole('button', {name: 'Apply to account'})).toBeEnabled();
        });

        test('an invalid value in the sidebar scrolls only the block\'s own list to the field, never the panel', async () => {
            scrollIntoView.mockClear();
            mockFetch({
                '/api/agent/preferences/propose/': () => json({error: {type: 'invalid_request', message: 'bad',
                    field_errors: {'compensation.minimum_salary': ['Must be zero or more.']}}}, 400),
                '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
            });
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            const form = await openEditor('sidebar');
            const input = within(form).getByLabelText('Minimum base salary');
            const region = form.closest<HTMLElement>('.priorities-scroll')!;
            // The field's row is 300px below the list's top edge and its reason ends 90px further down; the pinned
            // footer starts 400px below that edge. (The reason exists once the server has refused the value.)
            const rects = {row: {top: 500}, reason: {bottom: 590}, footer: {top: 600}};
            const row = input.closest<HTMLElement>('.priorities-field')!;
            const realQuery = row.querySelector.bind(row);
            jest.spyOn(region, 'getBoundingClientRect').mockImplementation(() => ({top: 200}) as DOMRect);
            jest.spyOn(row, 'getBoundingClientRect').mockImplementation(() => rects.row as DOMRect);
            jest.spyOn(row, 'querySelector').mockImplementation((selector: string) => {
                const found = realQuery(selector) as HTMLElement | null;
                if (found && selector === '.priorities-field-error') found.getBoundingClientRect = () => rects.reason as DOMRect;
                return found;
            });
            jest.spyOn(region.querySelector<HTMLElement>('.priorities-footer')!, 'getBoundingClientRect').mockImplementation(() => rects.footer as DOMRect);
            region.scrollTop = 40;
            fireEvent.change(input, {target: {value: '-5'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await waitFor(() => expect(input).toHaveFocus());
            expect(input).toHaveAttribute('aria-invalid', 'true');
            expect(row.querySelector('.priorities-field-error')).toHaveTextContent('Must be zero or more.');
            // The row fits above the footer down to its reason: it goes to the top of the list.
            expect(region.scrollTop).toBe(40 + 300 - 8);
            expect(scrollIntoView).not.toHaveBeenCalled();

            // A list too short for that (the footer starts 60px below the list's top edge): the value and the
            // reason sit just above the footer.
            rects.row = {top: 208};
            rects.reason = {bottom: 298};
            rects.footer = {top: 260};
            region.scrollTop = 0;
            fireEvent.change(input, {target: {value: '-6'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await waitFor(() => expect(region.scrollTop).toBe(298 - 260 + 4));
            expect(scrollIntoView).not.toHaveBeenCalled();
        });

        test('an error that names no field moves nothing: neither the block\'s list nor the panel', async () => {
            scrollIntoView.mockClear();
            mockFetch({
                '/api/agent/preferences/propose/': () => json({error: {type: 'server', message: 'Could not check right now.'}}, 500),
                '/api/agent/preferences/': () => json(snapshotBody(2, [chip])),
            });
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            const form = await openEditor('sidebar');
            const region = form.closest<HTMLElement>('.priorities-scroll')!;
            region.scrollTop = 40;
            fireEvent.change(within(form).getByLabelText('Minimum base salary'), {target: {value: '150000'}});
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            expect(await screen.findByTestId('priorities-form-error')).toHaveTextContent('Could not check right now.');
            // The editor looks for an invalid field on the next frame and finds none.
            await act(async () => { await new Promise((resolve) => window.requestAnimationFrame(() => resolve(null))); });
            expect(region.scrollTop).toBe(40);
            expect(scrollIntoView).not.toHaveBeenCalled();
            expect(form.querySelector('[aria-invalid="true"]')).toBeNull();
        });

        test('a failed reset is said at the top of the open list, and under the row once the list is collapsed', async () => {
            serve();
            render(<PrioritiesSection variant="sidebar" authenticated/>);
            const toggle = await screen.findByTestId('priorities-summary-toggle');
            fireEvent.click(toggle);
            fireEvent.click(screen.getByRole('button', {name: 'Reset priorities'}));
            fireEvent.click(within(screen.getByRole('group', {name: 'Confirm reset'})).getByRole('button', {name: 'Reset priorities'}));
            const open = await screen.findByTestId('priorities-view-error');
            // Open: it scrolls with the chips instead of taking the list's room.
            const scroller = open.closest('.priorities-scroll')!;
            expect(scroller.firstElementChild).toBe(open);
            expect(within(scroller as HTMLElement).getAllByTestId('priority-chip')).toHaveLength(2);
            expect(screen.getAllByTestId('priorities-view-error')).toHaveLength(1);

            fireEvent.click(screen.getByTestId('priorities-summary-toggle'));
            const collapsed = screen.getByTestId('priorities-view-error');
            expect(collapsed.closest('.priorities-scroll')).toBeNull();
            expect(collapsed.previousElementSibling).toHaveClass('priorities-summary-row');
            expect(screen.getAllByTestId('priorities-view-error')).toHaveLength(1);
        });
    });
});
