// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.

import * as React from 'react';
import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import '@testing-library/jest-dom';
import AssistantActions, {
    STALE_MESSAGE, TurnActions, companyUrl, filterLabel, filterQuery, filtersToPatch, parseActions,
} from './AssistantActions';
import {COMPANY_OPEN_EVENT} from '../suggestCompany/controller';
import {
    getWorkspaceSnapshot, registerFilterTarget, resetWorkspaceForTests, setWorkspaceContext,
} from '../workspace/store';
import {ApiFailure} from '../priorities/api';
import * as prioritiesApi from '../priorities/api';

jest.mock('../priorities/api', () => {
    const actual = jest.requireActual('../priorities/api');
    return {...actual, proposePriorities: jest.fn(), applyProposal: jest.fn()};
});

const proposePriorities = prioritiesApi.proposePriorities as jest.Mock;
const applyProposal = prioritiesApi.applyProposal as jest.Mock;

const remote = {type: 'propose_filters', target: 'rankings', filters: {rto_policy: 'R'}} as const;
const proposal = {
    id: 'p1', scope: 'account', change_count: 1, base_revision: 4, unsupported_criteria: [],
    changes: [{path: 'work_location.modes', old: [], new: ['remote']}],
    token: {patch: {set: {'work_location.modes': ['remote']}}, scope: 'account', base_revision: 4},
};

function turn(actions: TurnActions['actions'], names?: Record<number, string>): TurnActions {
    return {actions, revision: getWorkspaceSnapshot().contextRevision, names};
}

const assign = jest.fn();
const originalLocation = window.location;

beforeEach(() => {
    resetWorkspaceForTests();
    setWorkspaceContext({surface: 'rankings'});
    proposePriorities.mockReset();
    applyProposal.mockReset();
    assign.mockReset();
    Object.defineProperty(window, 'location', {
        value: {...originalLocation, assign}, writable: true, configurable: true,
    });
});

afterEach(() => {
    Object.defineProperty(window, 'location', {value: originalLocation, writable: true, configurable: true});
    document.getElementById('organization-list')?.remove();
});

describe('parseActions', () => {
    test('keeps valid actions and caps the list at three', () => {
        const open = {type: 'open_company', organization_id: 7};
        expect(parseActions([open, remote, {...remote}, open])).toEqual([open, remote, remote]);
    });

    test('drops hostile, unknown and unadvertised shapes', () => {
        const hostile: unknown[] = [
            null, 'text', 4, [],
            {type: 'compare_companies', organization_ids: [1, 2]},
            {type: 'navigate', url: 'javascript:alert(1)'},
            {type: 'open_company', organization_id: '7'},
            {type: 'open_company', organization_id: 0},
            {type: 'open_company', organization_id: 1.5},
            {type: 'open_company', organization_id: 2 ** 31},
            {type: 'open_company', organization_id: 7, url: '//evil.example'},
            {type: 'propose_filters', target: 'jobs', filters: {rto_policy: 'R'}},
            {type: 'propose_filters', target: 'rankings', filters: {}},
            {type: 'propose_filters', target: 'rankings', filters: {rto_policy: 'X'}},
            {type: 'propose_filters', target: 'rankings', filters: {rto_policy: 5}},
            {type: 'propose_filters', target: 'rankings', filters: {accelerated_vesting: false}},
            {type: 'propose_filters', target: 'rankings', filters: {rto_policy: 'R', page: 2}},
            {type: 'propose_filters', target: 'rankings', filters: null},
            {type: 'propose_filters', target: 'rankings', filters: {rto_policy: 'R'}, href: '/x'},
        ];
        // The list is capped at three, so each entry is parsed on its own.
        hostile.forEach((item) => expect(parseActions([item])).toEqual([]));
        expect(parseActions(undefined)).toEqual([]);
        expect(parseActions({type: 'open_company', organization_id: 7})).toEqual([]);
    });

    test('a missing target defaults to rankings and both filters may combine', () => {
        expect(parseActions([{type: 'propose_filters', filters: {rto_policy: 'H', accelerated_vesting: true}}]))
            .toEqual([{type: 'propose_filters', target: 'rankings', filters: {rto_policy: 'H', accelerated_vesting: true}}]);
    });
});

describe('URL, label and patch builders use enums and ids only', () => {
    test('filterQuery', () => {
        expect(filterQuery(remote)).toBe('rto=R');
        expect(filterQuery({...remote, filters: {accelerated_vesting: true}})).toBe('accelerated_vesting=1');
        expect(filterQuery({...remote, filters: {rto_policy: 'O', accelerated_vesting: true}}))
            .toBe('rto=O&accelerated_vesting=1');
        expect(filterQuery({...remote, filters: {}})).toBe('');
    });

    test('companyUrl', () => {
        expect(companyUrl(42)).toBe('/?company=42');
    });

    test('filterLabel covers each combination and the applied state', () => {
        expect(filterLabel(remote)).toBe('Apply remote filter');
        expect(filterLabel(remote, true)).toBe('Remote filter applied');
        expect(filterLabel({...remote, filters: {rto_policy: 'O'}})).toBe('Apply in-office filter');
        expect(filterLabel({...remote, filters: {accelerated_vesting: true}})).toBe('Apply early vesting filter');
        expect(filterLabel({...remote, filters: {accelerated_vesting: true}}, true)).toBe('Early vesting filter applied');
        expect(filterLabel({...remote, filters: {rto_policy: 'H', accelerated_vesting: true}})).toBe('Apply filters');
        expect(filterLabel({...remote, filters: {rto_policy: 'H', accelerated_vesting: true}}, true)).toBe('Filters applied');
    });

    test('filtersToPatch maps only the two supported paths', () => {
        expect(filtersToPatch({...remote, filters: {rto_policy: 'H'}})).toEqual({set: {'work_location.modes': ['hybrid']}});
        expect(filtersToPatch({...remote, filters: {rto_policy: 'O'}})).toEqual({set: {'work_location.modes': ['in-office']}});
        expect(filtersToPatch({...remote, filters: {rto_policy: 'R', accelerated_vesting: true}})).toEqual({
            set: {'work_location.modes': ['remote'], 'vesting.prefer_accelerated': true},
        });
    });
});

describe('AssistantActions', () => {
    test('renders nothing without actions', () => {
        const {container, rerender} = render(<AssistantActions turn={undefined} />);
        expect(container).toBeEmptyDOMElement();
        rerender(<AssistantActions turn={turn([])} />);
        expect(container).toBeEmptyDOMElement();
    });

    test('a filter action is an explicit button; nothing happens until it is pressed', () => {
        const target = jest.fn(() => true);
        registerFilterTarget(target);
        render(<AssistantActions turn={turn([remote])} />);
        expect(target).not.toHaveBeenCalled();
        const button = screen.getByRole('button', {name: 'Apply remote filter'});
        expect(button).toHaveAttribute('aria-disabled', 'false');
        fireEvent.click(button);
        expect(target).toHaveBeenCalledWith({rtoPolicy: 'R'});
        expect(assign).not.toHaveBeenCalled();
        expect(screen.getByRole('button', {name: 'Remote filter applied'})).toHaveAttribute('aria-disabled', 'true');
        // A second press on the applied button does nothing.
        fireEvent.click(screen.getByRole('button', {name: 'Remote filter applied'}));
        expect(target).toHaveBeenCalledTimes(1);
    });

    test('pressing an action keeps keyboard focus on the same button', () => {
        registerFilterTarget(() => true);
        render(<AssistantActions turn={turn([remote])} />);
        const button = screen.getByRole('button', {name: 'Apply remote filter'});
        button.focus();
        fireEvent.click(button);
        expect(document.activeElement).toBe(screen.getByRole('button', {name: 'Remote filter applied'}));
    });

    test('with no rankings list mounted a filter action navigates using allowlisted params only', () => {
        render(<AssistantActions turn={turn([{...remote, filters: {rto_policy: 'H', accelerated_vesting: true}}])} />);
        fireEvent.click(screen.getByRole('button', {name: 'Apply filters'}));
        expect(assign).toHaveBeenCalledWith('/?rto=H&accelerated_vesting=1');
    });

    test('a target that declines falls back to navigation and the button stays unapplied', () => {
        registerFilterTarget(() => false);
        render(<AssistantActions turn={turn([remote])} />);
        fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
        expect(assign).toHaveBeenCalledWith('/?rto=R');
        expect(screen.getByRole('button', {name: 'Apply remote filter'})).toBeInTheDocument();
    });

    test('open_company dispatches the in-page event when the list is mounted', () => {
        const marker = document.createElement('div');
        marker.id = 'organization-list';
        document.body.appendChild(marker);
        const heard = jest.fn();
        window.addEventListener(COMPANY_OPEN_EVENT, heard);
        render(<AssistantActions turn={turn([{type: 'open_company', organization_id: 9}], {9: 'Acme'})} />);
        fireEvent.click(screen.getByRole('button', {name: 'Open Acme'}));
        window.removeEventListener(COMPANY_OPEN_EVENT, heard);
        expect(heard).toHaveBeenCalledTimes(1);
        expect((heard.mock.calls[0][0] as CustomEvent).detail).toEqual({organizationId: 9});
        expect(assign).not.toHaveBeenCalled();
        expect(screen.getByRole('button', {name: 'Opened Acme'})).toHaveAttribute('aria-disabled', 'true');
    });

    test('open_company navigates from other pages and falls back to a generic label', () => {
        render(<AssistantActions turn={turn([{type: 'open_company', organization_id: 9}])} />);
        fireEvent.click(screen.getByRole('button', {name: 'Open company details'}));
        expect(assign).toHaveBeenCalledWith('/?company=9');
    });

    test('an unnamed open_company reads "Opened company" once applied in-page', () => {
        const marker = document.createElement('div');
        marker.id = 'organization-list';
        document.body.appendChild(marker);
        render(<AssistantActions turn={turn([{type: 'open_company', organization_id: 9}])} />);
        fireEvent.click(screen.getByRole('button', {name: 'Open company details'}));
        expect(screen.getByRole('button', {name: 'Opened company'})).toBeInTheDocument();
    });

    test('a reply for an earlier view is disabled with a visible reason and cannot act', () => {
        const target = jest.fn(() => true);
        registerFilterTarget(target);
        const answered = turn([remote]);
        render(<AssistantActions turn={answered} />);
        const region = screen.getByTestId('assistant-actions-stale');
        expect(region).toHaveTextContent('');
        act(() => {
            setWorkspaceContext({surface: 'rankings', page: 2});
        });
        const button = screen.getByRole('button', {name: 'Apply remote filter'});
        expect(button).toHaveAttribute('aria-disabled', 'true');
        expect(region).toHaveTextContent(STALE_MESSAGE);
        fireEvent.click(button);
        expect(target).not.toHaveBeenCalled();
        expect(assign).not.toHaveBeenCalled();
    });

    test('the click-time check blocks a context that moved after the last render', () => {
        const target = jest.fn(() => true);
        registerFilterTarget(target);
        render(<AssistantActions turn={turn([remote])} />);
        const button = screen.getByRole('button', {name: 'Apply remote filter'});
        // Move the context without letting React re-render before the click.
        setWorkspaceContext({surface: 'rankings', page: 3});
        fireEvent.click(button);
        expect(target).not.toHaveBeenCalled();
    });

    test('applied actions never show the stale message', () => {
        registerFilterTarget(() => true);
        render(<AssistantActions turn={turn([remote])} />);
        fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
        act(() => {
            setWorkspaceContext({surface: 'rankings', page: 2});
        });
        expect(screen.getByTestId('assistant-actions-stale')).toHaveTextContent('');
    });

    describe('save as a lasting requirement', () => {
        const applyFilter = async () => {
            registerFilterTarget(() => true);
            render(<AssistantActions turn={turn([remote])} />);
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
        };

        test('the follow-up appears only after the filter is applied', async () => {
            registerFilterTarget(() => true);
            render(<AssistantActions turn={turn([remote])} />);
            expect(screen.queryByTestId('assistant-action-save')).not.toBeInTheDocument();
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
            expect(screen.getByTestId('assistant-action-save')).toBeInTheDocument();
        });

        test('propose, review, apply and record the new revision', async () => {
            await applyFilter();
            let resolve: (value: unknown) => void = () => undefined;
            proposePriorities.mockReturnValue(new Promise((r) => { resolve = r; }));
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            expect(screen.getByText('Preparing the change for review…')).toBeInTheDocument();
            expect(proposePriorities).toHaveBeenCalledWith(
                {set: {'work_location.modes': ['remote']}}, 'account', expect.any(AbortSignal));
            await act(async () => { resolve(proposal); });
            expect(await screen.findByTestId('assistant-action-review')).toBeInTheDocument();
            applyProposal.mockResolvedValue({revision: 5, changes: [], undo: null, scope: 'account'});
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            expect(await screen.findByTestId('assistant-action-saved')).toHaveTextContent('Saved to your account.');
            expect(applyProposal).toHaveBeenCalledWith(proposal.token, expect.any(AbortSignal));
            expect(getWorkspaceSnapshot().prioritiesRevision).toBe(5);
        });

        test('a null revision from the server leaves the stored one alone', async () => {
            await applyFilter();
            proposePriorities.mockResolvedValue(proposal);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            await screen.findByTestId('assistant-action-review');
            const before = getWorkspaceSnapshot().prioritiesRevision;
            applyProposal.mockResolvedValue({revision: null, changes: [], undo: null, scope: 'account'});
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            await screen.findByTestId('assistant-action-saved');
            expect(getWorkspaceSnapshot().prioritiesRevision).toBe(before);
        });

        test('cancel writes nothing and offers the follow-up again', async () => {
            await applyFilter();
            proposePriorities.mockResolvedValue(proposal);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            await screen.findByTestId('assistant-action-review');
            fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
            expect(applyProposal).not.toHaveBeenCalled();
            expect(screen.queryByTestId('assistant-action-review')).not.toBeInTheDocument();
            expect(screen.getByTestId('assistant-action-save')).toBeInTheDocument();
        });

        test('a propose failure shows an inline error with Try again', async () => {
            await applyFilter();
            proposePriorities.mockRejectedValueOnce(new ApiFailure(400, 'invalid', 'Server said no'));
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            expect(await screen.findByTestId('assistant-action-error')).toHaveTextContent('Server said no');
            proposePriorities.mockRejectedValueOnce(new Error('network'));
            fireEvent.click(screen.getByRole('button', {name: 'Try again'}));
            await waitFor(() => expect(screen.getByTestId('assistant-action-error'))
                .toHaveTextContent(prioritiesApi.GENERIC_ERROR_MESSAGE));
            proposePriorities.mockResolvedValueOnce(proposal);
            fireEvent.click(screen.getByRole('button', {name: 'Try again'}));
            expect(await screen.findByTestId('assistant-action-review')).toBeInTheDocument();
        });

        test('an apply failure stays in the review, and a stale one offers Review latest', async () => {
            await applyFilter();
            proposePriorities.mockResolvedValue(proposal);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            await screen.findByTestId('assistant-action-review');
            applyProposal.mockRejectedValueOnce(new Error('boom'));
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            expect(await screen.findByTestId('priorities-review-error')).toHaveTextContent(prioritiesApi.GENERIC_ERROR_MESSAGE);
            const stale = new ApiFailure(409, 'preference_stale', 'Your priorities changed.', {}, 6);
            applyProposal.mockRejectedValueOnce(stale);
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            const latest = await screen.findByRole('button', {name: 'Review latest'});
            proposePriorities.mockClear();
            proposePriorities.mockResolvedValueOnce(proposal);
            fireEvent.click(latest);
            await waitFor(() => expect(proposePriorities).toHaveBeenCalledTimes(1));
        });

        test('abort on unmount while applying leaves the review untouched', async () => {
            registerFilterTarget(() => true);
            const {unmount} = render(<AssistantActions turn={turn([remote])} />);
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
            proposePriorities.mockResolvedValue(proposal);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            await screen.findByTestId('assistant-action-review');
            let signal: AbortSignal | undefined;
            applyProposal.mockImplementation((_token, s: AbortSignal) => {
                signal = s;
                return new Promise((_resolve, reject) => {
                    s.addEventListener('abort', () => reject(new Error('aborted')));
                });
            });
            fireEvent.click(screen.getByRole('button', {name: 'Apply to account'}));
            unmount();
            await act(async () => { await Promise.resolve(); });
            expect(signal?.aborted).toBe(true);
        });

        test('abort on unmount is signalled', async () => {
            registerFilterTarget(() => true);
            const {unmount} = render(<AssistantActions turn={turn([remote])} />);
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
            let signal: AbortSignal | undefined;
            proposePriorities.mockImplementation((_patch, _scope, s: AbortSignal) => {
                signal = s;
                return new Promise((_resolve, reject) => {
                    s.addEventListener('abort', () => reject(new Error('aborted')));
                });
            });
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            unmount();
            expect(signal?.aborted).toBe(true);
        });
    });
});
