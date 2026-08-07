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