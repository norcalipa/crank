// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Issue #485 (485b): the panel learns that newer results exist by polling the
// revision read, says so, and applies them on request or when idle.
import '@testing-library/jest-dom';
import {act, cleanup, fireEvent, render, screen} from '@testing-library/react';
import * as React from 'react';

import JobMatchPanel, {shouldAutoApplyRefresh} from './JobMatchPanel';
import {getWorkspaceSnapshot, resetWorkspaceForTests} from './workspace/store';

const POLL = 60_000;

function job(id: number, title: string, generation: number) {
    return {
        listing_id: id,
        title,
        employer_name: 'Acme Corp',
        organization_id: 7,
        organization_name: 'Acme Corp',
        canonical_url: `https://jobs.example.test/${id}`,
        location_text: 'Remote',
        is_remote: true,
        score: 80,
        reasons: ['Remote'],
        revision: {result_generation: generation},
    };
}

// What the server holds: a generation, the jobs it ranks, and what each read
// says. `statusGeneration` / `matchGeneration` override one read to simulate a
// publish landing between the three.
interface Server {
    generation: number | null;
    jobs: Record<number, Array<[number, string]>>;
    statusGeneration?: (call: number) => number | null | undefined;
    matchGeneration?: (call: number) => number | null | undefined;
    revision: () => Response | Promise<Response>;
    loads: number;
}

const json = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
    status, headers: {'Content-Type': 'application/json'},
});

function revisionAnswer(generation: number | null, seconds: number | null = 60) {
    return json({
        result_generation: generation, pending: false, stale: false, preference_revision: 1,
        generated_at: null, poll_after_seconds: seconds,
    });
}

function makeServer(generation: number | null = 1): Server {
    const server: Server = {
        generation,
        jobs: {1: [[1, 'Alpha'], [2, 'Bravo']], 2: [[3, 'Newest'], [1, 'Alpha'], [2, 'Bravo']], 3: [[4, 'Later'], [1, 'Alpha']]},
        revision: () => revisionAnswer(server.generation),
        loads: 0,
    };
    return server;
}

function install(server: Server) {
    global.fetch = jest.fn().mockImplementation((url: string) => {
        if (url.includes('/api/job-matches/revision/')) {
            return Promise.resolve(server.revision());
        }
        const generation = server.generation;
        if (url.includes('/api/job-matches/status/')) {
            server.loads += 1;
            const stamped = server.statusGeneration?.(server.loads);
            return Promise.resolve(json({
                state: 'ok', title: 'OK', message: 'OK', actions: [],
                result_generation: stamped === undefined ? generation : stamped,
            }));
        }
        const rows = generation === null ? [] : (server.jobs[generation] ?? []);
        if (url.includes('/api/job-matches/ranked/')) {
            return Promise.resolve(json({
                job_matches: rows.map(([id, title]) => job(id, title, generation!)),
                organization_matches: [],
            }));
        }
        const stamped = server.matchGeneration?.(server.loads);
        const matchGeneration = stamped === undefined ? generation : stamped;
        return Promise.resolve(json({
            count: rows.length, next: null, previous: null,
            results: rows.slice(0, 1).map(([id, title]) => job(id, title, matchGeneration!)),
        }));
    });
}

const calls = (fragment: string) =>
    (global.fetch as jest.Mock).mock.calls.filter(([url]) => String(url).includes(fragment)).length;

let visibility: 'visible' | 'hidden' = 'visible';
function setVisibility(next: 'visible' | 'hidden') {
    visibility = next;
    act(() => {
        document.dispatchEvent(new Event('visibilitychange'));
    });
}

const advance = async (ms: number) => {
    await act(async () => {
        await jest.advanceTimersByTimeAsync(ms);
    });
};

async function mount(server: Server, props: {isAuthenticated?: boolean} = {}) {
    install(server);
    const view = render(<JobMatchPanel {...props}/>);
    await screen.findByTestId('ranked-job-1');
    return view;
}

const notice = () => screen.queryByTestId('updated-results-notice');
const press = async () => {
    await act(async () => {
        fireEvent.click(screen.getByTestId('updated-results-refresh'));
    });
    await advance(0);
};

beforeEach(() => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date('2026-10-14T12:00:00Z'));
    visibility = 'visible';
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => visibility});
    window.scrollTo = jest.fn();
    Object.defineProperty(window, 'scrollY', {configurable: true, writable: true, value: 0});
});

afterEach(() => {
    cleanup();
    jest.useRealTimers();
    jest.restoreAllMocks();
    resetWorkspaceForTests();
    window.history.replaceState(null, '', '/');
});

describe('shouldAutoApplyRefresh (the idle rule)', () => {
    const cases: Array<[boolean, number, boolean, boolean]> = [];
    for (const returned of [true, false]) {
        for (const scrollY of [0, 1, 600]) {
            for (const focus of [true, false]) {
                cases.push([returned, scrollY, focus, returned && scrollY === 0 && !focus]);
            }
        }
    }
    test.each(cases)('returned=%s scrollY=%s focusInside=%s → %s', (returned, scrollY, focus, expected) => {
        expect(shouldAutoApplyRefresh({
            foundOnFirstCheckAfterReturn: returned, scrollY, focusInsidePanel: focus,
        })).toBe(expected);
    });
});

describe('polling and the notice', () => {
    test('asks once per interval and says nothing while the generation is the one shown', async () => {
        const server = makeServer(1);
        await mount(server);
        expect(calls('/revision/')).toBe(0);
        await advance(POLL);
        expect(calls('/revision/')).toBe(1);
        await advance(POLL);
        expect(calls('/revision/')).toBe(2);
        expect(notice()).not.toBeInTheDocument();
    });

    test('a lower generation than the one shown is ignored', async () => {
        const server = makeServer(2);
        await mount(server);
        server.generation = 1;
        await advance(POLL);
        expect(calls('/revision/')).toBe(1);
        expect(notice()).not.toBeInTheDocument();
    });

    test('a higher generation shows one notice and changes nothing else', async () => {
        const server = makeServer(1);
        await mount(server);
        const alpha = screen.getByTestId('ranked-job-1');
        const loadsBefore = calls('/status/');
        server.generation = 2;
        await advance(POLL);
        const banner = await screen.findByTestId('updated-results-notice');
        expect(banner).toHaveAttribute('role', 'status');
        expect(banner).toHaveTextContent('Updated results available.');
        expect(screen.getByRole('button', {name: 'Refresh results'})).toBeInTheDocument();
        // Same nodes, same list, nothing re-read, nothing scrolled or focused.
        expect(screen.getByTestId('ranked-job-1')).toBe(alpha);
        expect(screen.queryByTestId('ranked-job-3')).not.toBeInTheDocument();
        expect(calls('/status/')).toBe(loadsBefore);
        expect(window.scrollTo).not.toHaveBeenCalled();
        expect(document.activeElement).toBe(document.body);
    });

    test('the notice is announced once: later polls keep the same node and text', async () => {
        const server = makeServer(1);
        await mount(server);
        server.generation = 2;
        await advance(POLL);
        const banner = await screen.findByTestId('updated-results-notice');
        const text = banner.textContent;
        server.generation = 3;
        await advance(POLL);
        await advance(POLL);
        expect(screen.getAllByTestId('updated-results-notice')).toHaveLength(1);
        expect(screen.getByTestId('updated-results-notice')).toBe(banner);
        expect(banner.textContent).toBe(text);
        expect(banner.querySelectorAll('[aria-live="assertive"]')).toHaveLength(0);
    });

    test('a hidden tab sends nothing, and one check follows the return', async () => {
        const server = makeServer(1);
        await mount(server);
        setVisibility('hidden');
        await advance(10 * 60_000);
        expect(calls('/revision/')).toBe(0);
        setVisibility('visible');
        await advance(0);
        expect(calls('/revision/')).toBe(1);
    });

    test('live results with no generation start no polling at all', async () => {
        const server = makeServer(null);
        install(server);
        render(<JobMatchPanel/>);
        await advance(0);
        await advance(10 * 60_000);
        expect(calls('/revision/')).toBe(0);
    });

    test('a null poll_after_seconds ends polling after that answer', async () => {
        const server = makeServer(1);
        server.revision = () => revisionAnswer(1, null);
        await mount(server);
        await advance(POLL);
        await advance(10 * 60_000);
        expect(calls('/revision/')).toBe(1);
    });

    test('a failed poll shows nothing and backs off', async () => {
        const server = makeServer(1);
        server.revision = () => json({}, 500);
        await mount(server);
        await advance(POLL);
        expect(calls('/revision/')).toBe(1);
        await advance(POLL);
        expect(calls('/revision/')).toBe(1);
        await advance(POLL);
        expect(calls('/revision/')).toBe(2);
        expect(notice()).not.toBeInTheDocument();
        expect(screen.queryByTestId('refresh-failed-notice')).not.toBeInTheDocument();
        expect(screen.queryByTestId('job-match-error')).not.toBeInTheDocument();
    });

    test('a redirect stops polling and shows nothing; the next observed hydration starts it again', async () => {
        const server = makeServer(1);
        server.revision = () => ({
            redirected: true, ok: true, status: 200, url: '/accounts/login/?next=x', json: () => Promise.resolve({}),
        } as unknown as Response);
        await mount(server);
        await advance(POLL);
        await advance(10 * 60_000);
        expect(calls('/revision/')).toBe(1);
        expect(screen.queryByTestId('job-match-error')).not.toBeInTheDocument();
        expect(screen.queryByTestId('job-match-signed-out')).not.toBeInTheDocument();
        expect(screen.getByTestId('ranked-job-1')).toBeInTheDocument();
        // An unobserved hydration does not start it again.
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {unobserved: true}}));
        });
        await advance(POLL);
        expect(calls('/revision/')).toBe(1);
        server.revision = () => revisionAnswer(1);
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true, username: 'a'}}));
        });
        await advance(POLL);
        expect(calls('/revision/')).toBe(2);
    });

    test('a 401 stops polling as well', async () => {
        const server = makeServer(1);
        server.revision = () => json({}, 401);
        await mount(server);
        await advance(POLL);
        await advance(10 * 60_000);
        expect(calls('/revision/')).toBe(1);
    });

    test('an unobserved hydration with no purge neither starts nor stops polling', async () => {
        const server = makeServer(1);
        await mount(server);
        await advance(POLL);
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {unobserved: true}}));
        });
        await advance(POLL);
        expect(calls('/revision/')).toBe(2);
    });

    test('no timer or revision request survives unmount', async () => {
        const server = makeServer(1);
        const view = await mount(server);
        await advance(POLL);
        view.unmount();
        expect(jest.getTimerCount()).toBe(0);
        await advance(10 * 60_000);
        expect(calls('/revision/')).toBe(1);
    });
});

describe('Refresh results', () => {
    async function withNotice(server = makeServer(1)) {
        await mount(server);
        server.generation = 2;
        await advance(POLL);
        await screen.findByTestId('updated-results-notice');
        return server;
    }

    test('one quiet read switches the list, the count and the context together and focuses the heading', async () => {
        const server = await withNotice();
        const alpha = screen.getByTestId('ranked-job-1');
        const before = (global.fetch as jest.Mock).mock.calls.length;
        await press();
        expect((global.fetch as jest.Mock).mock.calls.length - before).toBe(3);
        expect(await screen.findByTestId('ranked-job-3')).toBeInTheDocument();
        expect(notice()).not.toBeInTheDocument();
        expect(screen.getByTestId('ranked-job-1')).toBe(alpha);
        expect(screen.queryByTestId('job-match-loading')).not.toBeInTheDocument();
        expect(getWorkspaceSnapshot().context?.resultGeneration).toBe(2);
        expect(document.activeElement).toBe(document.getElementById('job-match-panel-title'));
        expect(server.generation).toBe(2);
        // The next poll compares against the new generation: no notice again.
        await advance(POLL);
        expect(notice()).not.toBeInTheDocument();
    });

    test('a double press while the read runs sends one read', async () => {
        await withNotice();
        const before = calls('/status/');
        await act(async () => {
            fireEvent.click(screen.getByTestId('updated-results-refresh'));
            fireEvent.click(screen.getByTestId('updated-results-refresh'));
        });
        await advance(0);
        expect(calls('/status/') - before).toBe(1);
    });

    test('three reads that disagree are tried once more and then the old set stays with the notice', async () => {
        const server = await withNotice();
        server.statusGeneration = () => 3;
        const before = (global.fetch as jest.Mock).mock.calls.length;
        await press();
        expect((global.fetch as jest.Mock).mock.calls.length - before).toBe(6);
        expect(screen.queryByTestId('ranked-job-3')).not.toBeInTheDocument();
        expect(screen.getByTestId('ranked-job-1')).toBeInTheDocument();
        expect(notice()).toBeInTheDocument();
        expect(getWorkspaceSnapshot().context?.resultGeneration).toBe(1);
        expect(document.activeElement).not.toBe(document.getElementById('job-match-panel-title'));
        expect(screen.getByTestId('read-announcement')).toHaveTextContent('weren’t ready');
    });

    test('a disagreement that clears on the retry is applied', async () => {
        const server = await withNotice();
        // The first accept read (status call 2) straddles a publish; the retry does not.
        server.matchGeneration = (call) => (call === 2 ? 1 : 2);
        const before = (global.fetch as jest.Mock).mock.calls.length;
        await press();
        expect((global.fetch as jest.Mock).mock.calls.length - before).toBe(6);
        expect(await screen.findByTestId('ranked-job-3')).toBeInTheDocument();
        expect(notice()).not.toBeInTheDocument();
    });

    test('a read that fails keeps the list, shows the failed notice and keeps the notice', async () => {
        const server = await withNotice();
        const original = global.fetch as jest.Mock;
        global.fetch = jest.fn().mockImplementation((url: string, init?: RequestInit) =>
            (String(url).includes('/revision/') ? original(url, init) : Promise.reject(new Error('down'))));
        await press();
        expect(await screen.findByTestId('refresh-failed-notice')).toBeInTheDocument();
        expect(notice()).toBeInTheDocument();
        expect(screen.getByTestId('ranked-job-1')).toBeInTheDocument();
        expect(server.generation).toBe(2);
    });

    test('while the read runs the button is disabled and the notice stays', async () => {
        await withNotice();
        const original = global.fetch as jest.Mock;
        let release!: () => void;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        global.fetch = jest.fn().mockImplementation((url: string, init?: RequestInit) =>
            gate.then(() => original(url, init)));
        await act(async () => {
            fireEvent.click(screen.getByTestId('updated-results-refresh'));
        });
        expect(screen.getByTestId('updated-results-refresh')).toHaveAttribute('aria-disabled', 'true');
        expect(notice()).toBeInTheDocument();
        await act(async () => {
            release();
            await jest.advanceTimersByTimeAsync(0);
        });
        expect(notice()).not.toBeInTheDocument();
    });

    test('accepting sends no assistant turn and leaves a draft in the composer alone', async () => {
        const server = makeServer(1);
        install(server);
        render(<><JobMatchPanel/><textarea data-testid="composer" defaultValue="" /></>);
        await screen.findByTestId('ranked-job-1');
        fireEvent.change(screen.getByTestId('composer'), {target: {value: 'half a thought'}});
        server.generation = 2;
        await advance(POLL);
        await screen.findByTestId('updated-results-notice');
        await press();
        await screen.findByTestId('ranked-job-3');
        expect(calls('/api/agent/')).toBe(0);
        expect(screen.getByTestId('composer')).toHaveValue('half a thought');
        // The assistant is told which generation the reader now sees, so a
        // reply to a turn already in flight carries its "earlier view" note.
        expect(getWorkspaceSnapshot().context?.resultGeneration).toBe(2);
    });

    test('puts the first result present in both sets back at its offset', async () => {
        const server = makeServer(1);
        await mount(server);
        // Alpha sits at 120 px; once the newer list is on screen (a new row
        // above it, the notice gone) it sits at 200 px.
        jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
            const id = (this as HTMLElement).dataset?.testid ?? '';
            const newer = !!document.querySelector('[data-testid="ranked-job-3"]');
            const top = id === 'ranked-job-1' ? (newer ? 200 : 120) : id === 'ranked-job-2' ? 300 : 0;
            return {top, bottom: top + 100, left: 0, right: 10, width: 10, height: 100, x: 0, y: top, toJSON: () => ({})} as DOMRect;
        });
        const scrollBy = jest.fn();
        window.scrollBy = scrollBy as unknown as typeof window.scrollBy;
        server.generation = 2;
        await advance(POLL);
        await screen.findByTestId('updated-results-notice');
        await press();
        await screen.findByTestId('ranked-job-3');
        expect(scrollBy).toHaveBeenCalledWith({top: 80, left: 0, behavior: 'instant'});
    });
});

describe('the idle rule in the panel', () => {
    async function foundAfterReturn(prepare: () => void = () => undefined) {
        const server = makeServer(1);
        await mount(server);
        prepare();
        setVisibility('hidden');
        await advance(5 * 60_000);
        server.generation = 2;
        setVisibility('visible');
        await advance(0);
        return server;
    }

    test('applies on its own when found on the first check after the return, at the top, with no focus in the panel', async () => {
        await foundAfterReturn();
        expect(await screen.findByTestId('ranked-job-3')).toBeInTheDocument();
        expect(notice()).not.toBeInTheDocument();
        // Idle apply never moves focus.
        expect(document.activeElement).toBe(document.body);
    });

    test('shows only the notice when the page is scrolled', async () => {
        await foundAfterReturn(() => { (window as any).scrollY = 40; });
        expect(await screen.findByTestId('updated-results-notice')).toBeInTheDocument();
        expect(screen.queryByTestId('ranked-job-3')).not.toBeInTheDocument();
    });

    test('shows only the notice when something in the panel has focus', async () => {
        await foundAfterReturn(() => screen.getByTestId('job-match-refresh').focus());
        expect(await screen.findByTestId('updated-results-notice')).toBeInTheDocument();
        expect(screen.queryByTestId('ranked-job-3')).not.toBeInTheDocument();
        expect(document.activeElement).toBe(screen.getByTestId('job-match-refresh'));
    });

    test('shows only the notice when it was not found on the first check after a return', async () => {
        const server = makeServer(1);
        await mount(server);
        server.generation = 2;
        await advance(POLL);
        expect(await screen.findByTestId('updated-results-notice')).toBeInTheDocument();
        expect(screen.queryByTestId('ranked-job-3')).not.toBeInTheDocument();
    });

    test('a failed idle apply leaves the notice up', async () => {
        const server = makeServer(1);
        await mount(server);
        setVisibility('hidden');
        await advance(5 * 60_000);
        server.generation = 2;
        server.statusGeneration = () => 3;
        setVisibility('visible');
        await advance(0);
        expect(await screen.findByTestId('updated-results-notice')).toBeInTheDocument();
        expect(screen.queryByTestId('ranked-job-3')).not.toBeInTheDocument();
    });

    test('does not start a second read while one is running', async () => {
        const server = makeServer(1);
        await mount(server);
        setVisibility('hidden');
        await advance(5 * 60_000);
        server.generation = 2;
        const original = global.fetch as jest.Mock;
        global.fetch = jest.fn().mockImplementation((url: string, init?: RequestInit) =>
            (String(url).includes('/revision/') ? original(url, init) : new Promise(() => undefined)));
        await act(async () => {
            fireEvent.click(screen.getByTestId('job-match-refresh'));
        });
        const reads = calls('/status/');
        setVisibility('visible');
        await advance(0);
        expect(calls('/status/')).toBe(reads);
    });
});

describe('privacy (issue #485, AC 14)', () => {
    const purge = () => act(() => {
        document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
    });
    const hydrate = (detail: Record<string, unknown>) => act(() => {
        document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail}));
    });

    test('after a purge nothing is asked and the notice is gone, until an observed hydration', async () => {
        const server = makeServer(1);
        await mount(server);
        server.generation = 2;
        await advance(POLL);
        await screen.findByTestId('updated-results-notice');
        const asked = calls('/revision/');
        purge();
        expect(notice()).not.toBeInTheDocument();
        await advance(10 * 60_000);
        expect(calls('/revision/')).toBe(asked);
        hydrate({authenticated: true, username: 'alice'});
        await screen.findByTestId('ranked-job-3');
        await advance(POLL);
        expect(calls('/revision/')).toBe(asked + 1);
    });

    test('an answer that started before the purge is dropped', async () => {
        const server = makeServer(1);
        await mount(server);
        let answer!: () => void;
        server.revision = () => new Promise<Response>((resolve) => {
            answer = () => resolve(revisionAnswer(5));
        });
        await advance(POLL);
        expect(calls('/revision/')).toBe(1);
        purge();
        await act(async () => {
            answer();
            await jest.advanceTimersByTimeAsync(0);
        });
        expect(notice()).not.toBeInTheDocument();
        hydrate({authenticated: true, username: 'bob'});
        await act(async () => {
            await jest.advanceTimersByTimeAsync(0);
        });
        expect(notice()).not.toBeInTheDocument();
    });

    test('an unobserved hydration after a purge brings the results back but still sends no poll', async () => {
        const server = makeServer(1);
        await mount(server);
        const asked = calls('/revision/');
        purge();
        hydrate({authenticated: false, username: null, unobserved: true});
        await screen.findByTestId('ranked-job-1');
        await advance(10 * 60_000);
        expect(calls('/revision/')).toBe(asked);
        // The observed hydration that follows lets it run.
        hydrate({authenticated: true, username: 'alice'});
        await advance(POLL);
        expect(calls('/revision/')).toBe(asked + 1);
    });

    test('the session ending under a read stops polling with the rest of the account state', async () => {
        const server = makeServer(1);
        await mount(server);
        const asked = calls('/revision/');
        const original = global.fetch as jest.Mock;
        global.fetch = jest.fn().mockImplementation((url: string, init?: RequestInit) =>
            (String(url).includes('/status/') ? Promise.resolve(json({}, 401)) : original(url, init)));
        await act(async () => {
            fireEvent.click(screen.getByTestId('job-match-refresh'));
        });
        await advance(0);
        expect(screen.getByTestId('job-match-signed-out')).toBeInTheDocument();
        await advance(10 * 60_000);
        expect(calls('/revision/')).toBe(asked);
        expect(jest.getTimerCount()).toBe(0);
    });
});
