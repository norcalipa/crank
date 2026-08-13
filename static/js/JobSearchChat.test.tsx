// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, waitFor} from '@testing-library/react';
import * as React from 'react';

import JobSearchChat, {ChatMessage} from './JobSearchChat';

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

async function renderChat(existingMessages: ChatMessage[] = []) {
    // Resume the user's most recent conversation on mount.
    (global.fetch as jest.Mock).mockResolvedValueOnce(
        jsonResponse(emptyConversation(42, existingMessages)),
    );
    render(<JobSearchChat/>);
    // Wait until resume resolves and the input is enabled.
    await screen.findByLabelText('Message');
    await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
}

function messageUrl() {
    return '/api/agent/conversations/42/';
}

function postBodies(fetchMock: jest.Mock, urlPrefix: string) {
    return fetchMock.mock.calls
        .filter(([url]) => String(url).startsWith(urlPrefix))
        .map(([url, init]) => JSON.parse((init as RequestInit).body as string));
}

describe('JobSearchChat', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    describe('rendering & accessibility semantics', () => {
        test('uses a viewport-aware flex layout with a pinned composer', async () => {
            await renderChat();
            const chat = screen.getByTestId('job-search-chat');
            const history = screen.getByLabelText('Message history');
            const composer = document.querySelector('form')!.parentElement!;
            expect(chat).toHaveClass('d-flex', 'flex-column');
            expect(chat).toHaveStyle({height: 'calc(100dvh - 7rem)'});
            expect(history).toHaveClass('flex-grow-1');
            expect(history).toHaveStyle({overflowY: 'auto'});
            expect(composer).toHaveClass('flex-shrink-0');
        });

        test('renders an accessible input and live message history region', async () => {
            await renderChat();
            const input = screen.getByLabelText('Message');
            expect(input).toBeInTheDocument();
            expect(screen.getByRole('button', {name: 'Send message'})).toBeInTheDocument();
            expect(screen.getByRole('region', {name: 'Conversation'})).toBeInTheDocument();
            expect(screen.getByRole('note')).toHaveTextContent(/automated and can be wrong/i);
            expect(screen.getByRole('note')).toHaveTextContent(/saved to your account/i);
            expect(screen.getByLabelText('Message history')).toHaveAttribute('aria-live', 'polite');
            expect(screen.getByLabelText('Message history')).toHaveAttribute('aria-busy', 'false');
            expect(screen.getByTestId('empty-history')).toBeInTheDocument();
        });

        test('renders existing message history', async () => {
            await renderChat([userMessage('hello'), assistantMessage(2, 'hi there')]);
            expect(screen.getByText('hello')).toBeInTheDocument();
            expect(screen.getByText('hi there')).toBeInTheDocument();
            expect(screen.getByRole('article', {name: 'Your message'})).toHaveTextContent('hello');
            expect(screen.getByRole('article', {name: 'Assistant message'})).toHaveTextContent('hi there');
            expect(screen.queryByTestId('empty-history')).not.toBeInTheDocument();
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
            expect(scrollTo).toHaveBeenCalledWith({top: history.scrollHeight, behavior: 'smooth'});
            delete (window as unknown as {matchMedia?: unknown}).matchMedia;
        });

        test('uses instant scrolling when reduced motion is preferred', async () => {
            window.matchMedia = jest.fn().mockReturnValue({matches: true} as MediaQueryList);
            await renderChat([assistantMessage(1, 'latest')]);
            const history = screen.getByLabelText('Message history');
            expect(scrollTo).toHaveBeenCalledWith({top: history.scrollHeight, behavior: 'auto'});
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
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({
                    message: assistantMessage(3, 'Consider remote-friendly companies.'),
                    preferences_changed: true,
                }, 201),
            );

            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'I need remote'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));

            expect(screen.getByLabelText('Message history')).toHaveAttribute('aria-busy', 'true');
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
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({message: assistantMessage(4, 'Enter works'), preferences_changed: false}, 201),
            );
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'just pressing enter'}});
            fireEvent.submit(screen.getByRole('textbox', {name: 'Message'}));
            await screen.findByText('Enter works');
        });
    });

    describe('error & retry', () => {
        test('surfaces a stable error and retries with the same idempotency key', async () => {
            await renderChat();
            const mockFetch = global.fetch as jest.Mock;
            mockFetch
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
    });

    describe('reset / delete controls', () => {
        test('delete removes the conversation and resets the UI', async () => {
            await renderChat([userMessage('done with this')]);
            window.confirm = jest.fn().mockReturnValue(true);
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse({deleted: true}),
            );

            fireEvent.click(screen.getByLabelText('Delete conversation'));
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
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({detail: 'boom'}, 500));
            render(<JobSearchChat/>);
            expect(await screen.findByText(/could not load your conversation/i)).toBeInTheDocument();
            expect(screen.getByRole('button', {name: 'Start a conversation'})).toBeInTheDocument();
        });

        test('auto-creates a conversation on resume 404 so the input is usable', async () => {
            const mock = global.fetch as jest.Mock;
            // First call: GET resume → 404 (no existing conversation).
            mock.mockResolvedValueOnce(jsonResponse({}, 404));
            // Second call: POST create → new conversation.
            mock.mockResolvedValueOnce(jsonResponse(emptyConversation(7), 201));
            render(<JobSearchChat/>);
            await screen.findByLabelText('Message');
            await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled());
            expect(screen.getByTestId('empty-history')).toBeInTheDocument();
            expect(screen.queryByText(/could not load/i)).not.toBeInTheDocument();
            // Verify a POST with create_new was made.
            const posts = mock.mock.calls
                .map(([url, init]) => ({url: String(url), init: init as RequestInit}))
                .filter((c) => c.init?.method === 'POST');
            expect(posts.length).toBeGreaterThanOrEqual(1);
            const body = JSON.parse(posts[0].init.body as string);
            expect(body.create_new).toBe(true);
        });

        test('shows init error when auto-create after 404 also fails', async () => {
            const mock = global.fetch as jest.Mock;
            // First call: GET resume → 404 (no existing conversation).
            mock.mockResolvedValueOnce(jsonResponse({}, 404));
            // Second call: POST create → 500 (server error).
            mock.mockResolvedValueOnce(jsonResponse({}, 500));
            render(<JobSearchChat/>);
            expect(await screen.findByText(/could not start a conversation/i)).toBeInTheDocument();
            expect(screen.queryByLabelText('Message')).toBeDisabled();
        });

        test('starting a new conversation from the error state works', async () => {
            const mock = global.fetch as jest.Mock;
            mock.mockResolvedValueOnce(jsonResponse({detail: 'down'}, 503))
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
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
            render(<JobSearchChat/>);
            await screen.findByLabelText('Message');
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
                (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
                render(<JobSearchChat/>);
                await screen.findByLabelText('Message');
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
        test('rolls back the optimistic user turn on a non-JSON error', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [assistantMessage(0, 'ready')])),
            );
            render(<JobSearchChat/>);
            await screen.findByText('ready');
            (global.fetch as jest.Mock).mockResolvedValueOnce(new Response('plain text error', {status: 500}));
            fireEvent.change(screen.getByLabelText('Message'), {target: {value: 'boom'}});
            fireEvent.click(screen.getByRole('button', {name: 'Send message'}));
            await screen.findByText(/request failed \(500\)/i);
            expect(screen.queryByText('boom')).not.toBeInTheDocument();
            expect(screen.getByTestId('retry-button')).toBeInTheDocument();
        });

        test('dismisses the preference-update notice', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
            render(<JobSearchChat/>);
            await screen.findByLabelText('Message');
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

            fireEvent.click(screen.getByRole('button', {name: 'Export conversation'}));
            await waitFor(() => expect(click).toHaveBeenCalled());
            expect(createUrl).toHaveBeenCalled();
            expect(revoke).toHaveBeenCalled();
            expect(screen.queryByText(/could not export/i)).not.toBeInTheDocument();
        });

        test('surfaces an error when export fails', async () => {
            (global.fetch as jest.Mock).mockResolvedValueOnce(
                jsonResponse(emptyConversation(42, [userMessage('x')])),
            );
            render(<JobSearchChat/>);
            await screen.findByText('x');
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 500));
            fireEvent.click(screen.getByRole('button', {name: 'Export conversation'}));
            expect(await screen.findByText(/could not export your conversation/i)).toBeInTheDocument();
        });
    });

    describe('DOM bootstrap', () => {
        test('mounts itself onto a #job-search-chat element on DOMContentLoaded', async () => {
            const div = document.createElement('div');
            div.id = 'job-search-chat';
            document.body.appendChild(div);
            try {
                document.dispatchEvent(new Event('DOMContentLoaded', {bubbles: true}));
                await waitFor(() => expect(div.querySelector('.card.bg-dark')).toBeTruthy());
            } finally {
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
            (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(42)));
            render(<JobSearchChat/>);
            await screen.findByLabelText('Message');
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
        mock.mockResolvedValueOnce(jsonResponse({detail: 'down'}, 503))
            .mockResolvedValueOnce(jsonResponse({}, 500));
        render(<JobSearchChat/>);
        await screen.findByText(/could not load your conversation/i);
        fireEvent.click(screen.getByRole('button', {name: 'Start a conversation'}));
        await screen.findByText(/could not start a conversation/i);
    });

    test('reset succeeds when confirmed', async () => {
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [userMessage('old')])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('old');
        window.confirm = jest.fn().mockReturnValue(true);
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse(emptyConversation(7)));
        fireEvent.click(screen.getByLabelText('Reset conversation'));
        await waitFor(() => expect(screen.getByTestId('empty-history')).toBeInTheDocument());
        expect(screen.queryByText('old')).not.toBeInTheDocument();
    });

    test('reset surfaces an error when it fails', async () => {
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [userMessage('keep')])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('keep');
        window.confirm = jest.fn().mockReturnValue(true);
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 500));
        fireEvent.click(screen.getByLabelText('Reset conversation'));
        await screen.findByText(/could not reset the conversation/i);
        expect(screen.getByText('keep')).toBeInTheDocument();
    });

    test('delete surfaces an error when it fails', async () => {
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [userMessage('del')])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('del');
        window.confirm = jest.fn().mockReturnValue(true);
        (global.fetch as jest.Mock).mockResolvedValueOnce(jsonResponse({}, 500));
        fireEvent.click(screen.getByLabelText('Delete conversation'));
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
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Just text, no cards.');
        expect(screen.queryByTestId('result-cards')).not.toBeInTheDocument();
    });

    test('no result cards when results have empty arrays', async () => {
        const msg = assistantMessage(5, 'No matches found.', false, {jobs: [], organizations: []});
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
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [msg])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('Reload test.');
        expect(screen.getByText('Senior Engineer')).toBeInTheDocument();
        expect(screen.getByText('Acme Inc')).toBeInTheDocument();
    });

    test('new reply with results renders cards after submit', async () => {
        (global.fetch as jest.Mock).mockResolvedValueOnce(
            jsonResponse(emptyConversation(42, [userMessage('jobs?')])),
        );
        render(<JobSearchChat/>);
        await screen.findByText('jobs?');
        const results = makeResults();
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
