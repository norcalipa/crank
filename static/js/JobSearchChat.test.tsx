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

function assistantMessage(id: number, content: string, preferences_changed = false): ChatMessage {
    return {id, role: 'assistant', content, preferences_changed, created: null};
}

function userMessage(content: string): ChatMessage {
    return {id: 1, role: 'user', content, preferences_changed: false, created: null};
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
        test('renders an accessible input and live message history region', async () => {
            await renderChat();
            const input = screen.getByLabelText('Message');
            expect(input).toBeInTheDocument();
            expect(screen.getByRole('button', {name: 'Send message'})).toBeInTheDocument();
            expect(screen.getByLabelText('Message history')).toHaveAttribute('aria-live', 'polite');
            expect(screen.getByTestId('empty-history')).toBeInTheDocument();
        });

        test('renders existing message history', async () => {
            await renderChat([userMessage('hello'), assistantMessage(2, 'hi there')]);
            expect(screen.getByText('hello')).toBeInTheDocument();
            expect(screen.getByText('hi there')).toBeInTheDocument();
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

            await screen.findByText('Consider remote-friendly companies.');
            expect(screen.getByText('I need remote')).toBeInTheDocument();

            // Preference-change disclosure is announced.
            expect(await screen.findByText(/preferences were updated/i)).toBeInTheDocument();

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
