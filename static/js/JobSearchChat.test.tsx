// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, waitFor, act} from '@testing-library/react';
import * as React from 'react';

import {
    clearWorkspaceContext,
    getWorkspaceSnapshot,
    resetWorkspaceForTests,
    setWorkspaceAccount,
    setWorkspaceContext,
} from './workspace/store';
import JobSearchChat, {AssistantState, AssistantStatus, ChatMessage} from './JobSearchChat';

function jsonResponse(payload: unknown, status = 200): Response {
    return new Response(JSON.stringify(payload), {
        status,
        headers: {'Content-Type': 'application/json'},
    });
}

function emptyConversation(id: number, messages: ChatMessage[] = []) {
    return {
        id,
        active: true,
        created: null,
        modified: null,
        messages,
        preferences_changed: false,
    };
}

function assistantMessage(id: number, content: string, preferences_changed = false, results: any = null): ChatMessage {
    return {id, role: 'assistant', content, preferences_changed, created: null, results};
}

function userMessage(content: string): ChatMessage {
    return {id: 1, role: 'user', content, preferences_changed: false, created: null, results: null};
}

function assistantStatus(state: AssistantState, actions: string[] = []): {state: AssistantState; actions: string[]; checked_at: string} {
    return {state, actions, checked_at: '2026-09-14T00:00:00Z'};
}

function statusResponse(state: AssistantState, actions: string[] = []): Response {
    return jsonResponse(assistantStatus(state, actions));
}

// Fetch mock for suites written before the advisory status check (issue
// #457): the status URL always answers 'ready' and never reaches the queued
// mock, so those suites keep their exact fetch order and call counts.
function statusAwareFetch(): jest.Mock {
    const queue = jest.fn();
    const route = (input: RequestInfo | URL, init?: RequestInit) =>
        String(input).includes('/api/agent/assistant-status/')
            ? Promise.resolve(statusResponse('ready'))
            : queue(input, init);
    return new Proxy(route, {
        get: (_target, prop) => (prop === STATUS_AWARE ? true : (queue as any)[prop]),
    }) as unknown as jest.Mock;
}

const STATUS_AWARE = Symbol('statusAwareFetch');

function isStatusAware(fetchMock: unknown): boolean {
    return Boolean((fetchMock as any)[STATUS_AWARE]);
}

function userTurn(content: string, key: string, deliveryState: ChatMessage['delivery_state'], id = 1, retryAvailable?: boolean): ChatMessage {
    return {id, role: 'user', content, preferences_changed: false, created: null, results: null, idempotency_key: key, delivery_state: deliveryState, retry_available: retryAvailable};
}

// In-flight markers are stored per turn (conversation id + turn key) so
// concurrent turns/tabs cannot overwrite each other's recovery state.
function inflightKeyFor(conversationId: number, key: string): string {
    return `crank:jobsearch:inflight:${conversationId}:${key}`;
}

async function renderChat(existingMessages: ChatMessage[] = []) {
    // Mount order: the advisory status check fires first, then the resume.
    // A status-aware fetch mock answers the check itself.
    if (!isStatusAware(global.fetch)) {
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
    }
    // Resume the user's most recent conversation on mount.
    (global.fetch as jest.Mock).mockResolvedValueOnce(
        jsonResponse(emptyConversation(42, existingMessages)),
    );
    render(<JobSearchChat/>);
    // Wait until resume resolves and the input is enabled.
    await screen.findByLabelText('Message');
    await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
}

async function renderChatAs(existingMessages: ChatMessage[]) {
    // Render returning the instance handle for tests that mount twice.
    (global.fetch as jest.Mock).mockResolvedValueOnce(
        jsonResponse(emptyConversation(42, existingMessages)),
    );
    const instance = render(<JobSearchChat/>);
    await screen.findByLabelText('Message');
    await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
    return instance;
}

function lastPostedKey(fetchMock: jest.Mock): string {
    const posts = postBodies(fetchMock, messageUrl());
    return posts[posts.length - 1].idempotency_key;
}

// The advisory pre-send status check (issue #457) runs before the POST, so a
// send's POST is issued a tick after the click: wait for it, then read its key.
async function postedKeyAfterSend(fetchMock: jest.Mock): Promise<string> {
    await waitFor(() => expect(postBodies(fetchMock, messageUrl()).length).toBeGreaterThan(0));
    return lastPostedKey(fetchMock);
}

// Hold the next fetch open and settle it on demand, so a test can queue the
// follow-up reconcile response before the send's outcome lands.
function holdNextFetch(fetchMock: jest.Mock): (outcome: Response | Error) => void {
    let settle: (outcome: Response | Error) => void = () => {};
    fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve, reject) => {
        settle = (outcome) => (outcome instanceof Error ? reject(outcome) : resolve(outcome));
    }));
    return (outcome) => settle(outcome);
}

function messageUrl() {
    return '/api/agent/conversations/42/';
}

function postBodies(fetchMock: jest.Mock, urlPrefix: string) {
    return fetchMock.mock.calls
        .filter(([url]) => String(url).startsWith(urlPrefix))
        .filter(([, init]) => typeof (init as RequestInit)?.body === 'string')
        .map(([, init]) => JSON.parse((init as RequestInit).body as string));
}

const originalResizeObserver = globalThis.ResizeObserver;

describe('JobSearchChat', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        resetWorkspaceForTests();
        jest.restoreAllMocks();
        // Restore ResizeObserver so a mock leaking from a failing test cannot
        // affect later tests (MINOR-3).
        if (originalResizeObserver === undefined) {
            delete (globalThis as {ResizeObserver?: unknown}).ResizeObserver;
        } else {
            (globalThis as {ResizeObserver?: unknown}).ResizeObserver = originalResizeObserver;
        }
    });

    // jsdom implements requestAnimationFrame as a real ~16ms timer. Use this to
    // flush the height measurement before asserting on rendered style.
    function flushRaf(): Promise<void> {
        return new Promise((resolve) => {
            const id = window.requestAnimationFrame(() => {
                window.cancelAnimationFrame(id);
                resolve();
            });
        });
    }

    describe('viewport-reactive height measurement', () => {
        beforeEach(() => {
            // Pin the jsdom default explicitly (768) rather than relying on it, so
            // the expected pixel heights below are self-explanatory (NIT-3).
            window.innerHeight = 768;
        });

        test('sets a computed pixel height instead of a fixed 7rem offset', async () => {
            await renderChat();
            await act(async () => { await flushRaf(); });
            const chat = screen.getByTestId('job-search-chat');
            // innerHeight 768 - card top 0 - bottom gap 16 -> 752px.
            expect(chat).toHaveStyle({height: '752px', minHeight: '20rem'});
        });

        test('observes the parent for match-panel resizes when ResizeObserver is available', async () => {
            const observe = jest.fn();
            const disconnect = jest.fn();
            class MockResizeObserver {
                observe = observe;
                disconnect = disconnect;
            }
            (globalThis as {ResizeObserver?: unknown}).ResizeObserver = MockResizeObserver;
            await renderChat();
            // afterEach restores the original ResizeObserver, so there is no
            // manual leave-behind to leak into later tests.
            expect(observe).toHaveBeenCalled();
        });

        test('re-measures the card height when the viewport resizes', async () => {
            await renderChat();
            const chat = screen.getByTestId('job-search-chat');
            await act(async () => { await flushRaf(); });
            expect(chat).toHaveStyle({height: '752px'});

            // Shrink the viewport; the card must re-measure to the new height.
            window.innerHeight = 600;
            await act(async () => {
                fireEvent(window, new Event('resize'));
                await flushRaf();
            });
            // 600 - 0 - 16 = 584px (> MIN_CARD_PX 320).
            expect(chat).toHaveStyle({height: '584px'});
        });

        test('coalesces a resize burst into a single queued measure', async () => {
            await renderChat();
            await act(async () => { await flushRaf(); }); // settle mount measure
            const rafSpy = jest.spyOn(window, 'requestAnimationFrame');
            await act(async () => {
                fireEvent(window, new Event('resize'));
                fireEvent(window, new Event('resize'));
                fireEvent(window, new Event('resize'));
            });
            // The guard flag allows only one rAF to be outstanding for the burst.
            expect(rafSpy).toHaveBeenCalledTimes(1);
            await act(async () => { await flushRaf(); }); // drain the pending frame
        });

        test('clamps a negative (scrolled) card offset so the card never exceeds the viewport', async () => {
            await renderChat();
            const chat = screen.getByTestId('job-search-chat');
            // Simulate the page scrolled so the card's viewport offset is negative.
            jest.spyOn(chat, 'getBoundingClientRect').mockReturnValue({
                top: -200, bottom: 0, left: 0, right: 0, width: 0, height: 0, x: 0, y: -200,
                toJSON: () => ({}),
            } as DOMRect);
            await act(async () => {
                fireEvent(window, new Event('resize'));
                await flushRaf();
            });
            // Un-clamped, -200 would compute 768 - (-200) - 16 = 952px. Clamping the
            // offset to 0 keeps the card within the viewport at 752px.
            expect(chat).toHaveStyle({height: '752px'});
            expect(chat).not.toHaveStyle({height: '952px'});
        });

        test('falls back to the default bottom gap when the safe-area inset cannot be read', async () => {
            await renderChat();
            const chat = screen.getByTestId('job-search-chat');
            await act(async () => { await flushRaf(); });
            jest.spyOn(window, 'getComputedStyle').mockImplementationOnce(() => {
                throw new Error('getComputedStyle unavailable');
            });
            await act(async () => {
                fireEvent(window, new Event('resize'));
                await flushRaf();
            });
            // Non-fatal: measurement still completes with the 16px fallback.
            expect(chat).toHaveStyle({height: '752px'});
        });
    });

    describe('rendering & accessibility semantics', () => {
        test('uses a viewport-aware flex layout with a pinned composer', async () => {
            await renderChat();
            const chat = screen.getByTestId('job-search-chat');
            const history = screen.getByLabelText('Message history');
            const composer = document.querySelector('form')!.parentElement!;
            expect(chat).toHaveClass('d-flex', 'flex-column');
            expect(chat).toHaveStyle({minHeight: '20rem'});
            expect(history).toHaveClass('flex-grow-1');
            expect(history).toHaveStyle({overflowY: 'auto'});
            expect(composer).toHaveClass('flex-shrink-0');
        });

        test('renders an accessible input and live message history region', async () => {
            await renderChat();
            const input = screen.getByLabelText('Message');
            expect(input).toBeInTheDocument();
            expect(screen.getByRole('button', {name: 'Send message'})).toBeInTheDocument();
            // Round 2: the send control is a labeled ≥44px target, not a narrow strip.
            expect(screen.getByRole('button', {name: 'Send message'})).toHaveClass('chat-send');
            expect(screen.getByRole('button', {name: 'Send message'})).toHaveTextContent('Send');
            expect(screen.getByRole('region', {name: 'Conversation'})).toBeInTheDocument();
            expect(screen.getByRole('note')).toHaveTextContent(/automated and can be wrong/i);
            expect(screen.getByRole('note')).toHaveTextContent(/saved to your account/i);
            expect(screen.getByLabelText('Message history')).toHaveAttribute('aria-live', 'polite');
            expect(screen.getByLabelText('Message history')).toHaveAttribute('aria-busy', 'false');
            expect(screen.getByTestId('empty-history')).toBeInTheDocument();
            expect(screen.getByTestId('empty-history')).toHaveTextContent(/Job-match status and results appear in the Job Matches panel/i);
            // Prominent primary next action (visual review #472 round 1, item 11).
            const cta = screen.getByTestId('empty-history-cta');
            expect(cta).toHaveClass('btn', 'btn-primary');
            fireEvent.click(cta);
            expect(document.activeElement).toBe(screen.getByLabelText('Message'));
        });

        test('sheet placement renders the Back-to-results microcopy instead of the beside-panel copy', async () => {
            await renderChat();
            expect(screen.getByTestId('empty-history')).toHaveTextContent(/Job Matches panel/i);
            expect(screen.getByTestId('empty-history')).not.toHaveTextContent(/Tap Back to results/i);
        });

        test('workspaceMode="sheet" renders directional Back-to-results microcopy (issue #472)', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
            setWorkspaceAccount({status: 'authenticated', key: 'tester'});
            render(<JobSearchChat workspaceMode="sheet"/>);
            await screen.findByTestId('empty-history');
            expect(screen.getByTestId('empty-history')).toHaveTextContent(/Tap Back to results/i);
            expect(screen.getByTestId('empty-history')).not.toHaveTextContent(/Job Matches panel/i);
        });

        test('renders existing message history', async () => {
            await renderChat([userMessage('hello'), assistantMessage(2, 'hi there')]);
            expect(screen.getByText('hello')).toBeInTheDocument();
            expect(screen.getByText('hi there')).toBeInTheDocument();
            expect(screen.getByRole('article', {name: 'Your message'})).toHaveTextContent('hello');
            expect(screen.getByRole('article', {name: 'Assistant message'})).toHaveTextContent('hi there');
            expect(screen.queryByTestId('empty-history')).not.toBeInTheDocument();
        });

        test('renders high-contrast message bubbles with semantic surfaces', async () => {
            await renderChat([userMessage('hello'), assistantMessage(2, 'hi there')]);
            const userBubble = screen.getByRole('article', {name: 'Your message'}).firstElementChild as HTMLElement;
            const assistantBubble = screen.getByRole('article', {name: 'Assistant message'}).firstElementChild as HTMLElement;
            expect(userBubble).toHaveClass('chat-bubble', 'chat-bubble-user');
            expect(assistantBubble).toHaveClass('chat-bubble', 'chat-bubble-assistant');
        });

        test('compacts the data note behind a details toggle', async () => {
            await renderChat();
            const toggle = screen.getByTestId('data-note-toggle');
            expect(toggle).toHaveAttribute('aria-expanded', 'false');
            const note = screen.getByRole('note');
            // Collapsed: the details are still available to assistive tech.
            expect(note).toHaveTextContent(/saved to your account/i);
            const details = document.getElementById('job-search-data-note-details')!;
            expect(details).toHaveClass('visually-hidden');
            fireEvent.click(toggle);
            expect(toggle).toHaveAttribute('aria-expanded', 'true');
            expect(toggle).toHaveTextContent('Hide details');
            expect(document.getElementById('job-search-data-note-details')!).not.toHaveClass('visually-hidden');
            fireEvent.click(toggle);
            expect(toggle).toHaveAttribute('aria-expanded', 'false');
        });

        test('exposes a consistent keyboard-focus ring class on chat controls', async () => {
            await renderChat([userTurn('failed question', '123e4567-e89b-42d3-a456-426614174000', 'failed')]);
            expect(screen.getByTestId('retry-response-button')).toHaveClass('chat-focus');
            expect(screen.getByTestId('edit-as-new-button')).toHaveClass('chat-focus');
            expect(screen.getByRole('button', {name: 'Send message'})).toHaveClass('chat-focus');
            expect(screen.getByTestId('data-note-toggle')).toHaveClass('chat-focus');
        });

        test('submit is gated on a conversation and non-empty input', async () => {
            await renderChat();
            const send = screen.getByRole('button', {name: 'Send message'});
            expect(send).toBeDisabled();
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: '  '}});
            expect(send).toBeDisabled();
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'remote work'}});
            expect(send).toBeEnabled();
        });
    });

    describe('scroll behavior', () => {
        let scrollTo: jest.Mock;

        beforeEach(() => {
            scrollTo = jest.fn();
            Object.defineProperty(HTMLElement.prototype, 'scrollTo', {
                configurable: true,
                value: scrollTo,
            });
        });

        afterEach(() => {
            delete (HTMLElement.prototype as unknown as {scrollTo?: unknown}).scrollTo;
        });

        function setScrollMetrics(element: HTMLElement, values: {scrollHeight: number; scrollTop: number; clientHeight: number}) {
            Object.defineProperties(element, {
                scrollHeight: {configurable: true, value: values.scrollHeight},
                scrollTop: {configurable: true, value: values.scrollTop, writable: true},
                clientHeight: {configurable: true, value: values.clientHeight},
            });
        }

        test('scrolls initial history to the latest message with motion preference', async () => {
            window.matchMedia = jest.fn().mockReturnValue({matches: false} as MediaQueryList);
            await renderChat([assistantMessage(1, 'latest')]);
            const history = screen.getByLabelText('Message history');
            // The initial scroll runs in an effect after the resume commit;
            // await it so the assertion never races the effect (CI coverage).
            await waitFor(() => expect(scrollTo).toHaveBeenCalledWith({top: history.scrollHeight, behavior: 'smooth'}));
            delete (window as unknown as {matchMedia?: unknown}).matchMedia;
        });

        test('uses instant scrolling when reduced motion is preferred', async () => {
            window.matchMedia = jest.fn().mockReturnValue({matches: true} as MediaQueryList);
            await renderChat([assistantMessage(1, 'latest')]);
            const history = screen.getByLabelText('Message history');
            await waitFor(() => expect(scrollTo).toHaveBeenCalledWith({top: history.scrollHeight, behavior: 'auto'}));
            delete (window as unknown as {matchMedia?: unknown}).matchMedia;
        });

        test('shows jump-to-latest and preserves position when the reader scrolls up', async () => {
            await renderChat([assistantMessage(1, 'older'), assistantMessage(2, 'latest')]);
            const history = screen.getByLabelText('Message history');
            setScrollMetrics(history, {scrollHeight: 1000, scrollTop: 100, clientHeight: 200});
            fireEvent.scroll(history);
            expect(await screen.findByTestId('jump-to-latest')).toHaveTextContent('New messages');

            scrollTo.mockClear();
            fireEvent.click(screen.getByTestId('jump-to-latest'));
            expect(scrollTo).toHaveBeenCalledWith({top: 1000, behavior: 'auto'});
            expect(screen.queryByTestId('jump-to-latest')).not.toBeInTheDocument();
        });

        test('auto-scrolls new pending content only when already near the bottom', async () => {
            await renderChat([assistantMessage(1, 'ready')]);
            const history = screen.getByLabelText('Message history');
            setScrollMetrics(history, {scrollHeight: 1000, scrollTop: 752, clientHeight: 200});
            fireEvent.scroll(history);
            scrollTo.mockClear();

            const response = jsonResponse({message: assistantMessage(3, 'reply'), preferences_changed: false}, 201);
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(response);
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'hello'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText('reply');
            expect(scrollTo).toHaveBeenCalled();
            expect(screen.queryByTestId('jump-to-latest')).not.toBeInTheDocument();
        });

        test('does not auto-scroll newly appended content while reading older messages', async () => {
            await renderChat([assistantMessage(1, 'ready')]);
            const history = screen.getByLabelText('Message history');
            setScrollMetrics(history, {scrollHeight: 1000, scrollTop: 100, clientHeight: 200});
            fireEvent.scroll(history);
            scrollTo.mockClear();

            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(3, 'reply'), preferences_changed: false}, 201),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'hello'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText('reply');
            expect(scrollTo).not.toHaveBeenCalled();
            expect(screen.getByTestId('jump-to-latest')).toBeInTheDocument();
        });

        test('rechecks the bottom after viewport resize without moving older history', async () => {
            await renderChat([assistantMessage(1, 'ready')]);
            const history = screen.getByLabelText('Message history');
            setScrollMetrics(history, {scrollHeight: 1000, scrollTop: 100, clientHeight: 200});
            fireEvent.scroll(history);
            scrollTo.mockClear();
            fireEvent(window, new Event('resize'));
            expect(scrollTo).not.toHaveBeenCalled();
            expect(screen.getByTestId('jump-to-latest')).toBeInTheDocument();
        });
    });

    describe('submit / pending / success', () => {
        test('submits a message, shows pending state, and renders the assistant reply', async () => {
            await renderChat();
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            // Hold the POST response so the pending state is observable.
            let resolvePost: (response: Response) => void = () => {};
            (global.fetch as jest.Mock).mockImplementationOnce(
                () => new Promise<Response>((resolve) => { resolvePost = resolve; }),
            );

            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'I need remote'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));

            // The pre-send status re-check resolves before the turn starts.
            await waitFor(() =>
                expect(screen.getByLabelText('Message history')).toHaveAttribute('aria-busy', 'true'),
            );
            await act(async () => {
                resolvePost(jsonResponse({
                    message: assistantMessage(3, 'Consider remote-friendly companies.'),
                    preferences_changed: true,
                }, 201));
            });
            await screen.findByText('Consider remote-friendly companies.');
            expect(screen.getByText('I need remote')).toBeInTheDocument();

            // Preference-change disclosure is announced.
            expect(await screen.findByText(/preferences were updated/i)).toBeInTheDocument();
            expect(screen.getByRole('status', {name: 'Preference update'})).toHaveAttribute('aria-describedby', 'preference-update-help');
            expect(screen.getByText(/correct or remove a preference/i)).toBeInTheDocument();

            // Input is cleared and refocused; the form is no longer busy.
            expect(screen.getByLabelText('Message')).toHaveValue('');
            expect(screen.getByLabelText('Message')).toHaveFocus();

            const body = postBodies(global.fetch as jest.Mock, messageUrl())[0];
            expect(body.content).toBe('I need remote');
            expect(body.idempotency_key).toBeTruthy();
        });

        test('submits via the form on Enter', async () => {
            await renderChat();
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(4, 'Enter works'), preferences_changed: false}, 201),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'just pressing enter'}});
            fireEvent.submit(screen.getByRole('textbox', {name: 'Message'}));
            await screen.findByText('Enter works');
        });

        test('marks the composer row pending while the Stop control is rendered', async () => {
            await renderChat();
            let resolveReply: (v: unknown) => void = () => {};
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockImplementationOnce(
                () => new Promise((r) => { resolveReply = r; }),
            );

            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'hold on'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));

            // While Stop is present the row carries chat-composer-pending; CSS
            // compacts Send to an icon-only ≥44px target on narrow screens,
            // and the button keeps its accessible name throughout. (The POST
            // follows the advisory pre-send status check, issue #457.)
            const row = screen.getByLabelText('Message').closest('.input-group');
            await waitFor(() => expect(row).toHaveClass('chat-composer-pending'));
            expect(screen.getByTestId('stop-button')).toBeInTheDocument();
            expect(screen.getByRole('button', {name: 'Send message'})).toBeInTheDocument();

            resolveReply(jsonResponse({message: assistantMessage(5, 'Replied'), preferences_changed: false}, 201));
            await screen.findByText('Replied');
            expect(row).not.toHaveClass('chat-composer-pending');
            expect(screen.queryByTestId('stop-button')).not.toBeInTheDocument();
            // Ordinary states restore the visible Send label.
            expect(screen.getByRole('button', {name: 'Send message'})).toHaveTextContent('Send');
        });
    });

    describe('error & retry', () => {
        test('surfaces a stable error and retries with the same idempotency key', async () => {
            await renderChat();
            const mockFetch = global.fetch as jest.Mock;
            mockFetch
                .mockResolvedValueOnce(statusResponse('ready'))
                .mockResolvedValueOnce(
                    jsonResponse({
                        error: {type: 'service_error', message: 'We could not respond right now.', request_id: 'abc'},
                    }, 500),
                )
                .mockResolvedValueOnce(
                    jsonResponse({message: assistantMessage(5, 'All set.'), preferences_changed: false}, 201),
                );

            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'critical message'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));

            const alert = await screen.findByRole('alert');
            expect(alert).toHaveTextContent('We could not respond right now.');
            expect(screen.getByTestId('retry-button')).toBeInTheDocument();

            const firstBody = postBodies(mockFetch, messageUrl())[0];
            expect(firstBody.content).toBe('critical message');
            expect(firstBody.idempotency_key).toBeTruthy();

            fireEvent.click(screen.getByTestId('retry-button'));
            await screen.findByText('All set.');

            const arrivals = postBodies(mockFetch, messageUrl());
            expect(arrivals).toHaveLength(2);
            expect(arrivals[1].idempotency_key).toBe(firstBody.idempotency_key);
            expect(arrivals[1].content).toBe('critical message');
        });

        test('surfaces assistant_unavailable error with data-error-type and retries', async () => {
            await renderChat();
            const mockFetch = global.fetch as jest.Mock;
            mockFetch
                .mockResolvedValueOnce(statusResponse('ready'))
                .mockResolvedValueOnce(
                    jsonResponse({
                        error: {type: 'assistant_unavailable', message: 'The assistant is not available right now.', request_id: 'rid-503'},
                    }, 503),
                )
                .mockResolvedValueOnce(
                    jsonResponse({message: assistantMessage(6, 'Back online.'), preferences_changed: false}, 201),
                );

            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'config test'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));

            const alert = await screen.findByRole('alert');
            expect(alert).toHaveTextContent('The assistant is not available right now.');
            expect(alert).toHaveAttribute('data-error-type', 'assistant_unavailable');
            expect(screen.getByTestId('retry-button')).toBeInTheDocument();

            fireEvent.click(screen.getByTestId('retry-button'));
            await screen.findByText('Back online.');
            const arrivals = postBodies(mockFetch, messageUrl());
            expect(arrivals).toHaveLength(2);
            expect(arrivals[1].idempotency_key).toBe(arrivals[0].idempotency_key);
        });

        test('surfaces provider_timeout error with data-error-type', async () => {
            await renderChat();
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({
                    error: {type: 'provider_timeout', message: 'The assistant took too long.', request_id: 'rid-504'},
                }, 504),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'slow'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const alert = await screen.findByRole('alert');
            expect(alert).toHaveAttribute('data-error-type', 'provider_timeout');
            expect(screen.getByTestId('retry-button')).toBeInTheDocument();
        });

        test('surfaces cost_limit error with data-error-type', async () => {
            await renderChat();
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({
                    error: {type: 'cost_limit', message: 'Usage limit reached.', request_id: 'rid-429'},
                }, 429),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'too many'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const alert = await screen.findByRole('alert');
            expect(alert).toHaveAttribute('data-error-type', 'cost_limit');
            expect(alert).toHaveTextContent('Usage limit reached.');
            expect(screen.getByTestId('retry-button')).toBeInTheDocument();
        });

        test('surfaces invalid_output error with data-error-type', async () => {
            await renderChat();
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({
                    error: {type: 'invalid_output', message: 'Unexpected response.', request_id: 'rid-500'},
                }, 500),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'bad output'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const alert = await screen.findByRole('alert');
            expect(alert).toHaveAttribute('data-error-type', 'invalid_output');
        });

        test('surfaces unexpected_error with data-error-type', async () => {
            await renderChat();
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({
                    error: {type: 'unexpected_error', message: 'Unexpected error.', request_id: 'rid-500b'},
                }, 500),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'unexpected'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const alert = await screen.findByRole('alert');
            expect(alert).toHaveAttribute('data-error-type', 'unexpected_error');
        });

        test('clears error type when a new message is sent', async () => {
            await renderChat();
            const mockFetch = global.fetch as jest.Mock;
            mockFetch.mockResolvedValueOnce(statusResponse('ready'))
                .mockResolvedValueOnce(
                    jsonResponse({error: {type: 'provider_timeout', message: 'Timeout.'}}, 504),
                ).mockResolvedValueOnce(
                    jsonResponse({message: assistantMessage(7, 'ok'), preferences_changed: false}, 201),
                );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'first'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const alert1 = await screen.findByRole('alert');
            expect(alert1).toHaveAttribute('data-error-type', 'provider_timeout');
            // Retry with same key clears the error and succeeds.
            fireEvent.click(screen.getByTestId('retry-button'));
            await screen.findByText('ok');
            expect(screen.queryByRole('alert')).not.toBeInTheDocument();
        });

        test('recovers a conversation_closed turn onto the new active conversation with the same key (issue #487)', async () => {
            await renderChat();
            const mockFetch = global.fetch as jest.Mock;
            mockFetch
                .mockResolvedValueOnce(statusResponse('ready'))
                .mockResolvedValueOnce(
                    jsonResponse({
                        error: {
                            type: 'conversation_closed',
                            message: 'This conversation was reset or deleted while the assistant was responding.',
                            request_id: 'rid-409',
                        },
                    }, 409),
                )
                // Recovery: switch to the user's active conversation (the
                // reset's fresh one, or a newly created one after delete).
                .mockResolvedValueOnce(jsonResponse(emptyConversation(43)))
                // The retained turn replays onto the new conversation.
                .mockResolvedValueOnce(
                    jsonResponse(
                        {message: assistantMessage(8, 'Recovered reply.'), preferences_changed: false},
                        201,
                    ),
                );

            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'retained turn'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));

            // The turn is retried transparently onto the fresh conversation:
            // the reply arrives and no error is surfaced.
            const reply = await screen.findByText('Recovered reply.');
            expect(reply).toBeInTheDocument();
            expect(screen.queryByRole('alert')).not.toBeInTheDocument();

            const originalPosts = mockFetch.mock.calls.filter(([url, init]) =>
                String(url) === messageUrl() && init && (init as RequestInit).method === 'POST');
            const retryPosts = mockFetch.mock.calls.filter(([url, init]) =>
                String(url) === '/api/agent/conversations/43/' && init && (init as RequestInit).method === 'POST');
            expect(originalPosts).toHaveLength(1);
            expect(retryPosts).toHaveLength(1);
            const firstBody = JSON.parse((originalPosts[0][1] as RequestInit).body as string);
            const retryBody = JSON.parse((retryPosts[0][1] as RequestInit).body as string);
            // Same retained turn: same content, same idempotency key.
            expect(retryBody.idempotency_key).toBe(firstBody.idempotency_key);
            expect(retryBody.content).toBe('retained turn');
        });

        test('surfaces the error when conversation_closed recovery fails', async () => {
            await renderChat();
            const mockFetch = global.fetch as jest.Mock;
            mockFetch
                .mockResolvedValueOnce(statusResponse('ready'))
                .mockResolvedValueOnce(
                    jsonResponse({
                        error: {
                            type: 'conversation_closed',
                            message: 'This conversation was reset or deleted while the assistant was responding.',
                            request_id: 'rid-409b',
                        },
                    }, 409),
                )
                .mockRejectedValueOnce(new Error('start-failed'));

            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'unlucky turn'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));

            const alert = await screen.findByRole('alert');
            expect(alert).toHaveTextContent(
                'This conversation was reset or deleted while the assistant was responding.',
            );
            // The manual Retry offer remains so the user can resend.
            expect(screen.getByTestId('retry-button')).toBeInTheDocument();
        });
    });

    describe('reset / delete controls', () => {
        test('delete removes the conversation and resets the UI', async () => {
            await renderChat([userMessage('done with this')]);
            window.confirm = jest.fn().mockReturnValue(true);
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({deleted: true}),
            );

            fireEvent.click(screen.getByRole('button', {name: 'Delete conversation'}));
            await waitFor(() => expect(screen.getByTestId('empty-history')).toBeInTheDocument());
            expect(screen.queryByText('done with this')).not.toBeInTheDocument();
        });
    });
});
describe('additional JobSearchChat coverage', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        document.cookie = '';
    });

    describe('resume / init failure paths', () => {
        test('shows an init error and the start button when resume fails (non-404)', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({detail: 'boom'}, 500));
            render(<JobSearchChat/>);
            expect(await screen.findByText(/could not load your conversation/i)).toBeInTheDocument();
            expect(screen.getByRole('button', {name: 'Start a conversation'})).toBeInTheDocument();
        });

        test('a resume 404 renders the usable empty state WITHOUT creating a conversation (issue #472)', async () => {
            const mock = global.fetch as jest.Mock;
            // Mount order: status check first, then GET resume → 404. No POST
            // follows: opening the assistant must not create a row.
            mock.mockResolvedValueOnce(statusResponse('ready'));
            mock.mockResolvedValueOnce(jsonResponse({}, 404));
            render(<JobSearchChat/>);
            await screen.findByLabelText('Message');
            await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
            expect(screen.getByTestId('empty-history')).toBeInTheDocument();
            expect(screen.queryByText(/could not load/i)).not.toBeInTheDocument();
            const posts = mock.mock.calls
                .map(([url, init]) => ({url: String(url), init: init as RequestInit}))
                .filter((c) => c.init?.method === 'POST');
            expect(posts).toHaveLength(0);
        });

        test('createOnMount: shows init error when auto-create after 404 also fails', async () => {
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(statusResponse('ready'));
            // First call: GET resume → 404 (no existing conversation).
            mock.mockResolvedValueOnce(jsonResponse({}, 404));
            // Second call: POST create → 500 (server error).
            mock.mockResolvedValueOnce(jsonResponse({}, 500));
            render(<JobSearchChat createOnMount={true}/>);
            expect(await screen.findByText(/could not start a conversation/i)).toBeInTheDocument();
            expect(screen.queryByLabelText('Message')).toBeDisabled();
        });

        test('starting a new conversation from the error state works', async () => {
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(statusResponse('ready'))
                .mockResolvedValueOnce(jsonResponse({detail: 'down'}, 503))
                .mockResolvedValueOnce(jsonResponse(emptyConversation(11), 201));
            render(<JobSearchChat/>);
            await screen.findByText(/could not load your conversation/i, {}, {timeout: 5000});
            const startBtn = screen.getByRole('button', {name: 'Start a conversation'});
            fireEvent.click(startBtn);
            await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled(), {timeout: 3000});
            expect(screen.queryByText(/could not load/i)).not.toBeInTheDocument();
            const posts = mock.mock.calls
                .map(([, init]) => (init as RequestInit).body)
                .filter((body): body is string => typeof body === 'string')
                .map((body) => JSON.parse(body));
            expect(posts.some((b) => b.create_new === true)).toBe(true);
        });
    });

    describe('security & runtime branches', () => {
        test('includes the CSRF token header on state-changing requests', async () => {
            document.cookie = 'csrftoken=abc123token';
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
            render(<JobSearchChat/>);
            await screen.findByLabelText('Message');
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(8, 'ok'), preferences_changed: false}, 201),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'hi'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText('ok');
            const sendCall = (global.fetch as jest.Mock).mock.calls.find(([url]) =>
                String(url).includes('/api/agent/conversations/42/'),
            );
            const headers = (sendCall[1] as RequestInit).headers as Record<string, string>;
            expect(headers['X-CSRFToken']).toBe('abc123token');
            expect(headers['Content-Type']).toBe('application/json');
        });

        test('uses the idempotency-key fallback when crypto.randomUUID is unavailable', async () => {
            const original = Object.getOwnPropertyDescriptor(global.crypto, 'randomUUID');
            Object.defineProperty(global.crypto, 'randomUUID', {value: undefined, configurable: true});
            try {
                (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
                (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
                render(<JobSearchChat/>);
                await screen.findByLabelText('Message');
                (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
                (global.fetch as jest.Mock).mockResolvedValueOnce(
                    jsonResponse({message: assistantMessage(9, 'fallback'), preferences_changed: false}, 201),
                );
                fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'r'}});
                fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
                await screen.findByText('fallback');
                const body = postBodies(global.fetch as jest.Mock, messageUrl())[0];
                expect(body.idempotency_key).toMatch(/^[0-9a-fA-F-]{36}$/);
            } finally {
                if (original) {
                    Object.defineProperty(global.crypto, 'randomUUID', original);
                } else {
                    // @ts-expect-error restoring absence
                    delete global.crypto.randomUUID;
                }
            }
        });

        test('marks the last rendered message as an aria-live region', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [userMessage('first'), assistantMessage(2, 'second')])),
            );
            render(<JobSearchChat/>);
            await screen.findByText('second');
            const history = screen.getByLabelText('Message history');
            const last = history.lastElementChild as HTMLElement;
            expect(last).toHaveAttribute('aria-live', 'polite');
        });
    });

    describe('optimistic rollback & preference disclosure', () => {
        test('treats a non-JSON error as uncertain: question stays, check offered, no saved claim', async () => {
            window.localStorage.clear();
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [assistantMessage(0, 'ready')])),
            );
            render(<JobSearchChat/>);
            await screen.findByText('ready');
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(statusResponse('ready'));
            mock.mockResolvedValueOnce(new Response('plain text error', {status: 500}));
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'boom'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText(/request failed \(500\)/i);
            // Issue #458 (adversarial review): a proxy/gateway error is
            // uncertain — the server may or may not have the turn. The
            // question stays visible with an honest "not arrived yet"
            // panel and a Check action, never a false "your message is
            // saved" claim, and the marker stands for reconciliation.
            expect(screen.getByText('boom')).toBeInTheDocument();
            expect(screen.getByTestId('pending-turn')).toBeInTheDocument();
            expect(screen.getByTestId('pending-turn')).toHaveTextContent(/has not arrived/i);
            expect(screen.getByTestId('check-response-button')).toBeInTheDocument();
            expect(screen.queryByTestId('failed-turn')).not.toBeInTheDocument();
            const key = lastPostedKey(mock);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
            window.localStorage.clear();
        });

        test('dismisses the preference-update notice', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
            render(<JobSearchChat/>);
            await screen.findByLabelText('Message');
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(3, 'noted'), preferences_changed: true}, 201),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'prefs'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const notice = await screen.findByText(/preferences were updated/i);
            fireEvent.click(screen.getByLabelText('Dismiss preference notice'));
            await waitFor(() => expect(notice).not.toBeInTheDocument());
        });
    });

    describe('export', () => {
        test('downloads the conversation as a JSON file', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [userMessage('exportable')])),
            );
            render(<JobSearchChat/>);
            await screen.findByText('exportable');
            const createUrl = jest.fn().mockReturnValue('blob:mock');
            const revoke = jest.fn();
            Object.defineProperty(URL, 'createObjectURL', {value: createUrl, configurable: true});
            Object.defineProperty(URL, 'revokeObjectURL', {value: revoke, configurable: true});
            const click = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
            (global.fetch as jest.Mock).mockResolvedValueOnce(new Response(new Blob(['{}'], {type: 'application/json'})));

            fireEvent.click(screen.getByRole('button', {name: 'Export chat'}));
            await waitFor(() => expect(click).toHaveBeenCalled());
            expect(createUrl).toHaveBeenCalled();
            expect(revoke).toHaveBeenCalled();
            expect(screen.queryByText(/could not export/i)).not.toBeInTheDocument();
        });

        test('surfaces an error when export fails', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [userMessage('x')])),
            );
            render(<JobSearchChat/>);
            await screen.findByText('x');
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 500));
            fireEvent.click(screen.getByRole('button', {name: 'Export chat'}));
            expect(await screen.findByText(/could not export your conversation/i)).toBeInTheDocument();
        });
    });

    describe('createOnMount (issue #472)', () => {
        test('default (false): a 404 from the resume GET issues no POST and renders the empty state', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 404));
            render(<JobSearchChat/>);
            await screen.findByTestId('empty-history');
            const posts = (global.fetch as jest.Mock).mock.calls.filter(
                (call) => (call[1] as RequestInit | undefined)?.method === 'POST'
                    && String(call[0]).includes('/api/agent/conversations/'),
            );
            expect(posts).toHaveLength(0);
        });

        test('sending the first message from the not-started state creates the conversation, then sends', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 404));
            render(<JobSearchChat/>);
            await screen.findByTestId('empty-history');
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(7, 'hi there'), preferences_changed: false}, 201),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'hello'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText('hi there');
            const posts = (global.fetch as jest.Mock).mock.calls.filter(
                (call) => (call[1] as RequestInit | undefined)?.method === 'POST'
                    && String(call[0]) === '/api/agent/conversations/',
            );
            expect(posts).toHaveLength(1);
            expect(JSON.parse((posts[0][1] as RequestInit).body as string)).toEqual({create_new: true});
        });

        test('a failed create on first send surfaces an init error and sends nothing', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 404));
            render(<JobSearchChat/>);
            await screen.findByTestId('empty-history');
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 500));
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'hello'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            expect(await screen.findByText(/could not start a conversation/i)).toBeInTheDocument();
            const sends = (global.fetch as jest.Mock).mock.calls.filter(
                (call) => String(call[0]).match(/\/api\/agent\/conversations\/\d+\//),
            );
            expect(sends).toHaveLength(0);
        });

        test('createOnMount={true} still POSTs on a 404', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 404));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
            render(<JobSearchChat createOnMount={true}/>);
            await screen.findByLabelText('Message');
            const posts = (global.fetch as jest.Mock).mock.calls.filter(
                (call) => (call[1] as RequestInit | undefined)?.method === 'POST'
                    && String(call[0]) === '/api/agent/conversations/',
            );
            expect(posts).toHaveLength(1);
        });
    });

    describe('DOM bootstrap', () => {
        test('the component module no longer self-mounts (issue #472: the workspace owns mounting)', () => {
            const div = document.createElement('div');
            div.id = 'job-search-chat';
            document.body.appendChild(div);
            // The module's own DOMContentLoaded handler calls createRoot()
            // directly (it is not RTL's render()), so this is the only way
            // to get a handle to unmount it. Without an explicit unmount the
            // component's document-level listeners (issue #465's
            // crank:auth-hydrated / crank:private-state-purged handlers)
            // would keep running for the rest of the test file and react to
            // events other tests dispatch on document.
            const reactDomClient = require('react-dom/client') as {createRoot: (c: Element) => {unmount: () => void}};
            const realCreateRoot = reactDomClient.createRoot;
            let capturedRoot: {unmount: () => void} | null = null;
            const spy = jest.spyOn(reactDomClient, 'createRoot').mockImplementation((container) => {
                capturedRoot = realCreateRoot(container as Element);
                return capturedRoot;
            });
            try {
                document.dispatchEvent(new Event('DOMContentLoaded', {bubbles: true}));
                expect(div.querySelector('.card.bg-dark')).toBeNull();
            } finally {
                act(() => { capturedRoot?.unmount(); });
                spy.mockRestore();
                document.body.removeChild(div);
            }
        });
    });
});

describe('additional JobSearchChat coverage -- control/error paths', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('uses crypto.randomUUID for the idempotency key when available', async () => {
        const original = Object.getOwnPropertyDescriptor(global.crypto, 'randomUUID');
        Object.defineProperty(global.crypto, 'randomUUID', {value: () => 'fixed-uuid-1234', configurable: true});
        try {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
            render(<JobSearchChat/>);
            await screen.findByLabelText('Message');
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(7, 'sure'), preferences_changed: false}, 201),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'ok'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText('sure');
            const body = postBodies(global.fetch as jest.Mock, messageUrl())[0];
            expect(body.idempotency_key).toBe('fixed-uuid-1234');
        } finally {
            if (original) {
                Object.defineProperty(global.crypto, 'randomUUID', original);
            } else {
                delete (global.crypto as unknown as {randomUUID?: unknown}).randomUUID;
            }
        }
    });

    test('surfaces an error when starting a new conversation fails', async () => {
        const mock = global.fetch as jest.Mock;
        mock.mockResolvedValueOnce(statusResponse('ready'))
            .mockResolvedValueOnce(jsonResponse({detail: 'down'}, 503))
            .mockResolvedValueOnce(jsonResponse({}, 500));
        render(<JobSearchChat/>);
        await screen.findByText(/could not load your conversation/i);
        fireEvent.click(screen.getByRole('button', {name: 'Start a conversation'}));
        await screen.findByText(/could not start a conversation/i);
    });

    test('reset succeeds when confirmed', async () => {
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [userMessage('old')])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('old');
        window.confirm = jest.fn().mockReturnValue(true);
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(7)));
        fireEvent.click(screen.getByRole('button', {name: 'Reset chat'}));
        await waitFor(() => expect(screen.getByTestId('empty-history')).toBeInTheDocument());
        expect(screen.queryByText('old')).not.toBeInTheDocument();
    });

    test('reset surfaces an error when it fails', async () => {
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [userMessage('keep')])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('keep');
        window.confirm = jest.fn().mockReturnValue(true);
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 500));
        fireEvent.click(screen.getByRole('button', {name: 'Reset chat'}));
        await screen.findByText(/could not reset the conversation/i);
        expect(screen.getByText('keep')).toBeInTheDocument();
    });

    test('delete surfaces an error when it fails', async () => {
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [userMessage('del')])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('del');
        window.confirm = jest.fn().mockReturnValue(true);
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 500));
        fireEvent.click(screen.getByRole('button', {name: 'Delete conversation'}));
        await screen.findByText(/could not delete the conversation/i);
    });
});

describe('JobSearchChat result cards (issue #396)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    function makeResults() {
        return {
            jobs: [{
                id: 101,
                title: 'Senior Engineer',
                organization_name: 'Acme Inc',
                location: 'San Francisco',
                remote: true,
                compensation: {min: 150000, max: 250000, currency: 'USD', interval: 'year'},
                canonical_url: 'https://acme.example/jobs/101',
                observed_at: '2024-08-01T00:00:00',
                updated_at: '2024-08-02T00:00:00',
            }],
            organizations: [{
                id: 1,
                name: 'Acme Inc',
                url: 'https://acme.example',
                funding_round: 'A',
                rto_policy: 'R',
            }],
        };
    }

    test('renders job and org cards when results are present', async () => {
        const results = makeResults();
        const msg = assistantMessage(5, 'Check these out.', false, results);
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Check these out.');
        // Job card
        expect(screen.getByText('Senior Engineer')).toBeInTheDocument();
        expect(screen.getByText(/Acme Inc.*San Francisco/)).toBeInTheDocument();
        expect(screen.getByText(/150,000-250,000 USD year/)).toBeInTheDocument();
        // Org card
        expect(screen.getByText('Organizations')).toBeInTheDocument();
        // Links are safe (rel=noopener noreferrer)
        const jobLink = screen.getByLabelText(/Open listing for Senior Engineer/i);
        expect(jobLink).toHaveAttribute('href', 'https://acme.example/jobs/101');
        expect(jobLink).toHaveAttribute('rel', 'noopener noreferrer');
        expect(jobLink).toHaveAttribute('target', '_blank');
    });

    test('no result cards when results is null', async () => {
        const msg = assistantMessage(5, 'Just text, no cards.', false, null);
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Just text, no cards.');
        expect(screen.queryByTestId('result-cards')).not.toBeInTheDocument();
    });

    test('no result cards when results have empty arrays', async () => {
        const msg = assistantMessage(5, 'No matches found.', false, {jobs: [], organizations: []});
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('No matches found.');
        expect(screen.queryByTestId('result-cards')).not.toBeInTheDocument();
    });

    test('job cards are keyboard focusable with screen-reader labels', async () => {
        const results = makeResults();
        const msg = assistantMessage(5, 'Here you go.', false, results);
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Here you go.');
        const jobCard = screen.getByRole('article', {name: /Job: Senior Engineer at Acme Inc/i});
        expect(jobCard).toHaveAttribute('tabindex', '0');
        const orgCard = screen.getByRole('article', {name: /Organization: Acme Inc/i});
        expect(orgCard).toHaveAttribute('tabindex', '0');
    });

    test('history reload shows the same cards', async () => {
        const results = makeResults();
        const msg = assistantMessage(5, 'Reload test.', false, results);
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Reload test.');
        expect(screen.getByText('Senior Engineer')).toBeInTheDocument();
        expect(screen.getByText('Acme Inc')).toBeInTheDocument();
    });

    test('new reply with results renders cards after submit', async () => {
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [userMessage('jobs?')])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('jobs?');
        const results = makeResults();
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse({
                message: assistantMessage(10, 'Found one!', false, results),
                preferences_changed: false,
            }, 201),
        );
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'show jobs'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await screen.findByText('Found one!');
        expect(screen.getByText('Senior Engineer')).toBeInTheDocument();
    });

    test('org card shows funding round and RTO labels', async () => {
        const results = {
            jobs: [],
            organizations: [{
                id: 2, name: 'Globex', url: 'https://globex.example',
                funding_round: 'S', rto_policy: 'H',
            }],
        };
        const msg = assistantMessage(5, 'Check Globex.', false, results);
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Check Globex.');
        expect(screen.getByText('Seed')).toBeInTheDocument();
        expect(screen.getByText('Hybrid')).toBeInTheDocument();
    });

    test('job without compensation does not show comp line', async () => {
        const results = {
            jobs: [{
                id: 1, title: 'Dev', organization_name: 'X',
                location: 'NYC', remote: false, compensation: null,
                canonical_url: '', observed_at: null, updated_at: null,
            }],
            organizations: [],
        };
        const msg = assistantMessage(5, 'Simple job.', false, results);
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Simple job.');
        expect(screen.getByText('Dev')).toBeInTheDocument();
        // No link since canonical_url is empty
        expect(screen.queryByLabelText(/Open listing/i)).not.toBeInTheDocument();
    });

    test('job with only comp.min shows min+ format', async () => {
        const results = {
            jobs: [{
                id: 1, title: 'Dev', organization_name: 'X',
                location: 'NYC', remote: false,
                compensation: {min: 120000, max: null, currency: 'USD', interval: 'year'},
                canonical_url: '', observed_at: null, updated_at: null,
            }],
            organizations: [],
        };
        const msg = assistantMessage(5, 'Min only.', false, results);
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Min only.');
        expect(screen.getByText('120,000+ USD year')).toBeInTheDocument();
    });

    test('job with only comp.max shows up to max format', async () => {
        const results = {
            jobs: [{
                id: 1, title: 'Dev', organization_name: 'X',
                location: 'NYC', remote: false,
                compensation: {min: null, max: 180000, currency: 'EUR', interval: 'month'},
                canonical_url: '', observed_at: null, updated_at: null,
            }],
            organizations: [],
        };
        const msg = assistantMessage(5, 'Max only.', false, results);
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Max only.');
        expect(screen.getByText('up to 180,000 EUR month')).toBeInTheDocument();
    });

    test('result cards do not cause horizontal overflow at 390px', async () => {
        const results = {
            jobs: [{
                id: 1, title: 'A'.repeat(100), organization_name: 'B'.repeat(50),
                location: 'C'.repeat(50), remote: true, compensation: null,
                canonical_url: 'https://example.com/' + 'd'.repeat(200),
                observed_at: null, updated_at: null,
            }],
            organizations: [],
        };
        const msg = assistantMessage(5, 'Long.', false, results);
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Long.');
        const cards = screen.getByTestId('result-cards');
        expect(cards).toBeInTheDocument();
        // Check that job-card elements have overflow:hidden
        const jobCard = screen.getByRole('article', {name: /Job:/i});
        expect(jobCard).toHaveStyle({overflow: 'hidden', maxWidth: '100%'});
    });
});

// ── Textarea composer (issue #407) ──────────────────────────────────────

describe('textarea composer', () => {
    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('renders a textarea instead of a single-line input', async () => {
        await renderChat();
        const textarea = screen.getByRole('textbox', {name: 'Message'});
        expect(textarea.tagName).toBe('TEXTAREA');
        expect(textarea).toHaveAttribute('rows', '1');
    });

    test('Enter submits the message', async () => {
        await renderChat();
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse({message: assistantMessage(5, 'reply'), preferences_changed: false}, 201),
        );
        const textarea = screen.getByRole('textbox', {name: 'Message'});
        fireEvent.change(textarea, {target: {value: 'hello world'}});
        fireEvent.keyDown(textarea, {key: 'Enter', shiftKey: false});
        await screen.findByText('reply');
        expect(screen.getByText('reply')).toBeInTheDocument();
    });

    test('Enter does not submit when IME composition is active', async () => {
        await renderChat();
        const textarea = screen.getByRole('textbox', {name: 'Message'});
        fireEvent.change(textarea, {target: {value: 'nihao'}});
        fireEvent.keyDown(textarea, {
            key: 'Enter',
            shiftKey: false,
            nativeEvent: {isComposing: true} as KeyboardEvent,
        });
        expect(textarea).toHaveValue('nihao');
    });

    test('Shift+Enter does not submit the form', async () => {
        await renderChat();
        const textarea = screen.getByRole('textbox', {name: 'Message'});
        fireEvent.change(textarea, {target: {value: 'line one'}});
        fireEvent.keyDown(textarea, {key: 'Enter', shiftKey: true});
        // No submit occurred: input is unchanged and no pending/assistant state.
        expect(textarea).toHaveValue('line one');
    });

    test('auto-grows to fit content up to a max height', async () => {
        await renderChat();
        const textarea = screen.getByRole('textbox', {name: 'Message'}) as HTMLTextAreaElement;
        fireEvent.change(textarea, {target: {value: 'a'.repeat(200)}});
        // After the change, the effect should have set a height
        expect(textarea.style.height).toBeTruthy();
    });

    test('clamps the composed height to the max rows', async () => {
        // Pin the measured line-height so the 6-row ceiling is deterministic:
        // 24px * MAX_COMPOSER_ROWS(6) = 144px.
        jest.spyOn(window, 'getComputedStyle').mockImplementation(() =>
            ({lineHeight: '24px', getPropertyValue: () => ''} as unknown as CSSStyleDeclaration),
        );
        await renderChat();
        const textarea = screen.getByRole('textbox', {name: 'Message'}) as HTMLTextAreaElement;
        Object.defineProperty(textarea, 'scrollHeight', {configurable: true, writable: true, value: 500});
        fireEvent.change(textarea, {target: {value: 'a'.repeat(500)}});
        // scrollHeight (500) exceeds the 144px ceiling, so the composer must
        // clamp to the max height and scroll instead of growing unbounded.
        expect(textarea.style.height).toBe('144px');
        expect(textarea.style.overflowY).toBe('auto');
    });

    test('recalculates composer height when fonts finish loading', async () => {
        let settleFonts: () => void = () => {};
        const readyPromise = new Promise<void>((resolve) => { settleFonts = resolve; });
        // Stub document.fonts with a FontFaceSet-like ready hook (jsdom may not
        // implement one), so the listener fires on an explicit font load.
        Object.defineProperty(document, 'fonts', {configurable: true, value: {ready: readyPromise}});
        try {
            const gcs = jest.spyOn(window, 'getComputedStyle');
            await renderChat();
            const textarea = screen.getByRole('textbox', {name: 'Message'}) as HTMLTextAreaElement;
            fireEvent.change(textarea, {target: {value: 'hello world'}});
            const callsBefore = gcs.mock.calls.length;
            // Fonts finishing must re-run the measure so the composer height reflects
            // the real loaded face rather than the pre-load fallback line-height.
            await act(async () => { settleFonts(); await readyPromise; });
            expect(gcs.mock.calls.length).toBeGreaterThan(callsBefore);
            gcs.mockRestore();
        } finally {
            delete (document as {fonts?: unknown}).fonts;
        }
    });

    test('composer still works when document.fonts has no ready hook', async () => {
        // Documents with a null/absent FontFaceSet ready promise (or a missing
        // ready) must not throw and the composer must still measure on mount.
        Object.defineProperty(document, 'fonts', {configurable: true, value: {ready: null}});
        try {
            await renderChat();
            const textarea = screen.getByRole('textbox', {name: 'Message'}) as HTMLTextAreaElement;
            fireEvent.change(textarea, {target: {value: 'a'.repeat(20)}});
            expect(textarea.style.height).toBeTruthy();
        } finally {
            delete (document as {fonts?: unknown}).fonts;
        }
    });

    test('registers height listeners on visualViewport when present', async () => {
        // jsdom does not expose visualViewport (it is undefined by default), so
        // the composer's nullish guard is exercised in every other test. Provide
        // a mock here so the non-null path — actually attaching the resize
        // listener to visualViewport — is covered too.
        const addEventListener = jest.fn();
        const removeEventListener = jest.fn();
        Object.defineProperty(window, 'visualViewport', {
            configurable: true,
            value: {height: 800, addEventListener, removeEventListener},
        });
        try {
            (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse(emptyConversation(42)),
            );
            const {unmount} = render(<JobSearchChat/>);
            await screen.findByLabelText('Message');
            await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
            // On mount the composer attaches its resize listener to the viewport.
            expect(addEventListener).toHaveBeenCalledWith('resize', expect.any(Function));
            // Pin the listener identity so we can assert symmetry on cleanup.
            const viewportListener = addEventListener.mock.calls.find(
                ([event]) => event === 'resize',
            )?.[1] as Function;
            expect(viewportListener).toBeDefined();
            unmount();
            // And detaches the SAME function reference on cleanup.
            expect(removeEventListener).toHaveBeenCalledWith('resize', viewportListener);
        } finally {
            delete (window as {visualViewport?: unknown}).visualViewport;
        }
    });

    test('resets to single-row height after send', async () => {
        await renderChat();
        (global.fetch as jest.Mock)
            .mockResolvedValueOnce(statusResponse('ready'))
            .mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(5, 'got it'), preferences_changed: false}, 201),
            );
        const textarea = screen.getByRole('textbox', {name: 'Message'}) as HTMLTextAreaElement;
        fireEvent.change(textarea, {target: {value: 'hello'}});
        fireEvent.keyDown(textarea, {key: 'Enter', shiftKey: false});
        await screen.findByText('got it');
        expect(textarea).toHaveValue('');
    });

    test('clears the composer input and resets height when a send fails', async () => {
        await renderChat();
        (global.fetch as jest.Mock)
            .mockResolvedValueOnce(statusResponse('ready'))
            .mockResolvedValueOnce(
                jsonResponse({error: {message: 'server error'}}, 500),
            );
        const textarea = screen.getByRole('textbox', {name: 'Message'}) as HTMLTextAreaElement;
        fireEvent.change(textarea, {target: {value: 'will fail'}});
        fireEvent.keyDown(textarea, {key: 'Enter', shiftKey: false});
        await screen.findByTestId('chat-error');
        expect(textarea).toHaveValue('');
        // With the input cleared, the composer collapses back to a single row.
        await waitFor(() => expect(textarea.style.overflowY).toBe('hidden'));
    });
});

// ── Advisory assistant-status notice + composer gating (issue #457) ─────

describe('assistant availability status (issue #457)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    function statusCalls(fetchMock: jest.Mock) {
        return fetchMock.mock.calls.filter(([url]) => String(url).includes('/api/agent/assistant-status/'));
    }

    async function renderWithStatus(state: AssistantState, actions: string[] = [], existingMessages: ChatMessage[] = []) {
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse(state, actions));
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42, existingMessages)));
        render(<JobSearchChat/>);
        await screen.findByLabelText('Message');
        await waitFor(() => expect(screen.getByLabelText('Message')).toBeInTheDocument());
    }

    test('fetches the status on mount before resuming', async () => {
        await renderWithStatus('ready');
        const calls = (global.fetch as jest.Mock).mock.calls.map(([url]) => String(url));
        expect(calls[0]).toContain('/api/agent/assistant-status/');
        expect(calls[1]).toContain('/api/agent/conversations/');
    });

    test('re-checks the status before each send (advisory pre-send gate)', async () => {
        await renderWithStatus('ready');
        expect(statusCalls(global.fetch as jest.Mock)).toHaveLength(1);
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse({message: assistantMessage(20, 'ok'), preferences_changed: false}, 201),
        );
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'hello'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await screen.findByText('ok');
        // One mount check + one pre-send check; the POST followed the re-check.
        expect(statusCalls(global.fetch as jest.Mock)).toHaveLength(2);
        // The lazy job-match availability fetch (issue #476) may follow the
        // reply; only the agent calls matter for the ordering check.
        const calls = (global.fetch as jest.Mock).mock.calls
            .map(([url]) => String(url))
            .filter((url) => !url.includes('/api/job-matches/status/'));
        expect(calls[calls.length - 2]).toContain('/api/agent/assistant-status/');
        expect(calls[calls.length - 1]).toContain(messageUrl());
    });

    test('an unrecognized future state renders no notice (defensive fallback)', async () => {
        // Forward compatibility: a server enum value this client does not know
        // must degrade to the healthy baseline, not break the chat.
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse({state: 'something_new', actions: ['browse_rankings'], checked_at: 'x'} as unknown as AssistantStatus),
        );
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
        render(<JobSearchChat/>);
        await screen.findByLabelText('Message');
        await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
        expect(screen.queryByTestId('assistant-status-notice')).not.toBeInTheDocument();
        expect(screen.getByLabelText('Message')).toBeEnabled();
    });

    test('ready state renders no notice and keeps the composer usable', async () => {
        await renderWithStatus('ready');
        expect(screen.queryByTestId('assistant-status-notice')).not.toBeInTheDocument();
        expect(screen.getByLabelText('Message')).toBeEnabled();
    });

    test('signed_out state renders no notice (unreachable on /chat/ but advisory-safe)', async () => {
        await renderWithStatus('signed_out');
        expect(screen.queryByTestId('assistant-status-notice')).not.toBeInTheDocument();
        expect(screen.getByLabelText('Message')).toBeEnabled();
    });

    test('replies_disabled disables the composer and shows the notice with rankings action', async () => {
        await renderWithStatus('replies_disabled', ['browse_rankings']);
        const notice = screen.getByTestId('assistant-status-notice');
        expect(notice).toHaveAttribute('data-status-state', 'replies_disabled');
        // Scannable state label plus supporting copy.
        expect(notice).toHaveTextContent('Assistant unavailable');
        expect(notice).toHaveTextContent(/replies are paused right now/i);
        expect(notice).toHaveTextContent(/saved preferences remain available/i);
        const rankings = screen.getByRole('link', {name: 'Browse company rankings'});
        expect(rankings).toHaveAttribute('href', '/');
        expect(rankings).toHaveClass('assistant-status-notice-action');
        // No re-check affordance for a policy state (re-checking is not meaningful).
        expect(screen.queryByTestId('assistant-status-retry')).not.toBeInTheDocument();
        expect(screen.getByLabelText('Message')).toBeDisabled();
        expect(screen.getByRole('button', {name: 'Send message'})).toBeDisabled();
    });

    test('replies_disabled notice leaks no internal reason strings', async () => {
        await renderWithStatus('replies_disabled');
        const notice = screen.getByTestId('assistant-status-notice').textContent || '';
        for (const needle of ['LLM', 'API_KEY', 'provider', 'INTERACTIVE_AGENT_ENABLED', 'demo']) {
            expect(notice.toLowerCase()).not.toContain(needle.toLowerCase());
        }
    });

    test('inventory_unavailable disables the composer and shows rankings plus saved-preferences guidance', async () => {
        await renderWithStatus('inventory_unavailable', ['browse_rankings']);
        const notice = screen.getByTestId('assistant-status-notice');
        expect(notice).toHaveAttribute('data-status-state', 'inventory_unavailable');
        expect(notice).toHaveTextContent('Assistant unavailable');
        expect(notice).toHaveTextContent(/no active job listings/i);
        // Required saved-preferences guidance (issue #457 UI contract), phrased
        // so it cannot be read as the disabled composer accepting input now.
        expect(notice).toHaveTextContent(/saved preferences are still available/i);
        expect(notice).toHaveTextContent(/update them here once listings return/i);
        expect(screen.getByRole('link', {name: 'Browse company rankings'})).toHaveAttribute('href', '/');
        expect(screen.queryByTestId('assistant-status-retry')).not.toBeInTheDocument();
        expect(screen.getByLabelText('Message')).toBeDisabled();
    });

    test('temporarily_unavailable shows the notice with a working retry affordance', async () => {
        await renderWithStatus('temporarily_unavailable', ['retry']);
        const notice = screen.getByTestId('assistant-status-notice');
        expect(notice).toHaveAttribute('data-status-state', 'temporarily_unavailable');
        expect(notice).toHaveTextContent(/temporarily unavailable/i);
        expect(screen.getByLabelText('Message')).toBeEnabled();
        const retry = screen.getByTestId('assistant-status-retry');
        // Semantic recovery action: solid danger button matching the
        // alert-danger treatment (visual review #472 round 1, item 12).
        expect(retry).toHaveClass('btn-danger');
        expect(retry).not.toHaveClass('btn-outline-warning');
        expect(retry).toHaveClass('assistant-status-notice-action');
        // Recovery: the re-check now reports ready, so the notice disappears.
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        fireEvent.click(retry);
        await waitFor(() =>
            expect(screen.queryByTestId('assistant-status-notice')).not.toBeInTheDocument(),
        );
    });

    test('refreshing shows the notice with a retry affordance and keeps the composer usable', async () => {
        await renderWithStatus('refreshing', ['retry']);
        const notice = screen.getByTestId('assistant-status-notice');
        expect(notice).toHaveAttribute('data-status-state', 'refreshing');
        expect(notice).toHaveTextContent(/refreshing/i);
        expect(screen.getByTestId('assistant-status-retry')).toBeInTheDocument();
        expect(screen.getByLabelText('Message')).toBeEnabled();
    });

    test('a gating status re-check before send blocks the POST and surfaces the notice', async () => {
        // Mount reports ready; the pre-send re-check reports a futile state.
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
        render(<JobSearchChat/>);
        await screen.findByLabelText('Message');
        await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            statusResponse('inventory_unavailable', ['browse_rankings']),
        );
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'doomed message'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        const notice = await screen.findByTestId('assistant-status-notice');
        expect(notice).toHaveAttribute('data-status-state', 'inventory_unavailable');
        expect(screen.getByLabelText('Message')).toBeDisabled();
        // Advisory gate: no message POST was attempted after the re-check.
        const posts = postBodies(global.fetch as jest.Mock, messageUrl());
        expect(posts).toHaveLength(0);
        // The draft is preserved, not lost.
        expect(screen.getByLabelText('Message')).toHaveValue('doomed message');
    });

    test('a failed status fetch leaves the chat fully usable (advisory-only)', async () => {
        (global.fetch as jest.Mock).mockRejectedValueOnce(new Error('status down'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
        render(<JobSearchChat/>);
        await screen.findByLabelText('Message');
        await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
        // No notice, composer not gated, and the send still proceeds: the POST
        // path is authoritative when the advisory check is unavailable.
        expect(screen.queryByTestId('assistant-status-notice')).not.toBeInTheDocument();
        (global.fetch as jest.Mock).mockRejectedValueOnce(new Error('status down again'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse({message: assistantMessage(21, 'still works'), preferences_changed: false}, 201),
        );
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'hi'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await screen.findByText('still works');
    });

    test('POST 503 after a ready status still surfaces the durable error envelope', async () => {
        // A runtime outage between the status check and the send must surface
        // through the unchanged POST error path (AC-6 composes with #458's
        // durable turn-state recovery, which is verified there).
        await renderWithStatus('ready');
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse({
                error: {type: 'assistant_unavailable', message: 'The assistant is not available right now.', request_id: 'rid'},
            }, 503),
        );
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'will fail'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        const alert = await screen.findByRole('alert');
        expect(alert).toHaveAttribute('data-error-type', 'assistant_unavailable');
        expect(screen.getByTestId('retry-button')).toBeInTheDocument();
    });
});

// ---------------------------------------------------------------------------
// Issue #476: compact availability notice for empty assistant replies
// ---------------------------------------------------------------------------

describe('JobSearchChat availability notice (#476)', () => {
    beforeEach(() => {
        global.fetch = statusAwareFetch();
        window.innerHeight = 768;
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    const noMatchesStatus = {
        state: 'no_matches',
        title: 'No matches for your current requirements',
        message: 'Jobs are available, but none meet your saved requirements yet.',
        actions: ['chat'],
    };

    async function renderAndSend(statusPayloadValue: unknown) {
        const mock = global.fetch as jest.Mock;
        mock.mockImplementation((url: string) => {
            if (url.includes('/api/agent/conversations') && (mock as any).mock.calls.length === 1) {
                return Promise.resolve(jsonResponse(emptyConversation(42)));
            }
            if (url.includes('/api/job-matches/status/')) {
                return Promise.resolve(jsonResponse(statusPayloadValue));
            }
            // POST message -> assistant reply with no result cards.
            return Promise.resolve(jsonResponse(
                {message: assistantMessage(2, 'Nothing came back.'), preferences_changed: false},
                201,
            ));
        });
        render(<JobSearchChat/>);
        const input = await screen.findByLabelText('Message');
        await waitFor(() => expect(input).toBeEnabled());
        fireEvent.change(input, {target: {value: 'any jobs?'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await screen.findByText('Nothing came back.');
    }

    test('empty assistant reply shows an availability notice instead of silence', async () => {
        await renderAndSend(noMatchesStatus);
        const notice = await screen.findByTestId('availability-notice');
        expect(notice).toBeInTheDocument();
        expect(notice).toHaveAttribute('role', 'status');
        expect(notice).toHaveAttribute('aria-live', 'polite');
        expect(notice).toHaveTextContent('No matches for your current requirements');
        expect(notice).toHaveTextContent('Jobs are available, but none meet your saved requirements yet.');
        expect(screen.queryByTestId('result-cards')).not.toBeInTheDocument();
    });

    test('no availability notice when the state is ok', async () => {
        await renderAndSend({state: 'ok', title: 'Matches ready', message: 'You have matches.'});
        await waitFor(() => {
            expect(screen.queryByTestId('availability-notice')).not.toBeInTheDocument();
        });
    });

    test('status fetch failure never breaks the chat or renders a notice', async () => {
        const mock = global.fetch as jest.Mock;
        mock.mockImplementation((url: string) => {
            if (url.includes('/api/job-matches/status/')) {
                return Promise.reject(new Error('status down'));
            }
            if (mock.mock.calls.length === 1) {
                return Promise.resolve(jsonResponse(emptyConversation(42)));
            }
            return Promise.resolve(jsonResponse(
                {message: assistantMessage(2, 'Nothing came back.'), preferences_changed: false},
                201,
            ));
        });
        render(<JobSearchChat/>);
        const input = await screen.findByLabelText('Message');
        await waitFor(() => expect(input).toBeEnabled());
        fireEvent.change(input, {target: {value: 'any jobs?'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await screen.findByText('Nothing came back.');
        expect(screen.queryByTestId('availability-notice')).not.toBeInTheDocument();
    });

    test('assistant reply with results never shows the availability notice', async () => {
        const mock = global.fetch as jest.Mock;
        mock.mockImplementation((url: string) => {
            if (mock.mock.calls.length === 1) {
                return Promise.resolve(jsonResponse(emptyConversation(42)));
            }
            if (url.includes('/api/job-matches/status/')) {
                return Promise.resolve(jsonResponse(noMatchesStatus));
            }
            return Promise.resolve(jsonResponse({
                message: assistantMessage(2, 'Here is a match.', false, {
                    jobs: [{
                        id: 7, title: 'Senior Engineer', organization_name: 'Acme',
                        location: 'SF', remote: true, compensation: null,
                        canonical_url: 'https://jobs.example.test/7',
                        observed_at: null, updated_at: null,
                    }],
                    organizations: [],
                }),
                preferences_changed: false,
            }, 201));
        });
        render(<JobSearchChat/>);
        const input = await screen.findByLabelText('Message');
        await waitFor(() => expect(input).toBeEnabled());
        fireEvent.change(input, {target: {value: 'find jobs'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await screen.findByText('Here is a match.');
        expect(await screen.findByTestId('result-cards')).toBeInTheDocument();
        expect(screen.queryByTestId('availability-notice')).not.toBeInTheDocument();
    });
});

describe('durable turn state (issue #458)', () => {
    const KEY_A = '123e4567-e89b-42d3-a456-426614174000';
    const KEY_B = '987e6543-e21b-12d3-b456-426614174999';

    beforeEach(() => {
        global.fetch = statusAwareFetch();
        window.localStorage.clear();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        window.localStorage.clear();
    });

    describe('failed-turn rendering from server state', () => {
        test('a server-loaded failed user turn renders the saved-question notice with actions', async () => {
            await renderChat([userTurn('saved question', KEY_A, 'failed')]);
            expect(screen.getByTestId('failed-turn')).toBeInTheDocument();
            expect(screen.getByTestId('failed-turn')).toHaveTextContent(/response failed/i);
            expect(screen.getByTestId('failed-turn')).toHaveTextContent(/your message is saved/i);
            expect(screen.getByTestId('failed-turn')).toHaveClass('chat-failure-panel');
            expect(screen.getByRole('button', {name: 'Retry response'})).toBeInTheDocument();
            expect(screen.getByRole('button', {name: 'Edit as new message'})).toBeInTheDocument();
            expect(screen.getByRole('group', {name: 'Failed turn actions'})).toBeInTheDocument();
        });

        test('completed and pending turns do not render the failed-turn affordances', async () => {
            await renderChat([userTurn('done', KEY_A, 'completed'), userTurn('running', KEY_B, 'pending', 2)]);
            expect(screen.queryByTestId('failed-turn')).not.toBeInTheDocument();
            expect(screen.queryByRole('button', {name: 'Retry response'})).not.toBeInTheDocument();
        });

        test('retry-in-progress replaces the failure treatment with amber state and disabled controls', async () => {
            await renderChat([userTurn('saved question', KEY_A, 'failed')]);
            let resolvePost: ((r: Response) => void) | undefined;
            (global.fetch as jest.Mock).mockImplementationOnce(
                () => new Promise<Response>((resolve) => { resolvePost = resolve; }),
            );
            fireEvent.click(screen.getByTestId('retry-response-button'));
            // The red failure panel is REPLACED (not retained) by the amber retry panel.
            await screen.findByTestId('retrying-turn');
            expect(screen.queryByTestId('failed-turn')).not.toBeInTheDocument();
            expect(screen.getByTestId('retrying-turn')).toHaveClass('chat-retry-panel');
            expect(screen.getByTestId('retrying-turn')).toHaveTextContent(/retrying response…/i);
            // Retry control: stable muted disabled treatment with the primary label.
            const retryBtn = screen.getByTestId('retry-response-button');
            expect(retryBtn).toBeDisabled();
            expect(retryBtn).toHaveTextContent('Retrying…');
            expect(screen.getByTestId('edit-as-new-button')).toBeDisabled();
            // Stop stays clearly available while the retry is in flight.
            const stop = screen.getByTestId('stop-button');
            expect(stop).toBeEnabled();
            expect(stop).toHaveTextContent('Stop');
            expect(stop).toHaveClass('chat-stop');

            resolvePost!(jsonResponse({message: assistantMessage(12, 'recovered'), preferences_changed: false}, 201));
            await screen.findByText('recovered');
            expect(screen.queryByTestId('retrying-turn')).not.toBeInTheDocument();
        });

        test('alert retry control disappears while a retry is in flight; bubble shows the amber state', async () => {
            await renderChat();
            (global.fetch as jest.Mock)
                .mockResolvedValueOnce(
                    jsonResponse({error: {type: 'service_error', message: 'down'}}, 500),
                )
                .mockImplementationOnce(
                    () => new Promise<Response>(() => {}),
                );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'again'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const retryBtn = await screen.findByTestId('retry-button');
            expect(retryBtn).toBeEnabled();
            fireEvent.click(retryBtn);
            // The typed error alert yields to the in-progress treatment on the
            // bubble: amber panel, disabled "Retrying…" control, Stop available.
            await screen.findByTestId('retrying-turn');
            expect(screen.queryByTestId('retry-button')).not.toBeInTheDocument();
            const bubbleRetry = screen.getByTestId('retry-response-button');
            expect(bubbleRetry).toBeDisabled();
            expect(bubbleRetry).toHaveTextContent('Retrying…');
            expect(screen.getByTestId('stop-button')).toBeEnabled();
        });

        test('retrying a server-loaded failed turn resends the same key without duplicating history', async () => {
            await renderChat([userTurn('saved question', KEY_A, 'failed')]);
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(9, 'recovered!'), preferences_changed: false}, 201),
            );
            fireEvent.click(screen.getByTestId('retry-response-button'));
            await screen.findByText('recovered!');
            const bodies = postBodies(global.fetch as jest.Mock, messageUrl());
            expect(bodies).toHaveLength(1);
            expect(bodies[0].idempotency_key).toBe(KEY_A);
            // One user bubble, no duplicate, and the failed affordances are gone.
            expect(screen.getAllByText('saved question')).toHaveLength(1);
            expect(screen.queryByTestId('failed-turn')).not.toBeInTheDocument();
        });

        test('retrying does nothing for a failed turn without an idempotency key', async () => {
            await renderChat([{...userTurn('keyless', '', 'failed')}]);
            fireEvent.click(screen.getByTestId('retry-response-button'));
            await waitFor(() => {
                expect(postBodies(global.fetch as jest.Mock, messageUrl())).toHaveLength(0);
            });
        });

        test('edit as new message loads the composer and sending starts a new key', async () => {
            await renderChat([userTurn('needs fixing', KEY_A, 'failed')]);
            fireEvent.click(screen.getByTestId('edit-as-new-button'));
            expect(screen.getByLabelText('Message')).toHaveValue('needs fixing');
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(10, 'fresh answer'), preferences_changed: false}, 201),
            );
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText('fresh answer');
            const bodies = postBodies(global.fetch as jest.Mock, messageUrl());
            expect(bodies[0].content).toBe('needs fixing');
            expect(bodies[0].idempotency_key).not.toBe(KEY_A);
        });

        test('double-clicking retry applies at most one outcome (client pending guard)', async () => {
            await renderChat([userTurn('once only', KEY_A, 'failed')]);
            let resolvePost: ((r: Response) => void) | undefined;
            (global.fetch as jest.Mock).mockImplementationOnce(
                () => new Promise<Response>((resolve) => { resolvePost = resolve; }),
            );
            fireEvent.click(screen.getByTestId('retry-response-button'));
            fireEvent.click(screen.getByTestId('retry-response-button'));
            await waitFor(() => {
                expect(postBodies(global.fetch as jest.Mock, messageUrl())).toHaveLength(1);
            });
            resolvePost!(jsonResponse({message: assistantMessage(11, 'single'), preferences_changed: false}, 201));
            await screen.findByText('single');
            expect(postBodies(global.fetch as jest.Mock, messageUrl())).toHaveLength(1);
        });
    });

    describe('durable in-flight markers', () => {
        test('writes the per-turn marker before the POST resolves and clears it on success', async () => {
            await renderChat();
            let resolvePost: ((r: Response) => void) | undefined;
            (global.fetch as jest.Mock).mockImplementationOnce(
                () => new Promise<Response>((resolve) => { resolvePost = resolve; }),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'held turn'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const key = await postedKeyAfterSend(global.fetch as jest.Mock);
            const markerKey = inflightKeyFor(42, key);
            await waitFor(() => expect(window.localStorage.getItem(markerKey)).not.toBeNull());
            const marker = JSON.parse(window.localStorage.getItem(markerKey) || '{}');
            expect(marker).toEqual({conversationId: 42, content: 'held turn', key, ts: expect.any(Number)});

            resolvePost!(jsonResponse({message: assistantMessage(5, 'arrived'), preferences_changed: false}, 201));
            await screen.findByText('arrived');
            expect(window.localStorage.getItem(markerKey)).toBeNull();
            // The composer draft is cleared once the turn is sent.
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBeNull();
        });

        test('adopts server state on load when the server knows the in-flight key (late reply included)', async () => {
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'in flight', key: KEY_A, ts: Date.now()}),
            );
            await renderChat([userTurn('in flight', KEY_A, 'completed'), assistantMessage(2, 'late reply')]);
            expect(screen.getByText('late reply')).toBeInTheDocument();
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_A))).toBeNull();
            // No phantom draft: the server received the turn.
            expect(screen.getByLabelText('Message')).toHaveValue('');
        });

        test('restores the content as an unsent draft when the server never received the key', async () => {
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'never arrived', key: KEY_A, ts: Date.now()}),
            );
            await renderChat([]);
            expect(screen.getByLabelText('Message')).toHaveValue('never arrived');
            // The marker stays durable until an explicit send/discard (issue
            // #458 r2): a scan never silently deletes unsent content.
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_A))).not.toBeNull();
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('never arrived');
        });

        test('ignores markers belonging to a different conversation', async () => {
            window.localStorage.setItem(
                inflightKeyFor(7, KEY_A),
                JSON.stringify({conversationId: 7, content: 'other convo', key: KEY_A, ts: Date.now()}),
            );
            await renderChat([]);
            expect(screen.getByLabelText('Message')).toHaveValue('');
            // The marker belongs to conversation 7 and stays for its resume.
            expect(window.localStorage.getItem(inflightKeyFor(7, KEY_A))).not.toBeNull();
        });

        test('two concurrent turns keep separate markers; resolving one never clears the other', async () => {
            // Adversarial review (issue #458): a single global slot lost one of
            // two concurrent turns' recovery state. Markers are per turn now.
            await renderChat([]);
            let resolvePost: ((r: Response) => void) | undefined;
            (global.fetch as jest.Mock).mockImplementationOnce(
                () => new Promise<Response>((resolve) => { resolvePost = resolve; }),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'turn a'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const keyA = await postedKeyAfterSend(global.fetch as jest.Mock);
            await waitFor(() => expect(window.localStorage.getItem(inflightKeyFor(42, keyA))).not.toBeNull());
            // While turn A is in flight, another tab starts turn B in the same
            // conversation: its marker lands next to A's.
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_B),
                JSON.stringify({conversationId: 42, content: 'turn b', key: KEY_B, ts: Date.now()}),
            );
            // Both markers coexist.
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_B))).not.toBeNull();

            resolvePost!(jsonResponse({message: assistantMessage(6, 'a done'), preferences_changed: false}, 201));
            await screen.findByText('a done');
            // Resolving turn A clears only its own marker; turn B survives.
            expect(window.localStorage.getItem(inflightKeyFor(42, keyA))).toBeNull();
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_B))).not.toBeNull();
        });

        test('prunes corrupt and aged markers on load without touching valid ones', async () => {
            const otherConvKey = inflightKeyFor(7, KEY_B);
            window.localStorage.setItem(
                inflightKeyFor(42, 'corrupt-not-json'),
                '{not json',
            );
            window.localStorage.setItem(
                inflightKeyFor(42, 'no-conversation-id'),
                JSON.stringify({content: 'shape mismatch'}),
            );
            const aged = Date.now() - (25 * 60 * 60 * 1000);
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'stale', key: KEY_A, ts: aged}),
            );
            window.localStorage.setItem(
                otherConvKey,
                JSON.stringify({conversationId: 7, content: 'fine', key: KEY_B, ts: Date.now()}),
            );
            await renderChat([]);
            // Corrupt, shape-mismatched, and day-old markers are pruned.
            expect(window.localStorage.getItem(inflightKeyFor(42, 'corrupt-not-json'))).toBeNull();
            expect(window.localStorage.getItem(inflightKeyFor(42, 'no-conversation-id'))).toBeNull();
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_A))).toBeNull();
            // A valid other-conversation marker survives untouched.
            expect(window.localStorage.getItem(otherConvKey)).not.toBeNull();
            // Nothing restorable was restored as a draft.
            expect(screen.getByLabelText('Message')).toHaveValue('');
        });

        test('a marker after a failed (typed) send still reconciles against the server on reload', async () => {
            const first = await renderChatAs([]);
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({error: {type: 'assistant_unavailable', message: 'down'}}, 503),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'retry me'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByRole('alert');
            // The turn shows failed in place and the marker survives for reload
            // reconciliation (the server has the user message).
            expect(screen.getByTestId('failed-turn')).toBeInTheDocument();
            const key = lastPostedKey(global.fetch as jest.Mock);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
            first.unmount();

            // Reload: server has the failed turn; adopt it and drop the marker.
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [userTurn('retry me', key, 'failed')])),
            );
            const instance = await renderChatAs([userTurn('retry me', key, 'failed')]);
            expect(screen.getByTestId('failed-turn')).toBeInTheDocument();
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).toBeNull();
            instance.unmount();
        });
    });

    describe('multi-marker reconciliation (issue #458 r2)', () => {
        test('two unsent markers are both preserved; only the newest surfaces', async () => {
            // Round-2 review data-loss edge: eager clearing kept only the newest
            // unsent marker and silently deleted the older one. Both must survive
            // the scan — never silently delete unsent content.
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'older unsent', key: KEY_A, ts: Date.now() - 5000}),
            );
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_B),
                JSON.stringify({conversationId: 42, content: 'newer unsent', key: KEY_B, ts: Date.now()}),
            );
            await renderChat([]);
            // The newest unsent turn surfaces as the composer draft...
            expect(screen.getByLabelText('Message')).toHaveValue('newer unsent');
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('newer unsent');
            // ...while BOTH markers stay durable in storage.
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_A))).not.toBeNull();
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_B))).not.toBeNull();
        });

        test('one confirmed + one unsent marker: only the unsent one is kept', async () => {
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'confirmed unsent', key: KEY_A, ts: Date.now() - 5000}),
            );
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_B),
                JSON.stringify({conversationId: 42, content: 'still unsent', key: KEY_B, ts: Date.now()}),
            );
            await renderChat([userTurn('confirmed unsent', KEY_A, 'completed')]);
            // The server-confirmed marker is resolved...
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_A))).toBeNull();
            // ...the unsent one stays recoverable and surfaces as the draft.
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_B))).not.toBeNull();
            expect(screen.getByLabelText('Message')).toHaveValue('still unsent');
        });

        test('explicit send resolves only the surfaced marker; the older unsent one survives', async () => {
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'older unsent', key: KEY_A, ts: Date.now() - 5000}),
            );
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_B),
                JSON.stringify({conversationId: 42, content: 'newer unsent', key: KEY_B, ts: Date.now()}),
            );
            await renderChat([]);
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(5, 'sent it'), preferences_changed: false}, 201),
            );
            // Sending the surfaced draft is the explicit resolution of that
            // unsent turn — and only that one.
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText('sent it');
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_B))).toBeNull();
            // The older unsent turn stays recoverable for its conversation view.
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_A))).not.toBeNull();
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBeNull();
        });

        test('explicit discard (emptying the composer) resolves only the surfaced marker', async () => {
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'older unsent', key: KEY_A, ts: Date.now() - 5000}),
            );
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_B),
                JSON.stringify({conversationId: 42, content: 'newer unsent', key: KEY_B, ts: Date.now()}),
            );
            await renderChat([]);
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: ''}});
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_B))).toBeNull();
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_A))).not.toBeNull();
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBeNull();
        });

        test('the older unsent marker surfaces on the next load after the newer one was sent', async () => {
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'older unsent', key: KEY_A, ts: Date.now() - 5000}),
            );
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_B),
                JSON.stringify({conversationId: 42, content: 'newer unsent', key: KEY_B, ts: Date.now()}),
            );
            const first = await renderChatAs([]);
            expect(screen.getByLabelText('Message')).toHaveValue('newer unsent');
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(5, 'sent it'), preferences_changed: false}, 201),
            );
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText('sent it');
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_B))).toBeNull();
            first.unmount();

            // Next load: the server never received the older turn either, so it
            // is restored on its conversation view (one at a time, newest first).
            const second = await renderChatAs([]);
            expect(screen.getByLabelText('Message')).toHaveValue('older unsent');
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_A))).not.toBeNull();
            second.unmount();
        });

        test('a composer draft edited after the failed send wins over the marker; the marker stays recoverable', async () => {
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_B),
                JSON.stringify({conversationId: 42, content: 'never arrived', key: KEY_B, ts: Date.now()}),
            );
            const first = await renderChatAs([]);
            expect(screen.getByLabelText('Message')).toHaveValue('never arrived');
            // The user edits the restored text after the failure.
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'edited after the failure'}});
            first.unmount();

            // Reload: the newer draft wins — surfacing never clobbers the user's
            // latest typing — and the marker is still recoverable in storage.
            const second = await renderChatAs([]);
            expect(screen.getByLabelText('Message')).toHaveValue('edited after the failure');
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_B))).not.toBeNull();
            second.unmount();
        });
    });

    describe('composer draft persistence', () => {
        test('typing persists a per-conversation draft; loading restores it without clobbering', async () => {
            const first = await renderChatAs([]);
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'unfinished thought'}});
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('unfinished thought');
            first.unmount();

            // The next load restores the persisted draft (reload survival).
            const second = await renderChatAs([]);
            const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
            expect(box).toHaveValue('unfinished thought');
            // Clearing the composer removes the draft.
            fireEvent.change(box, {target: {value: ''}});
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBeNull();
            second.unmount();

            // A new load restores the draft only when present.
            window.localStorage.setItem('crank:jobsearch:draft:42', 'restored draft');
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'never arrived', key: KEY_A, ts: Date.now()}),
            );
            const third = await renderChatAs([]);
            // The unreceived in-flight content wins over the stale draft.
            expect(screen.getByLabelText('Message')).toHaveValue('never arrived');
            third.unmount();
        });

        test('a conversation restored via auto-create (createOnMount) also reconciles its marker', async () => {
            window.localStorage.setItem(
                inflightKeyFor(7, KEY_A),
                JSON.stringify({conversationId: 7, content: 'ghost turn', key: KEY_A, ts: Date.now()}),
            );
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(jsonResponse({}, 404));
            // Auto-created conversation happens to be id 7 with no trace of the key.
            mock.mockResolvedValueOnce(jsonResponse(emptyConversation(7), 201));
            render(<JobSearchChat createOnMount={true}/>);
            await screen.findByLabelText('Message');
            await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
            expect(screen.getByLabelText('Message')).toHaveValue('ghost turn');
            // The unsent turn stays recoverable in storage (issue #458 r2);
            // only an explicit send/discard resolves it.
            expect(window.localStorage.getItem(inflightKeyFor(7, KEY_A))).not.toBeNull();
        });
    });

    describe('turn_in_progress (409) reconciliation', () => {
        test('surfaces the still-working status and adopts the server state', async () => {
            await renderChat();
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(
                jsonResponse({error: {type: 'turn_in_progress', message: 'Still generating.'}}, 409),
            );
            // The follow-up reconcile GET returns the turn completed — the
            // server holds the POSTED turn (turn_in_progress means exactly
            // that), so the mocked history echoes the posted key.
            mock.mockImplementationOnce(async () => jsonResponse(
                emptyConversation(42, [
                    userTurn('concurrent', lastPostedKey(mock), 'completed'),
                    assistantMessage(3, 'finished elsewhere'),
                ]),
            ));
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'concurrent'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const alert = await screen.findByRole('alert');
            expect(alert).toHaveAttribute('data-error-type', 'turn_in_progress');
            expect(alert).toHaveTextContent('Still generating.');
            // Reconciled: the late reply shows and the marker is cleared.
            await screen.findByText('finished elsewhere');
            const key = lastPostedKey(mock);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).toBeNull();
            expect(screen.getByLabelText('Message')).toHaveValue('');
        });

        test('a 409 against a deleted conversation drops the marker instead of resurrecting it', async () => {
            await renderChat();
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(
                jsonResponse({error: {type: 'turn_in_progress', message: 'Still generating.'}}, 409),
            );
            mock.mockResolvedValueOnce(jsonResponse({detail: 'gone'}, 404));
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'doomed'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByRole('alert');
            const key = lastPostedKey(mock);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).toBeNull();
            // History unchanged (no reconcile payload adopted).
            expect(screen.queryByText('finished elsewhere')).not.toBeInTheDocument();
        });

        test('a failing reconcile GET is non-fatal and keeps the marker for the next load', async () => {
            await renderChat();
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(
                jsonResponse({error: {type: 'turn_in_progress', message: 'Still generating.'}}, 409),
            );
            mock.mockRejectedValueOnce(new Error('network down'));
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'flaky'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByRole('alert');
            const key = lastPostedKey(mock);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
        });
    });

    describe('stop waiting (honest cancel semantics)', () => {
        test('stop aborts the wait, asks the server, and keeps the marker when the check fails', async () => {
            await renderChat();
            const mock = global.fetch as jest.Mock;
            mock.mockImplementationOnce(
                (_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
                    init.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
                }),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'slow turn'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const stop = await screen.findByTestId('stop-button');
            expect(screen.getByLabelText('Message history')).toHaveAttribute('aria-busy', 'true');
            fireEvent.click(stop);
            await screen.findByText(/stopped waiting/i);
            expect(screen.getByLabelText('Message history')).toHaveAttribute('aria-busy', 'false');
            expect(screen.getByTestId('retry-button')).toBeInTheDocument();
            // The reconcile check also failed (no queued response), so the
            // marker stands for load-time reconciliation.
            const key = lastPostedKey(mock);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
        });

        test('stop with a pending server turn shows the honest not-arrived panel with a check action', async () => {
            await renderChat();
            const mock = global.fetch as jest.Mock;
            mock.mockImplementationOnce(
                (_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
                    init.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
                }),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'slow turn'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            // Capture the POST's key (issued after the advisory status check),
            // then queue the reconcile GET showing the turn persisted and
            // still pending.
            const key = await postedKeyAfterSend(mock);
            mock.mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [userTurn('slow turn', key, 'pending')])),
            );
            fireEvent.click(await screen.findByTestId('stop-button'));
            await screen.findByText(/stopped waiting/i);
            // The server owns the turn: the honest pending panel replaces
            // the optimistic bubble, and a follow-up check adopts the reply.
            await screen.findByTestId('pending-turn');
            mock.mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [
                    userTurn('slow turn', key, 'completed'),
                    assistantMessage(9, 'arrived after stop'),
                ])),
            );
            fireEvent.click(screen.getByTestId('check-response-button'));
            await screen.findByText('arrived after stop');
        });

        test('a network-level failure is uncertain: the turn stays with a check action and the marker', async () => {
            await renderChat();
            const mock = global.fetch as jest.Mock;
            mock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'unstable'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const alert = await screen.findByText(/failed to fetch/i);
            expect(alert).toBeInTheDocument();
            // Whether the server received the request is unknown: the turn
            // stays visible with the honest "not arrived yet" panel — never
            // a false "your message is saved" claim — and the marker stands.
            expect(screen.getByText('unstable')).toBeInTheDocument();
            expect(screen.getByTestId('pending-turn')).toBeInTheDocument();
            const key = lastPostedKey(mock);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
        });

        test('stop before the server received the turn restores it as a draft', async () => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            mock.mockImplementationOnce(
                (_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
                    init.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
                }),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'never sent'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const key = await postedKeyAfterSend(mock);
            // The reconcile GET succeeds and shows no trace of the key.
            mock.mockResolvedValueOnce(jsonResponse(emptyConversation(42, [])));
            fireEvent.click(await screen.findByTestId('stop-button'));
            // Honest outcome: stopped before delivery, kept as a draft. The
            // marker stays durable (r3): only an explicit send/discard or a
            // present confirmation clears it now.
            await screen.findByText(/stopped before the message was sent/i);
            expect(screen.getByLabelText('Message')).toHaveValue('never sent');
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('never sent');
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
            // Explicitly sending the surfaced draft resolves the marker.
            mock.mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(2, 'delivered now'), preferences_changed: false}, 201),
            );
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText('delivered now');
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).toBeNull();
        });

        test('stop against a conversation deleted elsewhere reports it honestly', async () => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            mock.mockImplementationOnce(
                (_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
                    init.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
                }),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'doomed'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const key = await postedKeyAfterSend(mock);
            mock.mockResolvedValueOnce(jsonResponse({detail: 'gone'}, 404));
            fireEvent.click(await screen.findByTestId('stop-button'));
            await screen.findByText(/this conversation is no longer available/i);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).toBeNull();
        });

        test('no stop button while idle', async () => {
            await renderChat();
            expect(screen.queryByTestId('stop-button')).not.toBeInTheDocument();
        });
    });

    describe('reset / delete clear durable state', () => {
        test('reset clears the in-flight markers and the conversation draft', async () => {
            await renderChat([userTurn('before reset', KEY_A, 'completed')]);
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'x', key: KEY_A, ts: Date.now()}),
            );
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_B),
                JSON.stringify({conversationId: 42, content: 'y', key: KEY_B, ts: Date.now()}),
            );
            window.localStorage.setItem(
                inflightKeyFor(7, '11111111-1111-4111-8111-111111111111'),
                JSON.stringify({
                    conversationId: 7,
                    content: 'other conversation',
                    key: '11111111-1111-4111-8111-111111111111',
                    ts: Date.now(),
                }),
            );
            window.localStorage.setItem('crank:jobsearch:draft:42', 'pending text');
            window.confirm = jest.fn().mockReturnValue(true);
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(43), 201));
            fireEvent.click(screen.getByRole('button', {name: 'Reset chat'}));
            await waitFor(() => expect(screen.getByTestId('empty-history')).toBeInTheDocument());
            // Every marker of THIS conversation is cleared...
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_A))).toBeNull();
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_B))).toBeNull();
            // ...but another conversation's marker survives (issue #458
            // adversarial review: per-turn markers cannot clobber each other).
            expect(
                window.localStorage.getItem(inflightKeyFor(7, '11111111-1111-4111-8111-111111111111'))
            ).not.toBeNull();
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBeNull();
        });

        test('delete clears the in-flight markers and the conversation draft', async () => {
            await renderChat([userTurn('before delete', KEY_A, 'completed')]);
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_A),
                JSON.stringify({conversationId: 42, content: 'x', key: KEY_A, ts: Date.now()}),
            );
            window.localStorage.setItem('crank:jobsearch:draft:42', 'pending text');
            window.confirm = jest.fn().mockReturnValue(true);
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({deleted: true}));
            fireEvent.click(screen.getByRole('button', {name: 'Delete conversation'}));
            await waitFor(() => expect(screen.getByTestId('empty-history')).toBeInTheDocument());
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_A))).toBeNull();
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBeNull();
        });
    });

    describe('pre-persistence failures keep a durable unsent draft (issue #458 r4)', () => {
        test.each([
            ['rate_limited', 429, 'Too many messages. Try again shortly.'],
            ['invalid_message', 400, 'Message content is required.'],
            ['not_found', 404, 'Conversation not found or not owned by this user.'],
        ])('%s keeps the per-turn marker until explicit resolution', async (type, status, message) => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(
                jsonResponse({error: {type, message}}, status),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'never persisted'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            // The typed alert speaks the server's message...
            const alert = await screen.findByRole('alert');
            expect(alert).toHaveTextContent(message);
            // ...but the question is NOT presented as a saved failed turn,
            // and no "your message is saved" copy appears anywhere.
            expect(screen.queryByTestId('failed-turn')).not.toBeInTheDocument();
            expect(screen.queryByText(/your message is saved/i)).not.toBeInTheDocument();
            // The content surfaces as an unsent draft.
            expect(screen.getByLabelText('Message')).toHaveValue('never persisted');
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('never persisted');
            // The per-turn marker stays durable: a definitive confirmed-absent
            // response never deletes unsent content (issue #458 r4). Only a
            // server-present confirmation, an explicit send/discard, or a gone
            // conversation clears it.
            const key = lastPostedKey(mock);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
        });

        test('the surfaced pre-persistence draft resolves only on explicit send or discard', async () => {
            const type = 'rate_limited';
            const status = 429;
            const message = 'Too many messages. Try again shortly.';
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(
                jsonResponse({error: {type, message}}, status),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'kept turn'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByRole('alert');
            const key = lastPostedKey(mock);
            // Explicit discard (emptying the composer) is the only way the
            // surfaced marker goes away — a scan or a later reconciliation
            // never deletes it silently.
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: ''}});
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).toBeNull();
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBeNull();

            // And an explicit send of a surfaced draft resolves it too.
            mock.mockResolvedValueOnce(
                jsonResponse({error: {type, message}}, status),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'kept again'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findAllByRole('alert');
            const key2 = lastPostedKey(mock);
            expect(key2).not.toBe(key);
            expect(window.localStorage.getItem(inflightKeyFor(42, key2))).not.toBeNull();
            mock.mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(5, 'sent it'), preferences_changed: false}, 201),
            );
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText('sent it');
            expect(window.localStorage.getItem(inflightKeyFor(42, key2))).toBeNull();
        });

        test('two concurrent definitive-absent responses keep both markers; the resolving tab surfaces newest-wins', async () => {
            // Round-4 review data-loss edge: two tabs each receiving a
            // definitive pre-persistence response must not collapse both
            // unsent turns into the single shared draft slot. Each tab keeps
            // its own per-turn marker; nothing is deleted on confirmation.
            await renderChat([]);
            let resolvePost: ((r: Response) => void) | undefined;
            (global.fetch as jest.Mock).mockImplementationOnce(
                () => new Promise<Response>((resolve) => { resolvePost = resolve; }),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'turn a'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const keyA = await postedKeyAfterSend(global.fetch as jest.Mock);
            await waitFor(() => expect(window.localStorage.getItem(inflightKeyFor(42, keyA))).not.toBeNull());
            // While turn A is in flight, another tab starts turn B: its
            // marker lands next to A's.
            window.localStorage.setItem(
                inflightKeyFor(42, KEY_B),
                JSON.stringify({conversationId: 42, content: 'turn b', key: KEY_B, ts: Date.now()}),
            );

            // This tab's request comes back with a definitive pre-persistence
            // failure: the turn was never stored, but nothing may be deleted.
            resolvePost!(jsonResponse(
                {error: {type: 'rate_limited', message: 'Too many messages. Try again shortly.'}}, 429,
            ));
            const alert = await screen.findByRole('alert');
            expect(alert).toHaveTextContent(/too many messages/i);
            // Both markers coexist: the confirmed-absent turn stays durable
            // and the other tab's in-flight marker is untouched.
            expect(window.localStorage.getItem(inflightKeyFor(42, keyA))).not.toBeNull();
            expect(window.localStorage.getItem(inflightKeyFor(42, KEY_B))).not.toBeNull();
            // The confirmed-absent turn surfaces as the draft, newest-wins.
            expect(screen.getByLabelText('Message')).toHaveValue('turn a');
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('turn a');
        });
    });

    describe('retry cap (adversarial review: bounded, honest retries)', () => {
        test('a server-loaded exhausted turn disables Retry and points to Edit', async () => {
            await renderChat([userTurn('gave up retrying', KEY_A, 'failed', 1, false)]);
            expect(screen.getByTestId('failed-turn')).toHaveTextContent(/after several retries/i);
            const retryBtn = screen.getByTestId('retry-response-button');
            expect(retryBtn).toBeDisabled();
            expect(retryBtn).toHaveTextContent(/retry limit reached/i);
            expect(screen.getByTestId('edit-as-new-button')).toBeEnabled();
        });

        test('clicking the exhausted turn retry control sends nothing', async () => {
            await renderChat([userTurn('gave up retrying', KEY_A, 'failed', 1, false)]);
            fireEvent.click(screen.getByTestId('retry-response-button'));
            await waitFor(() => {
                expect(postBodies(global.fetch as jest.Mock, messageUrl())).toHaveLength(0);
            });
        });

        test('a retry_limit_reached response reconciles to the exhausted state and documents the cap', async () => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(
                jsonResponse({error: {type: 'assistant_unavailable', message: 'down'}}, 503),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'poisoned'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByTestId('failed-turn');
            const key = lastPostedKey(mock);
            // Retry hits the server-side cap.
            mock.mockResolvedValueOnce(
                jsonResponse({
                    error: {
                        type: 'retry_limit_reached',
                        message: "This response couldn't be delivered after 5 attempts. Please edit the message and send it again as a new message.",
                    },
                }, 429),
            );
            // The reconcile GET returns the turn failed and exhausted.
            mock.mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [userTurn('poisoned', key, 'failed', 1, false)])),
            );
            fireEvent.click(screen.getByTestId('retry-response-button'));
            const retryBtn = await screen.findByRole('button', {name: 'Retry limit reached'});
            expect(retryBtn).toBeDisabled();
            expect(screen.getByTestId('failed-turn')).toHaveTextContent(/after several retries/i);
            // The cap is documented in the surfaced server copy.
            expect(await screen.findByRole('alert')).toHaveTextContent(/after 5 attempts/i);
        });
    });

    describe('retry ordering (adversarial review: stable transcript)', () => {
        test('a successful retry inserts its reply after the original turn, never at the end', async () => {
            // Server history: turn 1 failed; a newer turn 2 completed after it.
            await renderChat([
                userTurn('first question', KEY_A, 'failed', 1),
                userTurn('second question', KEY_B, 'completed', 2),
                assistantMessage(3, 'second answer'),
            ]);
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(4, 'first answer'), preferences_changed: false}, 201),
            );
            fireEvent.click(screen.getByTestId('retry-response-button'));
            await screen.findByText('first answer');
            // The reply lands immediately after its original question — the
            // order stays user1, assistant1, user2, assistant2 on screen and,
            // because the server pins replies the same way, after reload too.
            const bubbles = Array.from(
                screen.getByLabelText('Message history').querySelectorAll('.chat-bubble'),
            ).map((el) => el.textContent || '');
            expect(bubbles).toEqual([
                expect.stringContaining('first question'),
                expect.stringContaining('first answer'),
                expect.stringContaining('second question'),
                expect.stringContaining('second answer'),
            ]);
        });
    });

    describe('reset / delete guarded mid-flight (adversarial review)', () => {
        test('reset and delete are disabled while a turn is in flight', async () => {
            await renderChat();
            (global.fetch as jest.Mock).mockImplementationOnce(
                () => new Promise<Response>(() => {}),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'in flight'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByTestId('stop-button');
            expect(screen.getByRole('button', {name: 'Reset chat'})).toBeDisabled();
            expect(screen.getByRole('button', {name: 'Delete conversation'})).toBeDisabled();
        });
    });

    describe('uncertain failures reconcile against the server (adversarial review)', () => {
        test('a network failure the server never received is restored as a draft', async () => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            const settlePost = holdNextFetch(mock);
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'maybe lost'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const key = await postedKeyAfterSend(mock);
            // The reconcile GET succeeds and shows no trace of the key.
            mock.mockResolvedValueOnce(jsonResponse(emptyConversation(42, [])));
            settlePost(new TypeError('Failed to fetch'));
            // The turn is restored as a draft; nothing pretends it was sent.
            await screen.findByText(/kept as a draft/i);
            expect(screen.getByLabelText('Message')).toHaveValue('maybe lost');
            expect(screen.queryByTestId('failed-turn')).not.toBeInTheDocument();
            expect(screen.queryByTestId('pending-turn')).not.toBeInTheDocument();
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('maybe lost');
            // Durable contract (r3): the marker stands until the user
            // explicitly sends or discards the surfaced draft.
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
            // Emptying the composer is an explicit discard: the surfaced
            // marker is resolved for good.
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: ''}});
            await waitFor(() => expect(window.localStorage.getItem(inflightKeyFor(42, key))).toBeNull());
        });

        test('a network failure the server did receive adopts the server state', async () => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            const settlePost = holdNextFetch(mock);
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'arrived anyway'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const key = await postedKeyAfterSend(mock);
            // The reconcile GET shows the turn failed server-side.
            mock.mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [userTurn('arrived anyway', key, 'failed')])),
            );
            settlePost(new TypeError('Failed to fetch'));
            // The server state wins: the turn renders as failed (saved), not
            // as a draft.
            await screen.findByTestId('failed-turn');
            expect(screen.getByTestId('failed-turn')).toHaveTextContent(/your message is saved/i);
            expect(screen.getByLabelText('Message')).toHaveValue('');
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).toBeNull();
        });

        test('a non-JSON error reconciles to an unsent draft when the server never received it', async () => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            const settlePost = holdNextFetch(mock);
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'uncertain text'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const key = await postedKeyAfterSend(mock);
            mock.mockResolvedValueOnce(jsonResponse(emptyConversation(42, [])));
            settlePost(new Response('plain text error', {status: 500}));
            await screen.findByText(/kept as a draft/i);
            expect(screen.getByLabelText('Message')).toHaveValue('uncertain text');
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('uncertain text');
            // Durable contract (r3): the marker stands; only an explicit
            // send/discard or a present confirmation clears it.
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
        });

        test('two tabs confirmed absent keep both unsent markers; the newest surfaces', async () => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            const settlePost = holdNextFetch(mock);
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'from this tab'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const key = await postedKeyAfterSend(mock);
            // The other tab sends its own turn while this one is waiting.
            const otherKey = '22222222-2222-4222-8222-222222222222';
            window.localStorage.setItem(
                inflightKeyFor(42, otherKey),
                JSON.stringify({conversationId: 42, content: 'from the other tab', key: otherKey, ts: Date.now()}),
            );
            // This tab's reconcile GET shows the server never received it.
            mock.mockResolvedValueOnce(jsonResponse(emptyConversation(42, [])));
            settlePost(new TypeError('Failed to fetch'));
            await screen.findByText(/kept as a draft/i);
            // Both unsent markers survive: this tab's absent reconciliation
            // no longer deletes its own marker nor destroys the other tab's
            // recovery state through the single shared draft slot.
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
            expect(window.localStorage.getItem(inflightKeyFor(42, otherKey))).not.toBeNull();
            // The newest unsent content is the one surfaced in the composer.
            expect(screen.getByLabelText('Message')).toHaveValue('from this tab');
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('from this tab');
        });

        test('a later check that confirms absence keeps the marker and surfaces the draft', async () => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            // The send and the immediate reconcile both fail to reach the
            // server: the turn stays pending with a Check action.
            mock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
            mock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'check me'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const key = await postedKeyAfterSend(mock);
            await screen.findByTestId('pending-turn');
            // The check now reaches the server and it never received the turn.
            mock.mockResolvedValueOnce(jsonResponse(emptyConversation(42, [])));
            fireEvent.click(screen.getByTestId('check-response-button'));
            await screen.findByText(/may not have been sent/i);
            // The kept marker surfaces in the composer; the optimistic
            // pending bubble stops posing as in-flight.
            expect(screen.queryByTestId('pending-turn')).not.toBeInTheDocument();
            expect(screen.getByLabelText('Message')).toHaveValue('check me');
            expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('check me');
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).not.toBeNull();
        });

        test('a non-JSON error against a conversation deleted elsewhere reports it honestly', async () => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            const settlePost = holdNextFetch(mock);
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'gateway doomed'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const key = await postedKeyAfterSend(mock);
            mock.mockResolvedValueOnce(jsonResponse({detail: 'gone'}, 404));
            settlePost(new Response('plain text error', {status: 502}));
            await screen.findByText(/this conversation is no longer available/i);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).toBeNull();
        });

        test('a network failure against a conversation deleted elsewhere reports it honestly', async () => {
            await renderChat([]);
            const mock = global.fetch as jest.Mock;
            const settlePost = holdNextFetch(mock);
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'doomed'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            const key = await postedKeyAfterSend(mock);
            mock.mockResolvedValueOnce(jsonResponse({detail: 'gone'}, 404));
            settlePost(new TypeError('Failed to fetch'));
            await screen.findByText(/this conversation is no longer available/i);
            expect(window.localStorage.getItem(inflightKeyFor(42, key))).toBeNull();
        });
    });

    describe('localStorage failure tolerance', () => {
        // jsdom's localStorage is not spyable; swap the window property.
        function breakStorage(method: 'getItem' | 'setItem' | 'removeItem') {
            const original = window.localStorage;
            const failing = {
                getItem: original.getItem.bind(original),
                setItem: original.setItem.bind(original),
                removeItem: original.removeItem.bind(original),
                clear: original.clear.bind(original),
                key: original.key.bind(original),
                get length() { return original.length; },
                [method]: () => { throw new Error('storage unavailable'); },
            };
            Object.defineProperty(window, 'localStorage', {value: failing, configurable: true});
            return () => {
                Object.defineProperty(window, 'localStorage', {value: original, configurable: true});
            };
        }

        test('turns still send when localStorage writes fail', async () => {
            const restoreSet = breakStorage('setItem');
            const restoreRemove = breakStorage('removeItem');
            try {
                await renderChatAs([]);
                (global.fetch as jest.Mock).mockResolvedValueOnce(
                    jsonResponse({message: assistantMessage(6, 'works anyway'), preferences_changed: false}, 201),
                );
                fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'no storage'}});
                fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
                await screen.findByText('works anyway');
            } finally {
                // Undo in reverse (LIFO) order: each breakStorage() snapshots
                // whatever window.localStorage currently is, so undoing
                // restoreSet before restoreRemove would re-clobber the real
                // storage with the still-broken intermediate object and leak
                // a permanently broken localStorage into every later test.
                restoreRemove();
                restoreSet();
            }
        });

        test('loading still works when localStorage reads fail', async () => {
            const restoreGet = breakStorage('getItem');
            try {
                (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
                render(<JobSearchChat/>);
                await screen.findByLabelText('Message');
                await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
            } finally {
                restoreGet();
            }
        });
    });
});

describe('signed-out visitor and account-switch purge (issue #465)', () => {
    beforeEach(() => {
        global.fetch = statusAwareFetch();
        window.localStorage.clear();
        window.sessionStorage.clear();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        window.localStorage.clear();
        window.sessionStorage.clear();
    });

    function conversationsCalls(fetchMock: jest.Mock): unknown[][] {
        return fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/agent/conversations/'));
    }

    test('isAuthenticated=false renders the introduction and CTA, gates the composer, and issues no conversation fetch', async () => {
        render(<JobSearchChat
            isAuthenticated={false}
            visitorState="anonymous_first_visit"
            signInUrl="/accounts/login/?next=%2Fchat%2F"
            signedOutMessage="Ask the CRank assistant about compensation."
        />);

        const intro = await screen.findByTestId('signed-out-introduction');
        expect(intro).toHaveTextContent('Ask the CRank assistant about compensation.');
        const cta = screen.getByTestId('chat-sign-in-cta');
        expect(cta).toHaveAttribute('href', '/accounts/login/?next=%2Fchat%2F');
        expect(screen.getByRole('button', {name: 'Send message'})).toBeDisabled();
        // The textarea itself stays usable so a draft can be typed (AC-8);
        // only sending is gated on an account.
        expect(screen.getByLabelText('Message')).toBeEnabled();
        expect(screen.queryByTestId('empty-history')).not.toBeInTheDocument();

        // Give any (wrongly issued) resume fetch a tick to fire, then assert
        // it never did: an anonymous GET must create no conversation row.
        await act(async () => { await Promise.resolve(); });
        expect(conversationsCalls(global.fetch as jest.Mock)).toHaveLength(0);
    });

    test('visitorState=session_expired renders whatever signedOutMessage the server resolved', async () => {
        // The server (crank.views.job_search_page) already resolves
        // visitorState into the one correct message and sends only that one
        // down as signedOutMessage — this component never branches on
        // visitorState itself, so an anonymous-first-visit response can
        // never leak the expiry copy (or vice versa) via an unused prop.
        render(<JobSearchChat
            isAuthenticated={false}
            visitorState="session_expired"
            signInUrl="/accounts/login/?next=%2Fchat%2F"
            signedOutMessage="Your session has expired."
        />);

        const intro = await screen.findByTestId('signed-out-introduction');
        expect(intro).toHaveTextContent('Your session has expired.');
        expect(screen.getByRole('log', {name: 'Message history'})).toBeEmptyDOMElement();
    });

    test('a draft typed while signed out is written to the pending pre-conversation slot', async () => {
        render(<JobSearchChat isAuthenticated={false} visitorState="anonymous_first_visit"
                               signedOutMessage="intro"/>);
        const input = await screen.findByLabelText('Message');

        fireEvent.change(input, {target: {value: 'looking for remote roles'}});

        expect(window.localStorage.getItem('crank:jobsearch:draft:pending')).toBe('looking for remote roles');

        fireEvent.change(input, {target: {value: ''}});
        expect(window.localStorage.getItem('crank:jobsearch:draft:pending')).toBeNull();
    });

    test('a pending draft is adopted into the composer once a conversation exists after sign-in', async () => {
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'restored draft text');
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({detail: 'not found'}, 404));
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(99)));

        render(<JobSearchChat isAuthenticated/>);

        await waitFor(() => expect(screen.getByLabelText('Message')).toHaveValue('restored draft text'));
        expect(window.localStorage.getItem('crank:jobsearch:draft:pending')).toBeNull();
        expect(window.localStorage.getItem('crank:jobsearch:draft:99')).toBe('restored draft text');
    });

    test('crank:auth-hydrated with a username different from crank:last-account purges storage and resets the view', async () => {
        window.localStorage.setItem('crank:last-account', 'alice');
        window.localStorage.setItem('crank:jobsearch:draft:42', 'alice draft');
        await renderChat([userMessage('a secret message from alice')]);
        expect(screen.getByText('a secret message from alice')).toBeInTheDocument();

        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(55)));
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true, username: 'bob'}}));
        });

        await waitFor(() => expect(screen.queryByText('a secret message from alice')).not.toBeInTheDocument());
        expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBeNull();
        expect(window.localStorage.getItem('crank:last-account')).toBe('bob');
        // The view re-resumes for the new account rather than staying blank.
        await waitFor(() => expect(conversationsCalls(global.fetch as jest.Mock).length).toBeGreaterThan(1));
    });

    test('crank:auth-hydrated with the same username purges nothing', async () => {
        window.localStorage.setItem('crank:last-account', 'alice');
        window.localStorage.setItem('crank:jobsearch:draft:42', 'alice draft');
        await renderChat([userMessage('alice can still see this')]);

        act(() => {
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true, username: 'alice'}}));
        });

        expect(screen.getByText('alice can still see this')).toBeInTheDocument();
        expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBe('alice draft');
    });

    test('crank:private-state-purged aborts an in-flight send and clears the view', async () => {
        const abortSpy = jest.spyOn(AbortController.prototype, 'abort');
        await renderChat([]);
        const mock = global.fetch as jest.Mock;
        const settlePost = holdNextFetch(mock);
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'in flight'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await postedKeyAfterSend(mock);

        act(() => {
            document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
        });

        expect(abortSpy).toHaveBeenCalled();
        await waitFor(() => expect(screen.getByLabelText('Message')).toHaveValue(''));
        expect(screen.queryByText('in flight')).not.toBeInTheDocument();
        settlePost(new Response(null, {status: 499}));
    });

    test('the server-rendered accountKey purges the previous account before the resume can adopt its draft', async () => {
        // The regression this covers: conversation resume and
        // adoptPendingDraft() run from mount effects, while the whoami
        // hydration that used to be the only account-switch signal resolves
        // much later. Between the two, alice's pending draft could be
        // adopted into bob's conversation (issue #465 AC-9/10). The trusted
        // server-rendered accountKey is compared synchronously instead.
        window.localStorage.setItem('crank:last-account', 'alice');
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'alice private draft');
        window.localStorage.setItem('crank:jobsearch:draft:7', 'alice conversation draft');
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(77)));

        render(<JobSearchChat isAuthenticated accountKey="bob"/>);

        await screen.findByLabelText('Message');
        await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
        expect(screen.getByLabelText('Message')).toHaveValue('');
        expect(window.localStorage.getItem('crank:jobsearch:draft:pending')).toBeNull();
        expect(window.localStorage.getItem('crank:jobsearch:draft:7')).toBeNull();
        expect(window.localStorage.getItem('crank:last-account')).toBe('bob');

        // The delayed whoami finally lands and agrees: nothing further to
        // purge, and alice's draft has never been on screen.
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true, username: 'bob'}}));
        });
        expect(screen.getByLabelText('Message')).toHaveValue('');
    });

    test('the same accountKey still adopts the pending draft (AC-8 is not over-purged)', async () => {
        window.localStorage.setItem('crank:last-account', 'alice');
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'alice draft');
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({detail: 'not found'}, 404));
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(99)));

        render(<JobSearchChat isAuthenticated accountKey="alice"/>);

        await waitFor(() => expect(screen.getByLabelText('Message')).toHaveValue('alice draft'));
        expect(window.localStorage.getItem('crank:last-account')).toBe('alice');
    });

    test('a first sign-in records the account and keeps the draft composed while signed out', async () => {
        // No prior crank:last-account: the signed-out draft belongs to the
        // person who just signed in, so AC-8 restoration must still happen.
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'draft from before sign-in');
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({detail: 'not found'}, 404));
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(101)));

        render(<JobSearchChat isAuthenticated accountKey="alice"/>);

        await waitFor(() => expect(screen.getByLabelText('Message')).toHaveValue('draft from before sign-in'));
        expect(window.localStorage.getItem('crank:last-account')).toBe('alice');
    });

    test('an empty accountKey (signed-out render) neither purges nor records an account', async () => {
        window.localStorage.setItem('crank:last-account', 'alice');
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'alice draft');

        render(<JobSearchChat isAuthenticated={false} visitorState="session_expired"
                               signedOutMessage="Your session has expired." accountKey=""/>);

        await screen.findByTestId('signed-out-introduction');
        expect(window.localStorage.getItem('crank:last-account')).toBe('alice');
        // AC-8: the same person's draft survives their own session expiry.
        await waitFor(() => expect(screen.getByLabelText('Message')).toHaveValue('alice draft'));
    });

    test('a resume response that arrives after an account switch is discarded, not rendered', async () => {
        window.localStorage.setItem('crank:last-account', 'alice');
        const mock = global.fetch as jest.Mock;
        const settleResume = holdNextFetch(mock);
        const abortSpy = jest.spyOn(AbortController.prototype, 'abort');

        render(<JobSearchChat isAuthenticated accountKey="alice"/>);
        await screen.findByLabelText('Message');

        // The account switches while alice's resume is still in flight; the
        // new account's own resume is queued behind it.
        mock.mockResolvedValueOnce(jsonResponse(emptyConversation(88)));
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true, username: 'bob'}}));
        });
        expect(abortSpy).toHaveBeenCalled();

        // alice's response lands late. The purge epoch moved on, so it must
        // be dropped rather than rendered into bob's view.
        await act(async () => {
            settleResume(jsonResponse(emptyConversation(42, [userMessage('alice private message')])));
            await Promise.resolve();
        });

        await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
        expect(screen.queryByText('alice private message')).not.toBeInTheDocument();
    });

    test('crank:auth-hydrated tolerates a broken localStorage read without throwing', async () => {
        await renderChat([userMessage('alice message')]);
        const original = window.localStorage;
        const failing: Storage = {
            ...original,
            getItem: () => { throw new Error('storage unavailable'); },
        };
        Object.defineProperty(window, 'localStorage', {value: failing, configurable: true});
        try {
            expect(() => {
                act(() => {
                    document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true, username: 'bob'}}));
                });
            }).not.toThrow();
            // A failed read is treated as no prior account: nothing to
            // compare against, so nothing is purged.
            expect(screen.getByText('alice message')).toBeInTheDocument();
        } finally {
            Object.defineProperty(window, 'localStorage', {value: original, configurable: true});
        }
    });
});

describe('preference proposal → apply → undo (issue #466 review)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    const undoToken = {expected_revision: 3, document: {notes: ''}};
    const changes = [
        {path: 'compensation.minimum_salary', old: null, new: 200000},
        {path: 'work_location.modes', old: [], new: ['remote']},
        {path: 'notes', old: '', new: 'prefers remote-first teams'},
    ];
    const proposal = {
        id: 'prop-1',
        scope: 'account',
        changes,
        change_count: 3,
        base_revision: 2,
        unsupported_criteria: [],
        token: {patch: {"set": {"notes": "prefers remote-first teams"}}, scope: 'account', base_revision: 2},
    };

    async function submitTurnWithProposal(payload: object) {
        await renderChat();
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(payload, 201));
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'only remote, 200k+'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await screen.findByTestId('preference-proposal-notice');
    }

    async function submitAndApply(applyResponse: object, status = 200) {
        await submitTurnWithProposal({
            message: assistantMessage(9, 'I suggest these updates.', false),
            preferences_changed: false,
            preference_proposal: proposal,
        });
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(applyResponse, status));
        fireEvent.click(screen.getByTestId('preference-apply-button'));
    }

    test('the turn renders the read-only proposal diff with Apply/Dismiss', async () => {
        await submitTurnWithProposal({
            message: assistantMessage(9, 'I suggest these updates.', false),
            preferences_changed: false,
            preference_proposal: proposal,
        });

        const notice = screen.getByTestId('preference-proposal-notice');
        expect(notice).toHaveAttribute('role', 'status');
        expect(screen.getByText('Compensation › minimum salary')).toBeInTheDocument();
        expect(screen.getByText('Work location › modes')).toBeInTheDocument();
        expect(screen.getByText('Notes')).toBeInTheDocument();
        expect(screen.getAllByText('Not set').length).toBeGreaterThanOrEqual(2);
        expect(screen.getByText('200,000')).toBeInTheDocument();
        expect(screen.getByText('prefers remote-first teams')).toBeInTheDocument();
        // Nothing was applied yet: no applied notice, no plain banner.
        expect(screen.queryByTestId('preference-change-notice')).not.toBeInTheDocument();
        expect(screen.queryByRole('status', {name: 'Preference update'})).not.toBeInTheDocument();
        expect(screen.getByTestId('preference-apply-button')).toBeEnabled();
        expect(screen.getByTestId('preference-proposal-dismiss-button')).toBeEnabled();
    });

    test('dismiss discards the proposal without any apply request', async () => {
        await submitTurnWithProposal({
            message: assistantMessage(10, 'I suggest these updates.', false),
            preferences_changed: false,
            preference_proposal: proposal,
        });
        fireEvent.click(screen.getByTestId('preference-proposal-dismiss-button'));
        expect(screen.queryByTestId('preference-proposal-notice')).not.toBeInTheDocument();
        const applyBodies = postBodies(global.fetch as jest.Mock, '/api/agent/preferences/apply/');
        expect(applyBodies).toHaveLength(0);
    });

    test('apply posts the token and renders the applied diff with Undo', async () => {
        await submitAndApply({
            applied: true, scope: 'account', revision: 3, changes, undo: undoToken,
        });
        const notice = await screen.findByTestId('preference-change-notice');
        expect(notice).toBeInTheDocument();
        expect(screen.getByText('Compensation › minimum salary')).toBeInTheDocument();
        expect(screen.getByRole('button', {name: 'Undo preference update'})).toBeEnabled();
        const applyBodies = postBodies(global.fetch as jest.Mock, '/api/agent/preferences/apply/');
        expect(applyBodies).toHaveLength(1);
        expect(applyBodies[0]).toEqual({proposal: proposal.token, decision: 'apply'});
        // The proposal is consumed.
        expect(screen.queryByTestId('preference-proposal-notice')).not.toBeInTheDocument();
    });

    test('apply button is disabled and busy while the request is in flight', async () => {
        await submitTurnWithProposal({
            message: assistantMessage(11, 'I suggest these updates.', false),
            preferences_changed: false,
            preference_proposal: proposal,
        });
        let resolveApply: (response: Response) => void = () => {};
        (global.fetch as jest.Mock).mockImplementationOnce(
            () => new Promise<Response>((resolve) => { resolveApply = resolve; }),
        );
        fireEvent.click(screen.getByTestId('preference-apply-button'));
        const busyButton = await screen.findByRole('button', {name: 'Applying preference proposal'});
        expect(busyButton).toBeDisabled();
        expect(busyButton).toHaveAttribute('aria-busy', 'true');
        await act(async () => {
            resolveApply(jsonResponse({
                applied: true, scope: 'account', revision: 3, changes, undo: undoToken,
            }));
        });
        expect(await screen.findByTestId('preference-change-notice')).toBeInTheDocument();
    });

    test('a stale conflict on apply surfaces the review action and applies nothing', async () => {
        await submitAndApply({
            error: {
                type: 'preference_stale',
                message: 'Your preferences changed since this proposal was prepared.',
                request_id: 'req-1',
            },
        }, 409);
        const errorPanel = await screen.findByTestId('preference-proposal-error');
        expect(errorPanel).toHaveAttribute('role', 'alert');
        expect(errorPanel).toHaveTextContent(/changed since this proposal/i);
        // The proposal stays visible; nothing was applied.
        expect(screen.getByTestId('preference-proposal-notice')).toBeInTheDocument();
        expect(screen.queryByTestId('preference-change-notice')).not.toBeInTheDocument();
        const reviewButton = screen.getByTestId('preference-proposal-review-button');
        expect(reviewButton).toHaveTextContent('Review current preferences');
        fireEvent.click(reviewButton);
        expect(screen.getByLabelText('Message')).toHaveFocus();
    });

    test('a network failure on apply shows retry copy, not the stale-only review action', async () => {
        await submitTurnWithProposal({
            message: assistantMessage(14, 'I suggest these updates.', false),
            preferences_changed: false,
            preference_proposal: proposal,
        });
        (global.fetch as jest.Mock).mockRejectedValueOnce(new Error('offline'));
        fireEvent.click(screen.getByTestId('preference-apply-button'));
        const errorPanel = await screen.findByTestId('preference-proposal-error');
        expect(errorPanel).toHaveTextContent(/check your connection/i);
        // Transient failure: retry stays possible; the stale-only review
        // action does NOT appear for a connectivity failure (issue #466 review).
        expect(screen.getByTestId('preference-apply-button')).toBeEnabled();
        expect(screen.queryByTestId('preference-proposal-review-button')).not.toBeInTheDocument();
    });

    test('a this-search-only proposal applies unsaved and confirms', async () => {
        const searchProposal = {...proposal, scope: 'search', token: {...proposal.token, scope: 'search'}};
        await submitTurnWithProposal({
            message: assistantMessage(15, 'Try this filter.', false),
            preferences_changed: false,
            preference_proposal: searchProposal,
        });
        expect(screen.getByText(/will not be saved/i)).toBeInTheDocument();
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({
            applied: false, scope: 'search',
            matches: {job_matches: [{listing_id: 1}], organization_matches: []},
        }));
        fireEvent.click(screen.getByTestId('preference-apply-button'));
        const confirmation = await screen.findByTestId('preference-search-applied');
        expect(confirmation).toHaveTextContent(/this search only/i);
        expect(confirmation).toHaveTextContent(/not saved/i);
        // Nothing persisted: no applied notice with Undo.
        expect(screen.queryByTestId('preference-change-notice')).not.toBeInTheDocument();
    });

    test('legacy response without a proposal renders no proposal notice', async () => {
        await renderChat();
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({
            message: assistantMessage(16, 'Reply.', false),
            preferences_changed: false,
        }, 201));
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'legacy'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await screen.findByText('Reply.');
        expect(screen.queryByTestId('preference-proposal-notice')).not.toBeInTheDocument();
    });

    test('keyboard: apply and dismiss controls are focusable buttons', async () => {
        await submitTurnWithProposal({
            message: assistantMessage(17, 'I suggest these updates.', false),
            preferences_changed: false,
            preference_proposal: proposal,
        });
        const applyButton = screen.getByTestId('preference-apply-button');
        applyButton.focus();
        expect(applyButton).toHaveFocus();
        const dismissButton = screen.getByTestId('preference-proposal-dismiss-button');
        dismissButton.focus();
        expect(dismissButton).toHaveFocus();
    });
});

describe('preference change undo (issue #466)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    const undoToken = {expected_revision: 3, document: {notes: ''}};
    const changes = [
        {path: 'compensation.minimum_salary', old: null, new: 200000},
        {path: 'notes', old: '', new: 'prefers remote-first teams'},
    ];
    const proposal = {
        id: 'prop-undo',
        scope: 'account',
        changes,
        change_count: 2,
        base_revision: 2,
        unsupported_criteria: [],
        token: {patch: {"set": {"notes": "prefers remote-first teams"}}, scope: 'account', base_revision: 2},
    };

    async function applyProposalThen() {
        await renderChat();
        (global.fetch as jest.Mock).mockResolvedValueOnce(statusResponse('ready'));
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({
            message: assistantMessage(20, 'I suggest these updates.', false),
            preferences_changed: false,
            preference_proposal: proposal,
        }, 201));
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'only remote'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await screen.findByTestId('preference-proposal-notice');
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({
            applied: true, scope: 'account', revision: 3, changes, undo: undoToken,
        }));
        fireEvent.click(screen.getByTestId('preference-apply-button'));
        await screen.findByTestId('preference-change-notice');
    }

    test('successful undo posts the token and confirms', async () => {
        await applyProposalThen();
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse({undone: true, revision: 4, changes: [{path: 'notes', old: 'x', new: ''}]}),
        );
        fireEvent.click(screen.getByRole('button', {name: 'Undo preference update'}));
        expect(await screen.findByTestId('preference-change-undone')).toHaveTextContent(/undone/i);

        const undoBodies = postBodies(global.fetch as jest.Mock, '/api/agent/preferences/undo/');
        expect(undoBodies).toHaveLength(1);
        expect(undoBodies[0]).toEqual({undo: undoToken});
        expect(screen.queryByTestId('preference-change-notice')).not.toBeInTheDocument();
    });

    test('loading state: undo button is disabled and busy while the request is in flight', async () => {
        await applyProposalThen();
        let resolveUndo: (response: Response) => void = () => {};
        (global.fetch as jest.Mock).mockImplementationOnce(
            () => new Promise<Response>((resolve) => { resolveUndo = resolve; }),
        );
        fireEvent.click(screen.getByRole('button', {name: 'Undo preference update'}));
        const busyButton = await screen.findByRole('button', {name: 'Undoing preference update'});
        expect(busyButton).toBeDisabled();
        expect(busyButton).toHaveAttribute('aria-busy', 'true');

        await act(async () => {
            resolveUndo(jsonResponse({undone: true, revision: 4, changes: []}));
        });
        expect(await screen.findByTestId('preference-change-undone')).toBeInTheDocument();
    });

    test('error state: a stale-revision conflict (409) surfaces the review action', async () => {
        await applyProposalThen();
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({
            error: {
                type: 'preference_stale',
                message: 'Your preferences changed since this update. Please review the current preferences before undoing.',
                request_id: 'req-1',
            },
        }, 409));
        fireEvent.click(screen.getByRole('button', {name: 'Undo preference update'}));
        const errorPanel = await screen.findByTestId('preference-undo-error');
        expect(errorPanel).toHaveAttribute('role', 'alert');
        expect(errorPanel).toHaveTextContent(/changed since this update/i);
        expect(screen.getByTestId('preference-change-notice')).toBeInTheDocument();
        expect(screen.getByRole('button', {name: 'Undo preference update'})).toBeEnabled();
        // The primary recovery after a stale conflict is reviewing the
        // current preferences, not retrying the dead undo token.
        const reviewButton = screen.getByTestId('preference-review-button');
        expect(reviewButton).toHaveTextContent('Review current preferences');
        fireEvent.click(reviewButton);
        expect(screen.getByLabelText('Message')).toHaveFocus();
    });

    test('error state: a network failure shows retry copy and NOT the stale-only review action', async () => {
        await applyProposalThen();
        (global.fetch as jest.Mock).mockRejectedValueOnce(new Error('offline'));
        fireEvent.click(screen.getByRole('button', {name: 'Undo preference update'}));
        const errorPanel = await screen.findByTestId('preference-undo-error');
        expect(errorPanel).toHaveTextContent(/check your connection/i);
        // Transient failure: retry stays possible; the stale-only review
        // action does NOT appear for a connectivity failure (issue #466 review).
        expect(screen.getByRole('button', {name: 'Undo preference update'})).toBeEnabled();
        expect(screen.queryByTestId('preference-review-button')).not.toBeInTheDocument();
    });

    test('dismiss hides the notice and discards the token', async () => {
        await applyProposalThen();
        fireEvent.click(screen.getByRole('button', {name: 'Dismiss preference notice'}));
        expect(screen.queryByTestId('preference-change-notice')).not.toBeInTheDocument();
        expect(screen.queryByRole('status', {name: 'Preference update'})).not.toBeInTheDocument();
    });
});

describe('preference diff formatting helpers (issue #466)', () => {
    test('preferencePathLabel prettifies dotted paths', async () => {
        const {preferencePathLabel} = await import('./JobSearchChat');
        expect(preferencePathLabel('compensation.minimum_salary')).toBe('Compensation › minimum salary');
        expect(preferencePathLabel('notes')).toBe('Notes');
        expect(preferencePathLabel('work_location.modes')).toBe('Work location › modes');
    });

    test('preferenceValueLabel renders every value shape', async () => {
        const {preferenceValueLabel} = await import('./JobSearchChat');
        expect(preferenceValueLabel(null)).toBe('Not set');
        expect(preferenceValueLabel(undefined)).toBe('Not set');
        expect(preferenceValueLabel('')).toBe('Not set');
        expect(preferenceValueLabel(true)).toBe('Yes');
        expect(preferenceValueLabel(false)).toBe('No');
        expect(preferenceValueLabel(200000)).toBe('200,000');
        expect(preferenceValueLabel('remote')).toBe('remote');
        expect(preferenceValueLabel([])).toBe('None');
        expect(preferenceValueLabel(['remote', 'hybrid'])).toBe('remote, hybrid');
        expect(preferenceValueLabel({channel: 'email'})).toBe('{"channel":"email"}');
    });
});

describe('workspace navigation state (issue #479)', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
        resetWorkspaceForTests();
        window.localStorage.clear();
    });

    afterEach(() => {
        resetWorkspaceForTests();
        window.localStorage.clear();
    });

    function conversationCalls(): unknown[][] {
        return (global.fetch as jest.Mock).mock.calls
            .filter(([url]) => String(url) === '/api/agent/conversations/');
    }

    test('an unknown account defers resume and pending-draft adoption until hydration, reconciling first', async () => {
        window.localStorage.setItem('crank:last-account', 'previous-user');
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'previous user secret');
        (global.fetch as jest.Mock).mockImplementation((url: string) => Promise.resolve(
            String(url).includes('/api/agent/assistant-status/')
                ? statusResponse('ready')
                : jsonResponse(emptyConversation(42, [])),
        ));
        render(<JobSearchChat workspaceMode="drawer"/>);
        await Promise.resolve();
        expect(conversationCalls()).toHaveLength(0);
        expect(screen.getByLabelText('Message')).toHaveValue('');

        act(() => setWorkspaceAccount({status: 'authenticated', key: 'new-user'}));

        await waitFor(() => expect(conversationCalls()).toHaveLength(1));
        await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
        // The previous account's pending draft was purged by the reconcile
        // that ran before the resume, so it is never adopted.
        expect(screen.getByLabelText('Message')).toHaveValue('');
        expect(window.localStorage.getItem('crank:jobsearch:draft:pending')).toBeNull();
        expect(window.localStorage.getItem('crank:last-account')).toBe('new-user');
        expect(getWorkspaceSnapshot().conversationId).toBe(42);
    });

    test('a same-account pending draft is adopted after hydration', async () => {
        window.localStorage.setItem('crank:last-account', 'alice');
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'carry me over');
        (global.fetch as jest.Mock).mockImplementation((url: string) => Promise.resolve(
            String(url).includes('/api/agent/assistant-status/')
                ? statusResponse('ready')
                : jsonResponse(emptyConversation(42, [])),
        ));
        render(<JobSearchChat workspaceMode="drawer"/>);
        act(() => setWorkspaceAccount({status: 'authenticated', key: 'alice'}));
        await waitFor(() => expect(screen.getByLabelText('Message')).toHaveValue('carry me over'));
    });

    test('an already-known account at mount (lazy mount after hydration) resumes without waiting', async () => {
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        (global.fetch as jest.Mock).mockImplementation((url: string) => Promise.resolve(
            String(url).includes('/api/agent/assistant-status/')
                ? statusResponse('ready')
                : jsonResponse(emptyConversation(7, [])),
        ));
        render(<JobSearchChat workspaceMode="docked"/>);
        await waitFor(() => expect(getWorkspaceSnapshot().conversationId).toBe(7));
        expect(window.localStorage.getItem('crank:last-account')).toBe('alice');
    });

    test('an anonymous account never issues the resume request', async () => {
        setWorkspaceAccount({status: 'anonymous', key: ''});
        (global.fetch as jest.Mock).mockImplementation(() => Promise.resolve(statusResponse('ready')));
        render(<JobSearchChat workspaceMode="docked"/>);
        await waitFor(() => expect(screen.getByLabelText('Message')).toBeInTheDocument());
        expect(conversationCalls()).toHaveLength(0);
        expect(getWorkspaceSnapshot().conversationId).toBeNull();
    });

    test('the conversation id follows reset in the store', async () => {
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        (global.fetch as jest.Mock).mockImplementation((url: string) => Promise.resolve(
            String(url).includes('/api/agent/assistant-status/')
                ? statusResponse('ready')
                : jsonResponse(emptyConversation(11, [])),
        ));
        render(<JobSearchChat workspaceMode="docked"/>);
        await waitFor(() => expect(getWorkspaceSnapshot().conversationId).toBe(11));
        act(() => { document.dispatchEvent(new CustomEvent('crank:private-state-purged')); });
        await waitFor(() => expect(getWorkspaceSnapshot().conversationId).toBe(11));
    });

    test('a reply that lands after the context changed is appended with an "Answered about" note and leaves the store alone', async () => {
        global.fetch = statusAwareFetch();
        await renderChat([]);
        act(() => setWorkspaceContext({surface: 'company', organizationId: 3, organizationName: 'Acme'}));
        const mock = global.fetch as jest.Mock;
        const settlePost = holdNextFetch(mock);
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'tell me about them'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await postedKeyAfterSend(mock);

        act(() => clearWorkspaceContext());
        const revision = getWorkspaceSnapshot().contextRevision;
        const contextAfterClear = getWorkspaceSnapshot().context;

        settlePost(jsonResponse({message: assistantMessage(99, 'Acme is a great fit'), preferences_changed: false}));

        await screen.findByText('Acme is a great fit');
        expect(screen.getByTestId('stale-context-note')).toHaveTextContent('Answered about Acme');
        expect(getWorkspaceSnapshot().contextRevision).toBe(revision);
        expect(getWorkspaceSnapshot().context).toEqual(contextAfterClear);
    });

    test('a reply under an unchanged context has no note', async () => {
        global.fetch = statusAwareFetch();
        await renderChat([]);
        act(() => setWorkspaceContext({surface: 'company', organizationId: 3, organizationName: 'Acme'}));
        const mock = global.fetch as jest.Mock;
        const settlePost = holdNextFetch(mock);
        fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'hello'}});
        fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
        await postedKeyAfterSend(mock);
        settlePost(jsonResponse({message: assistantMessage(98, 'Hi there'), preferences_changed: false}));
        await screen.findByText('Hi there');
        expect(screen.queryByTestId('stale-context-note')).not.toBeInTheDocument();
    });
});
