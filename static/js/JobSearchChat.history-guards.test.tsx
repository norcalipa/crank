// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, act, waitFor} from '@testing-library/react';
import * as React from 'react';

import JobSearchChat from './JobSearchChat';
import type {ConversationMenuProps} from './chat/ConversationMenu';

// The menu is replaced by a stub that hands its props to the test, so the chat's
// handlers can be called at moments the real menu never allows (issue #547).
let menuProps: ConversationMenuProps | null = null;
jest.mock('./chat/ConversationMenu', () => {
    const actual = jest.requireActual('./chat/ConversationMenu');
    return {
        ...actual,
        ConversationMenu: (props: ConversationMenuProps) => {
            menuProps = props;
            return null;
        },
    };
});

const json = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
    status, headers: {'Content-Type': 'application/json'},
});
const conversation = (id: number) => ({
    id, active: true, created: null, modified: null, messages: [], preferences_changed: false,
});

describe('chat history handlers with no conversation (issue #547)', () => {
    beforeEach(() => {
        global.fetch = jest.fn((url: RequestInfo | URL) => Promise.resolve(
            String(url).includes('assistant-status')
                ? json({state: 'ready', actions: [], checked_at: '2026-09-14T00:00:00Z'})
                : String(url).endsWith('/delete/') ? json({deleted: true}) : json(conversation(42)),
        )) as unknown as typeof fetch;
    });

    afterEach(() => {
        menuProps = null;
        jest.restoreAllMocks();
    });

    test('Export after the conversation is gone reports an error, never success', async () => {
        render(<JobSearchChat/>);
        await waitFor(() => expect(menuProps?.hasConversation).toBe(true));
        await act(async () => { expect(await menuProps!.onDeleteConversation()).toBeNull(); });
        await waitFor(() => expect(menuProps?.hasConversation).toBe(false));
        const exportCalls = (global.fetch as jest.Mock).mock.calls.filter(([url]) => String(url).includes('/export/'));
        let outcome: unknown = null;
        await act(async () => { outcome = await menuProps!.onExport(); });
        expect(outcome).toBe('There is no conversation to export.');
        expect((global.fetch as jest.Mock).mock.calls.filter(([url]) => String(url).includes('/export/')))
            .toEqual(exportCalls);
        expect(screen.queryByText('Conversation exported')).not.toBeInTheDocument();
    });
});
