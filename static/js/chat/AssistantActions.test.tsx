// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.

import * as React from 'react';
import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import '@testing-library/jest-dom';
import AssistantActions, {
    STALE_MESSAGE, TurnActions, matchesReplyView, companyUrl, filterLabel, filterQuery, filtersToPatch, parseActions,
} from './AssistantActions';
import {
    clearWorkspaceContext, getWorkspaceSnapshot, registerCompanyTarget, registerFilterTarget, resetWorkspaceForTests, setPrioritiesRevision, setWorkspaceAccount, setWorkspaceContext,
} from '../workspace/store';
import {ApiFailure} from '../priorities/api';
import * as prioritiesApi from '../priorities/api';

jest.mock('../priorities/api', () => {
    const actual = jest.requireActual('../priorities/api');
    return {...actual, proposePriorities: jest.fn(), applyProposal: jest.fn(), undoApplied: jest.fn()};
});

const proposePriorities = prioritiesApi.proposePriorities as jest.Mock;
const applyProposal = prioritiesApi.applyProposal as jest.Mock;
const undoApplied = prioritiesApi.undoApplied as jest.Mock;

const remote = {type: 'propose_filters', target: 'rankings', filters: {rto_policy: 'R'}} as const;
const proposal = {
    id: 'p1', scope: 'account', change_count: 1, base_revision: 4, unsupported_criteria: [],
    changes: [{path: 'work_location.modes', old: [], new: ['remote']}],
    token: {patch: {set: {'work_location.modes': ['remote']}}, scope: 'account', base_revision: 4},
};

function turn(actions: TurnActions['actions'], names?: Record<number, string>): TurnActions {
    return {actions, revision: getWorkspaceSnapshot().contextRevision, names, context: getWorkspaceSnapshot().context};
}

const assign = jest.fn();
const originalLocation = window.location;
const revealed: Array<{el: HTMLElement; arg: unknown}> = [];

beforeEach(() => {
    resetWorkspaceForTests();
    setWorkspaceContext({surface: 'rankings'});
    proposePriorities.mockReset();
    proposePriorities.mockRejectedValue(new Error('offline'));
    applyProposal.mockReset();
    undoApplied.mockReset();
    assign.mockReset();
    revealed.length = 0;
    Element.prototype.scrollIntoView = function (this: HTMLElement, arg?: boolean | ScrollIntoViewOptions) {
        revealed.push({el: this, arg});
    };
    Object.defineProperty(window, 'location', {
        value: {...originalLocation, assign}, writable: true, configurable: true,
    });
});

afterEach(() => {
    delete (Element.prototype as {scrollIntoView?: unknown}).scrollIntoView;
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

describe('matchesReplyView', () => {
    const base = {surface: 'company', organizationId: 12, organizationName: 'Acme', filters: {}, page: 3} as const;
    const none = {filters: {}, companies: new Set<number>()};

    test('needs both a live and an original context', () => {
        expect(matchesReplyView(null, base, none)).toBe(false);
        expect(matchesReplyView({...base}, null, none)).toBe(false);
        expect(matchesReplyView({...base}, undefined, none)).toBe(false);
    });

    test('the original context matches; any other entity does not unless the reply opened it', () => {
        expect(matchesReplyView({...base}, base, none)).toBe(true);
        const other = {...base, organizationId: 7, organizationName: 'Other'};
        expect(matchesReplyView(other, base, none)).toBe(false);
        expect(matchesReplyView(other, base, {...none, companies: new Set([7])})).toBe(true);
        expect(matchesReplyView({surface: 'rankings', filters: {}, page: 3}, base, none)).toBe(false);
        expect(matchesReplyView({surface: 'rankings', filters: {}, page: 3}, base, {...none, companies: new Set([12])})).toBe(true);
    });

    test('an opened company does not excuse a job, a comparison or another surface', () => {
        const opened = {...none, companies: new Set([7])};
        expect(matchesReplyView({...base, organizationId: 7, jobId: 4}, base, opened)).toBe(false);
        expect(matchesReplyView({...base, organizationId: 7, comparisonIds: [7, 8]}, base, opened)).toBe(false);
        expect(matchesReplyView({surface: 'jobs', filters: {}, page: 3}, base, opened)).toBe(false);
    });

    test('applied filters, and the page reset they bring, are the reply\'s own; other values are not', () => {
        const applied = {filters: {rtoPolicy: 'R' as const}, companies: new Set<number>()};
        expect(matchesReplyView({...base, filters: {rtoPolicy: 'R'}, page: 1}, base, applied)).toBe(true);
        expect(matchesReplyView({...base, filters: {rtoPolicy: 'R'}, page: undefined}, base, applied)).toBe(true);
        expect(matchesReplyView({...base, filters: {rtoPolicy: 'R'}, page: 5}, base, applied)).toBe(false);
        expect(matchesReplyView({...base, filters: {rtoPolicy: 'O'}, page: 1}, base, applied)).toBe(false);
        expect(matchesReplyView({...base, filters: {rtoPolicy: 'R'}, page: 3}, base, none)).toBe(false);
        expect(matchesReplyView({...base, filters: {acceleratedVesting: true}}, base, none)).toBe(false);
        expect(matchesReplyView({...base, searchTerm: 'x'}, base, none)).toBe(false);
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

    test('open_company opens through the typed company target when the list is mounted', () => {
        const open = jest.fn(() => true);
        const release = registerCompanyTarget(open);
        render(<AssistantActions turn={turn([{type: 'open_company', organization_id: 9}], {9: 'Acme'})} />);
        fireEvent.click(screen.getByRole('button', {name: 'Open Acme'}));
        release();
        expect(open).toHaveBeenCalledWith(9);
        expect(assign).not.toHaveBeenCalled();
        expect(screen.getByRole('button', {name: 'Opened Acme'})).toHaveAttribute('aria-disabled', 'true');
        expect(screen.queryByTestId('assistant-action-not-in-list')).not.toBeInTheDocument();
    });

    test('a company missing from the ranked list says so and never claims it opened', () => {
        const release = registerCompanyTarget(() => false);
        render(<AssistantActions turn={turn([{type: 'open_company', organization_id: 9}], {9: 'Acme'})} />);
        fireEvent.click(screen.getByRole('button', {name: 'Open Acme'}));
        release();
        expect(screen.getByTestId('assistant-action-not-in-list')).toHaveTextContent("isn't in the ranked list");
        expect(screen.getByRole('button', {name: 'Open Acme'})).toHaveAttribute('aria-disabled', 'false');
        expect(screen.queryByRole('button', {name: 'Opened Acme'})).not.toBeInTheDocument();
        expect(assign).not.toHaveBeenCalled();
    });

    test('the not-in-list note clears once the company does open', () => {
        let present = false;
        const release = registerCompanyTarget(() => present);
        render(<AssistantActions turn={turn([{type: 'open_company', organization_id: 9}], {9: 'Acme'})} />);
        fireEvent.click(screen.getByRole('button', {name: 'Open Acme'}));
        expect(screen.getByTestId('assistant-action-not-in-list')).toBeInTheDocument();
        present = true;
        fireEvent.click(screen.getByRole('button', {name: 'Open Acme'}));
        release();
        expect(screen.queryByTestId('assistant-action-not-in-list')).not.toBeInTheDocument();
        expect(screen.getByRole('button', {name: 'Opened Acme'})).toBeInTheDocument();
    });

    test('open_company navigates from other pages and falls back to a generic label', () => {
        render(<AssistantActions turn={turn([{type: 'open_company', organization_id: 9}])} />);
        fireEvent.click(screen.getByRole('button', {name: 'Open company details'}));
        expect(assign).toHaveBeenCalledWith('/?company=9');
    });

    test('an unnamed open_company reads "Opened company" once applied in-page', () => {
        const release = registerCompanyTarget(() => true);
        render(<AssistantActions turn={turn([{type: 'open_company', organization_id: 9}])} />);
        fireEvent.click(screen.getByRole('button', {name: 'Open company details'}));
        release();
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

    describe('a suggestion turning stale scrolls its note into view', () => {
        const mountInLog = (bubbles: number, buttonBottom: number) => {
            registerFilterTarget(() => true);
            const log = document.createElement('div');
            log.setAttribute('role', 'log');
            log.innerHTML = '<div class="chat-bubble-assistant"></div>'.repeat(bubbles);
            document.body.appendChild(log);
            const bubble = log.firstElementChild as HTMLElement;
            const spy = jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
                if (this === log) return {top: 0, bottom: 100} as DOMRect;
                if (this.classList.contains('assistant-action-btn')) return {top: 0, bottom: buttonBottom} as DOMRect;
                return {top: 50, bottom: 180} as DOMRect;
            });
            render(<AssistantActions turn={turn([remote])} />, {container: bubble.appendChild(document.createElement('div'))});
            act(() => {
                setWorkspaceContext({surface: 'rankings', page: 2});
            });
            return {log, done: () => { spy.mockRestore(); log.remove(); }};
        };

        test('follows the newest bubble when its action row was in view', () => {
            const {done} = mountInLog(1, 60);
            expect(revealed).toHaveLength(1);
            expect(revealed[0].el).toBe(screen.getByTestId('assistant-actions-stale'));
            expect(revealed[0].arg).toEqual({block: 'nearest', behavior: 'instant'});
            done();
        });

        test('leaves a reader who scrolled up alone', () => {
            const {done} = mountInLog(1, 160);
            expect(revealed).toHaveLength(0);
            done();
        });

        test('leaves an older bubble alone', () => {
            const {done} = mountInLog(2, 60);
            expect(revealed).toHaveLength(0);
            done();
        });
    });

    describe('the reveal follows a card that is still growing', () => {
        const original = globalThis.ResizeObserver;
        let observers: Array<{callback: () => void; observe: jest.Mock; disconnect: jest.Mock}>;
        let spy: jest.SpyInstance;
        let log: HTMLElement;
        let view: ReturnType<typeof render>;

        beforeEach(() => {
            jest.useFakeTimers();
            observers = [];
            globalThis.ResizeObserver = class {
                observe = jest.fn();
                disconnect = jest.fn();
                constructor(public callback: () => void) {
                    observers.push(this);
                }
            } as unknown as typeof ResizeObserver;
            registerFilterTarget(() => true);
            log = document.createElement('div');
            log.setAttribute('role', 'log');
            log.innerHTML = '<div class="chat-bubble-assistant"></div>';
            document.body.appendChild(log);
            spy = jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
                if (this === log) return {top: 0, bottom: 100} as DOMRect;
                if (this.classList.contains('assistant-action-btn')) return {top: 0, bottom: 60} as DOMRect;
                return {top: 50, bottom: 180} as DOMRect;
            });
            view = render(<AssistantActions turn={turn([remote])} />, {
                container: (log.firstElementChild as HTMLElement).appendChild(document.createElement('div')),
            });
            act(() => {
                setWorkspaceContext({surface: 'rankings', page: 2});
            });
        });

        afterEach(() => {
            jest.useRealTimers();
            globalThis.ResizeObserver = original;
            spy.mockRestore();
            log.remove();
        });

        test('re-applies the reveal when the bubble grows after the first scroll', () => {
            expect(revealed).toHaveLength(1);
            expect(observers[0].observe).toHaveBeenCalledWith(log.firstElementChild);
            observers[0].callback();
            expect(revealed).toHaveLength(2);
        });

        test.each(['wheel', 'touchstart', 'keydown', 'pointerdown', 'focusin'])('stops following on the reader\'s %s', (name) => {
            log.dispatchEvent(new Event(name));
            expect(observers[0].disconnect).toHaveBeenCalled();
        });

        test('stops following when the reader scrolls, but not for its own scroll', () => {
            log.dispatchEvent(new Event('scroll'));
            expect(observers[0].disconnect).not.toHaveBeenCalled();
            log.scrollTop = 40;
            log.dispatchEvent(new Event('scroll'));
            expect(observers[0].disconnect).toHaveBeenCalled();
        });

        test('keeps following while the card grows however long that takes', () => {
            act(() => {
                jest.advanceTimersByTime(10_000);
            });
            expect(observers[0].disconnect).not.toHaveBeenCalled();
            observers[0].callback();
            expect(revealed).toHaveLength(2);
        });

        test('stops following once the reply has been scrolled out of view', () => {
            spy.mockImplementation(function (this: Element) {
                return (this === log ? {top: 0, bottom: 100} : {top: -300, bottom: -150}) as DOMRect;
            });
            observers[0].callback();
            expect(observers[0].disconnect).toHaveBeenCalled();
            expect(revealed).toHaveLength(1);
        });

        test('stops following when the reply unmounts', () => {
            view.unmount();
            expect(observers[0].disconnect).toHaveBeenCalled();
        });

        test('works without ResizeObserver', () => {
            delete (globalThis as {ResizeObserver?: unknown}).ResizeObserver;
            act(() => {
                setWorkspaceContext({surface: 'rankings', page: 5});
            });
            expect(observers).toHaveLength(1);
        });
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
            fireEvent.click(screen.getByRole('button', {name: 'Save'}));
            expect(await screen.findByTestId('assistant-action-saved')).toHaveTextContent('Saved to your account.');
            expect(applyProposal).toHaveBeenCalledWith(proposal.token, expect.any(AbortSignal));
            expect(getWorkspaceSnapshot().prioritiesRevision).toBe(5);
        });

        test('the review card names the field as the chip does', async () => {
            await applyFilter();
            proposePriorities.mockResolvedValue(proposal);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            const review = await screen.findByTestId('assistant-action-review');
            expect(review).toHaveTextContent('Work arrangement');
            expect(review).not.toHaveTextContent('Work location');
        });

        test('a null revision from the server leaves the stored one alone', async () => {
            await applyFilter();
            proposePriorities.mockResolvedValue(proposal);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            await screen.findByTestId('assistant-action-review');
            const before = getWorkspaceSnapshot().prioritiesRevision;
            applyProposal.mockResolvedValue({revision: null, changes: [], undo: null, scope: 'account'});
            fireEvent.click(screen.getByRole('button', {name: 'Save'}));
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
                .toHaveTextContent("Couldn't prepare the change for review."));
            proposePriorities.mockResolvedValueOnce(proposal);
            fireEvent.click(screen.getByRole('button', {name: 'Try again'}));
            expect(await screen.findByTestId('assistant-action-review')).toBeInTheDocument();
        });

        test('opening the review or an error reveals its last control, not the whole bubble', async () => {
            await applyFilter();
            const log = screen.getByTestId('assistant-actions').parentElement as HTMLElement;
            log.setAttribute('role', 'log');
            const spy = jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
                return (this === log ? {top: 100, bottom: 300} : {top: 150, bottom: 250}) as DOMRect;
            });
            revealed.length = 0;
            proposePriorities.mockRejectedValueOnce(new Error('network'));
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            await screen.findByTestId('assistant-action-error');
            expect(revealed[revealed.length - 1].el).toBe(screen.getByRole('button', {name: 'Try again'}));
            proposePriorities.mockResolvedValueOnce(proposal);
            fireEvent.click(screen.getByRole('button', {name: 'Try again'}));
            await screen.findByTestId('assistant-action-review');
            expect(revealed[revealed.length - 1].el).toBe(screen.getByRole('button', {name: 'Cancel'}));
            applyProposal.mockRejectedValueOnce(new Error('boom'));
            fireEvent.click(screen.getByRole('button', {name: 'Save'}));
            await screen.findByTestId('priorities-review-error');
            expect(revealed[revealed.length - 1].el.tagName).toBe('BUTTON');
            spy.mockRestore();
        });

        test('a focused card heading pushed above the view is scrolled back when it fits with the control', async () => {
            await applyFilter();
            const log = screen.getByTestId('assistant-actions').parentElement as HTMLElement;
            log.setAttribute('role', 'log');
            log.scrollTop = 100;
            const rects = jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
                if (this === log) return {top: 100, bottom: 300} as DOMRect;
                if (this.tagName === 'H3') return {top: 80, bottom: 100} as DOMRect;
                return {top: 200, bottom: 260} as DOMRect;
            });
            proposePriorities.mockResolvedValueOnce(proposal);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            await screen.findByTestId('assistant-action-review');
            expect(document.activeElement?.tagName).toBe('H3');
            expect(log.scrollTop).toBe(80);
            rects.mockRestore();
        });

        test('a pinned bar over the control scrolls the transcript on by the overlap', async () => {
            await applyFilter();
            const log = screen.getByTestId('assistant-actions').parentElement as HTMLElement;
            log.setAttribute('role', 'log');
            const bar = document.createElement('div');
            document.body.appendChild(bar);
            const rects = jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
                if (this === log) return {top: 100, bottom: 300} as DOMRect;
                if (this === bar) return {top: 240, bottom: 300} as DOMRect;
                return {top: 200, bottom: 260, left: 10, width: 100} as DOMRect;
            });
            const hit = jest.fn(() => bar);
            Object.defineProperty(document, 'elementFromPoint', {value: hit, configurable: true});
            proposePriorities.mockResolvedValueOnce(proposal);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            await screen.findByTestId('assistant-action-review');
            expect(hit).toHaveBeenCalled();
            expect(log.scrollTop).toBeGreaterThanOrEqual(28);
            hit.mockReturnValue(null as never);
            log.scrollTop = 0;
            await act(async () => { setWorkspaceContext({page: 9}); });
            delete (document as {elementFromPoint?: unknown}).elementFromPoint;
            rects.mockRestore();
            bar.remove();
        });

        test('a prefetched proposal with no changes replaces the Save button with the already note', async () => {
            registerFilterTarget(() => true);
            proposePriorities.mockResolvedValueOnce({...proposal, changes: []});
            render(<AssistantActions turn={turn([remote])} />);
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
            expect(await screen.findByTestId('assistant-action-already')).toHaveTextContent('Already one of your requirements.');
            expect(screen.queryByTestId('assistant-action-save')).not.toBeInTheDocument();
        });

        test('a prefetched proposal opens the review without another request', async () => {
            registerFilterTarget(() => true);
            proposePriorities.mockResolvedValueOnce(proposal);
            render(<AssistantActions turn={turn([remote])} />);
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
            await waitFor(() => expect(proposePriorities).toHaveBeenCalledTimes(1));
            await act(async () => undefined);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            expect(screen.getByTestId('assistant-action-review')).toBeInTheDocument();
            expect(proposePriorities).toHaveBeenCalledTimes(1);
        });

        test('a proposal with no changes says it is already a requirement', async () => {
            await applyFilter();
            proposePriorities.mockResolvedValueOnce({...proposal, changes: []});
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            expect(await screen.findByTestId('assistant-action-already')).toHaveTextContent('Already one of your requirements.');
            expect(screen.queryByTestId('assistant-action-review')).not.toBeInTheDocument();
        });

        test('an apply failure stays in the review, and a stale one offers Review latest', async () => {
            await applyFilter();
            proposePriorities.mockResolvedValue(proposal);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            await screen.findByTestId('assistant-action-review');
            applyProposal.mockRejectedValueOnce(new Error('boom'));
            fireEvent.click(screen.getByRole('button', {name: 'Save'}));
            expect(await screen.findByTestId('priorities-review-error')).toHaveTextContent(prioritiesApi.GENERIC_ERROR_MESSAGE);
            const stale = new ApiFailure(409, 'preference_stale', 'Your priorities changed.', {}, 6);
            applyProposal.mockRejectedValueOnce(stale);
            fireEvent.click(screen.getByRole('button', {name: 'Save'}));
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
            fireEvent.click(screen.getByRole('button', {name: 'Save'}));
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

describe('per-reply staleness (issue #484 review)', () => {
    const open12 = {type: 'open_company', organization_id: 12} as const;
    // Like OrganizationList: the target applies the change and reports the new context.
    const registerRankings = () => registerFilterTarget((filters) => {
        setWorkspaceContext({filters});
        return true;
    });

    test('applying one action leaves a sibling action of the same reply enabled', () => {
        registerRankings();
        const release = registerCompanyTarget(() => {
            setWorkspaceContext({surface: 'company', organizationId: 12});
            return true;
        });
        render(<AssistantActions turn={turn([remote, open12], {12: 'Acme'})} />);
        fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
        expect(screen.getByRole('button', {name: 'Open Acme'})).toHaveAttribute('aria-disabled', 'false');
        expect(screen.getByTestId('assistant-actions-stale')).toHaveTextContent('');
        fireEvent.click(screen.getByRole('button', {name: 'Open Acme'}));
        release();
        expect(screen.getByRole('button', {name: 'Opened Acme'})).toBeInTheDocument();
        expect(screen.getByRole('button', {name: 'Remote filter applied'})).toBeInTheDocument();
        expect(screen.getByTestId('assistant-actions-stale')).toHaveTextContent('');
    });

    test('a change the reply did not cause still makes the sibling stale', () => {
        registerRankings();
        let now = 1_000_000;
        const clock = jest.spyOn(Date, 'now').mockImplementation(() => now);
        render(<AssistantActions turn={turn([remote, open12], {12: 'Acme'})} />);
        fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
        now += 5000;
        act(() => {
            setWorkspaceContext({page: 4});
        });
        clock.mockRestore();
        expect(screen.getByRole('button', {name: 'Open Acme'})).toHaveAttribute('aria-disabled', 'true');
        expect(screen.getByTestId('assistant-actions-stale')).toHaveTextContent(STALE_MESSAGE);
    });

    // Like OrganizationList: opening reports the dialog's context (after a fetch,
    // so after any fixed delay) and closing reports the list without the company.
    describe('open, close, then apply (click order never strands a sibling)', () => {
        const asked = {surface: 'company', organizationId: 12, organizationName: 'Acme', filters: {}, page: 3} as const;
        let slow: (() => void) | null;
        beforeEach(() => {
            slow = null;
            setWorkspaceContext(asked);
            registerFilterTarget((filters) => {
                setWorkspaceContext({filters, page: 1});
                return true;
            });
        });

        const openThenClose = (delayed: boolean) => {
            const release = registerCompanyTarget(() => {
                const commit = () => setWorkspaceContext({surface: 'company', organizationId: 12, organizationName: 'Acme'});
                if (delayed) {
                    slow = commit;
                } else {
                    commit();
                }
                return true;
            });
            render(<AssistantActions turn={turn([remote, open12], {12: 'Acme'})} />);
            fireEvent.click(screen.getByRole('button', {name: 'Open Acme'}));
            return release;
        };

        const closeDialog = () => act(() => {
            clearWorkspaceContext();
            setWorkspaceContext({surface: 'rankings'});
        });

        test.each([[false], [true]])('Open, close the dialog, then Apply stays enabled (slow fetch: %s)', (delayed) => {
            const release = openThenClose(delayed);
            if (slow) {
                act(() => slow!());
            }
            closeDialog();
            release();
            const apply = screen.getByRole('button', {name: 'Apply remote filter'});
            expect(apply).toHaveAttribute('aria-disabled', 'false');
            expect(screen.getByTestId('assistant-actions-stale')).toHaveTextContent('');
            fireEvent.click(apply);
            expect(screen.getByRole('button', {name: 'Remote filter applied'})).toBeInTheDocument();
            expect(screen.getByRole('button', {name: 'Opened Acme'})).toBeInTheDocument();
            expect(screen.getByTestId('assistant-actions-stale')).toHaveTextContent('');
        });

        test('the dialog committing long after the click is still the reply\'s own change', () => {
            const clock = jest.spyOn(Date, 'now').mockImplementation(() => 5_000_000);
            const release = openThenClose(true);
            clock.mockImplementation(() => 5_009_000);
            act(() => slow!());
            release();
            clock.mockRestore();
            expect(screen.getByRole('button', {name: 'Apply remote filter'})).toHaveAttribute('aria-disabled', 'false');
        });

        test('a different company opened by the reader still makes the reply stale', () => {
            const release = openThenClose(false);
            act(() => {
                setWorkspaceContext({surface: 'company', organizationId: 99, organizationName: 'Other'});
            });
            release();
            expect(screen.getByRole('button', {name: 'Apply remote filter'})).toHaveAttribute('aria-disabled', 'true');
            expect(screen.getByTestId('assistant-actions-stale')).toHaveTextContent(STALE_MESSAGE);
        });

        test('a filter the reader changes after applying one makes the reply stale', () => {
            const release = openThenClose(false);
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
            act(() => {
                setWorkspaceContext({filters: {rtoPolicy: 'O'}});
            });
            release();
            expect(screen.getByTestId('assistant-actions-stale')).toHaveTextContent(STALE_MESSAGE);
        });

        test('a click the target declines is not remembered as an effect', () => {
            const release = registerCompanyTarget(() => false);
            render(<AssistantActions turn={turn([remote, open12], {12: 'Acme'})} />);
            fireEvent.click(screen.getByRole('button', {name: 'Open Acme'}));
            release();
            act(() => {
                setWorkspaceContext({surface: 'company', organizationId: 12, organizationName: 'Acme', page: 8});
            });
            expect(screen.getByTestId('assistant-actions-stale')).toHaveTextContent(STALE_MESSAGE);
        });
    });

    test('Back removing the filter reverts the label and lets it be applied again', () => {
        const target = jest.fn((filters) => {
            setWorkspaceContext({filters});
            return true;
        });
        registerFilterTarget(target);
        setWorkspaceContext({surface: 'rankings', filters: {}});
        let now = 1_000_000;
        const clock = jest.spyOn(Date, 'now').mockImplementation(() => now);
        render(<AssistantActions turn={{...turn([remote]), context: getWorkspaceSnapshot().context}} />);
        fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
        expect(screen.getByRole('button', {name: 'Remote filter applied'})).toBeInTheDocument();
        now += 5000;
        act(() => {
            setWorkspaceContext({filters: {}});
        });
        const again = screen.getByRole('button', {name: 'Apply remote filter'});
        expect(again).toHaveAttribute('aria-disabled', 'false');
        fireEvent.click(again);
        clock.mockRestore();
        expect(target).toHaveBeenCalledTimes(2);
        expect(screen.getByRole('button', {name: 'Remote filter applied'})).toBeInTheDocument();
    });

    test('only an announcing reply puts the stale message in a live region', () => {
        const {rerender} = render(<AssistantActions turn={turn([remote])} announce={false} />);
        act(() => {
            setWorkspaceContext({page: 2});
        });
        const quiet = screen.getByTestId('assistant-actions-stale');
        expect(quiet).toHaveTextContent(STALE_MESSAGE);
        expect(quiet).not.toHaveAttribute('role');
        rerender(<AssistantActions turn={turn([remote])} announce />);
        expect(screen.getByTestId('assistant-actions-stale')).toHaveAttribute('role', 'status');
    });
});

describe('focus, undo and scrolling after a save (issue #484 review)', () => {
    const undoToken = {id: 'u1'} as never;
    const open = async () => {
        registerFilterTarget(() => true);
        render(<AssistantActions turn={turn([remote])} />);
        fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
    };
    const openReview = async () => {
        await open();
        proposePriorities.mockResolvedValue(proposal);
        fireEvent.click(screen.getByTestId('assistant-action-save'));
        await screen.findByTestId('assistant-action-review');
    };
    const saveWithUndo = async (undo: unknown = undoToken) => {
        await openReview();
        applyProposal.mockResolvedValue({revision: 5, changes: [], undo, scope: 'account'});
        fireEvent.click(screen.getByRole('button', {name: 'Save'}));
        await screen.findByTestId('assistant-action-saved');
    };

    test('Cancel returns focus to the follow-up button', async () => {
        await openReview();
        screen.getByRole('button', {name: 'Cancel'}).focus();
        fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
        expect(document.activeElement).toBe(screen.getByTestId('assistant-action-save'));
    });

    test('Save moves focus to the saved note', async () => {
        await openReview();
        screen.getByRole('button', {name: 'Save'}).focus();
        applyProposal.mockResolvedValue({revision: 5, changes: [], undo: null, scope: 'account'});
        fireEvent.click(screen.getByRole('button', {name: 'Save'}));
        const note = await screen.findByTestId('assistant-action-saved');
        expect(document.activeElement).toBe(note);
        expect(screen.queryByTestId('assistant-action-undo')).not.toBeInTheDocument();
    });

    test('focus follows the follow-up button into the loading note and then the retry button', async () => {
        await open();
        let reject: (reason: unknown) => void = () => undefined;
        proposePriorities.mockReturnValue(new Promise((_resolve, r) => { reject = r; }));
        screen.getByTestId('assistant-action-save').focus();
        fireEvent.click(screen.getByTestId('assistant-action-save'));
        expect(document.activeElement).toHaveTextContent('Preparing the change for review');
        await act(async () => { reject(new Error('offline')); });
        await screen.findByTestId('assistant-action-error');
        expect(document.activeElement).toBe(screen.getByRole('button', {name: 'Try again'}));
    });

    test('focus the reader moved elsewhere is left alone', async () => {
        await open();
        const elsewhere = document.createElement('button');
        document.body.appendChild(elsewhere);
        proposePriorities.mockRejectedValue(new Error('offline'));
        fireEvent.click(screen.getByTestId('assistant-action-save'));
        elsewhere.focus();
        await screen.findByTestId('assistant-action-error');
        expect(document.activeElement).toBe(elsewhere);
        elsewhere.remove();
    });

    test('Undo reverses the save and records the new revision', async () => {
        await saveWithUndo();
        undoApplied.mockResolvedValue(6);
        screen.getByTestId('assistant-action-undo').focus();
        fireEvent.click(screen.getByTestId('assistant-action-undo'));
        await waitFor(() => expect(screen.getByTestId('assistant-action-saved')).toHaveTextContent('Change undone.'));
        expect(document.activeElement).toBe(screen.getByTestId('assistant-action-saved'));
        expect(undoApplied).toHaveBeenCalledWith(undoToken, expect.any(AbortSignal));
        expect(getWorkspaceSnapshot().prioritiesRevision).toBe(6);
        expect(screen.queryByTestId('assistant-action-undo')).not.toBeInTheDocument();
    });

    test('an undo with no revision leaves the stored one alone, and a second press while pending is ignored', async () => {
        await saveWithUndo();
        let resolve: (value: number | null) => void = () => undefined;
        undoApplied.mockReturnValue(new Promise((r) => { resolve = r; }));
        const button = screen.getByTestId('assistant-action-undo');
        fireEvent.click(button);
        fireEvent.click(screen.getByTestId('assistant-action-undo'));
        expect(undoApplied).toHaveBeenCalledTimes(1);
        expect(screen.getByTestId('assistant-action-undo')).toHaveTextContent('Undoing…');
        await act(async () => { resolve(null); });
        expect(getWorkspaceSnapshot().prioritiesRevision).toBe(5);
    });

    test('an undo failure explains itself and keeps Undo available where the reader left focus', async () => {
        await saveWithUndo();
        screen.getByTestId('assistant-action-undo').focus();
        undoApplied.mockRejectedValueOnce(new ApiFailure(409, 'preference_stale', 'Your priorities changed.', {}, 6));
        fireEvent.click(screen.getByTestId('assistant-action-undo'));
        expect(await screen.findByTestId('assistant-action-undo-error'))
            .toHaveTextContent('can no longer be undone');
        expect(document.activeElement).toBe(screen.getByTestId('assistant-action-undo'));
        undoApplied.mockRejectedValueOnce(new ApiFailure(500, 'server_error', 'Server said no'));
        fireEvent.click(screen.getByTestId('assistant-action-undo'));
        await waitFor(() => expect(screen.getByTestId('assistant-action-undo-error')).toHaveTextContent('Server said no'));
        undoApplied.mockRejectedValueOnce(new Error('offline'));
        fireEvent.click(screen.getByTestId('assistant-action-undo'));
        await waitFor(() => expect(screen.getByTestId('assistant-action-undo-error'))
            .toHaveTextContent('Could not undo. Try again.'));
    });

    test('unmounting during an undo aborts it without a late update', async () => {
        const {unmount} = (() => {
            registerFilterTarget(() => true);
            return render(<AssistantActions turn={turn([remote])} />);
        })();
        fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
        proposePriorities.mockResolvedValue(proposal);
        fireEvent.click(screen.getByTestId('assistant-action-save'));
        await screen.findByTestId('assistant-action-review');
        applyProposal.mockResolvedValue({revision: 5, changes: [], undo: undoToken, scope: 'account'});
        fireEvent.click(screen.getByRole('button', {name: 'Save'}));
        await screen.findByTestId('assistant-action-saved');
        let signal: AbortSignal | undefined;
        undoApplied.mockImplementation((_token, s: AbortSignal) => {
            signal = s;
            return new Promise((_resolve, reject) => { s.addEventListener('abort', () => reject(new Error('aborted'))); });
        });
        fireEvent.click(screen.getByTestId('assistant-action-undo'));
        unmount();
        await act(async () => { await Promise.resolve(); });
        expect(signal?.aborted).toBe(true);
    });

    describe('a response landing after an account change or unmount writes nothing', () => {
        const saveToReview = async () => {
            registerFilterTarget(() => true);
            render(<AssistantActions turn={turn([remote])} />);
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
            proposePriorities.mockResolvedValue(proposal);
            await act(async () => undefined);
            fireEvent.click(screen.getByTestId('assistant-action-save'));
            await screen.findByTestId('assistant-action-review');
        };

        test('Save for the previous account does not record its revision', async () => {
            const write = setPrioritiesRevision;
            write(2);
            await saveToReview();
            let land: (value: unknown) => void = () => undefined;
            applyProposal.mockReturnValue(new Promise((resolve) => { land = resolve; }));
            fireEvent.click(screen.getByRole('button', {name: 'Save'}));
            act(() => {
                setWorkspaceAccount({status: 'authenticated', key: 'someone-else'});
            });
            await act(async () => {
                land({revision: 9, undo: null});
            });
            expect(getWorkspaceSnapshot().prioritiesRevision).toBe(2);
            expect(screen.queryByTestId('assistant-action-saved')).not.toBeInTheDocument();
        });

        test('Save records its revision when the account is unchanged', async () => {
            await saveToReview();
            applyProposal.mockResolvedValue({revision: 9, undo: null});
            fireEvent.click(screen.getByRole('button', {name: 'Save'}));
            await screen.findByTestId('assistant-action-saved');
            expect(getWorkspaceSnapshot().prioritiesRevision).toBe(9);
        });

        test('Undo for the previous account does not record its revision', async () => {
            await saveToReview();
            applyProposal.mockResolvedValue({revision: 9, undo: {id: 'u1'}});
            fireEvent.click(screen.getByRole('button', {name: 'Save'}));
            await screen.findByTestId('assistant-action-saved');
            let land: (value: unknown) => void = () => undefined;
            undoApplied.mockReturnValue(new Promise((resolve) => { land = resolve; }));
            fireEvent.click(screen.getByTestId('assistant-action-undo'));
            act(() => {
                setWorkspaceAccount({status: 'authenticated', key: 'someone-else'});
            });
            await act(async () => {
                land(11);
            });
            expect(getWorkspaceSnapshot().prioritiesRevision).toBe(9);
            expect(screen.queryByText('Change undone.')).not.toBeInTheDocument();
        });

        test('an aborted Save that still resolves writes nothing', async () => {
            await saveToReview();
            let land: (value: unknown) => void = () => undefined;
            applyProposal.mockReturnValue(new Promise((resolve) => { land = resolve; }));
            fireEvent.click(screen.getByRole('button', {name: 'Save'}));
            cleanup();
            await act(async () => {
                land({revision: 9, undo: null});
            });
            expect(getWorkspaceSnapshot().prioritiesRevision).toBeNull();
        });
    });

    describe('scrolling', () => {
        const stub = (scroller: HTMLElement, host: {top: number; bottom: number}) =>
            jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
                return (this === scroller ? {top: 0, bottom: 100} : host) as DOMRect;
            });

        test('the nearest scrolling ancestor scrolls even when it is not the log', () => {
            registerFilterTarget(() => true);
            const panel = document.createElement('div');
            panel.style.overflowY = 'auto';
            panel.innerHTML = '<div class="chat-bubble-assistant"></div>';
            document.body.appendChild(panel);
            const spy = stub(panel, {top: 50, bottom: 180});
            render(<AssistantActions turn={turn([remote])} />, {
                container: (panel.firstElementChild as HTMLElement).appendChild(document.createElement('div')),
            });
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
            expect(revealed.length).toBeGreaterThan(0);
            spy.mockRestore();
            panel.remove();
        });

        test('a reader who scrolled this reply out of view is not pulled back by a later update', async () => {
            registerFilterTarget(() => true);
            const log = document.createElement('div');
            log.setAttribute('role', 'log');
            log.innerHTML = '<div class="chat-bubble-assistant"></div>';
            document.body.appendChild(log);
            const spy = stub(log, {top: -300, bottom: -150});
            render(<AssistantActions turn={turn([remote])} />, {
                container: (log.firstElementChild as HTMLElement).appendChild(document.createElement('div')),
            });
            proposePriorities.mockResolvedValue(proposal);
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
            await act(async () => undefined);
            expect(screen.getByTestId('assistant-action-save')).toBeInTheDocument();
            expect(log.scrollTop).toBe(0);
            expect(revealed).toHaveLength(0);
            spy.mockRestore();
            log.remove();
        });

        test('with no scrolling ancestor at all nothing throws', () => {
            registerFilterTarget(() => true);
            render(<AssistantActions turn={turn([remote])} />);
            fireEvent.click(screen.getByRole('button', {name: 'Apply remote filter'}));
            expect(screen.getByRole('button', {name: 'Remote filter applied'})).toBeInTheDocument();
        });
    });
});
