// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import * as React from 'react';
import {act, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import PrioritiesSection, {SidebarPriorities} from './PrioritiesSection';
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
        expect(screen.getByRole('status')).toHaveTextContent('1 change to review');
        expect(screen.getByText('Compensation › Minimum base salary', {exact: false})).toBeInTheDocument();
    });

    test('a propose failure without field errors shows the server message; network error too', async () => {
        let mode: 'msg' | 'net' = 'msg';
        mockFetch({
            '/api/agent/preferences/propose/': () => {
                if (mode === 'net') throw new Error('x');
                return json({error: {message: 'No can do'}}, 400);
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
        expect(await screen.findByText('No can do')).toBeInTheDocument();
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
            expect(await screen.findAllByTestId('priority-chip')).toHaveLength(1);
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
        expect(await screen.findAllByTestId('priority-chip')).toHaveLength(1);
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
            fireEvent.click(screen.getByRole('button', {name: 'Review changes'}));
            await screen.findByRole('heading', {name: 'Review your changes'});
            const first = bodies.length;
            current = snapshotBody(5, [chip], {preferences: {}});
            current.fields[0] = field({value: 200000, set: true});
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            await screen.findByTestId('priorities-review-error');
            fireEvent.click(screen.getByRole('button', {name: 'Review latest'}));
            await waitFor(() => expect(bodies.length).toBe(first + 1));
            expect(bodies[first].base_revision ?? bodies[first].expected_revision ?? 5).toBe(5);
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
});
