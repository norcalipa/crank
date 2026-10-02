// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, waitFor} from '@testing-library/react';
import * as React from 'react';

import {
    ConversationMenu,
    ConversationMenuProps,
    DELETE_CONVERSATION_COPY,
    NEW_CONVERSATION_COPY,
} from './ConversationMenu';

const setup = (overrides: Partial<ConversationMenuProps> = {}) => {
    const props: ConversationMenuProps = {
        title: <h2>Conversation</h2>,
        hasConversation: true,
        hasMessages: true,
        pending: false,
        onExport: jest.fn().mockResolvedValue(null),
        onNewConversation: jest.fn().mockResolvedValue(null),
        onDeleteConversation: jest.fn().mockResolvedValue(null),
        ...overrides,
    };
    const utils = render(<ConversationMenu {...props}/>);
    return {props, ...utils};
};

const more = () => screen.getByRole('button', {name: 'More'});

describe('ConversationMenu', () => {
    test('opens with focus on the first item and closes on Escape returning focus to More', () => {
        setup();
        fireEvent.click(more());
        expect(screen.getByRole('menuitem', {name: 'New conversation'})).toHaveFocus();
        fireEvent.keyDown(screen.getByRole('menu'), {key: 'Escape'});
        expect(screen.queryByRole('menu')).not.toBeInTheDocument();
        expect(more()).toHaveFocus();
    });

    test('arrow keys, Home and End move between enabled items and wrap', () => {
        setup({hasMessages: false});
        fireEvent.click(more());
        const menu = screen.getByRole('menu');
        // Export is disabled (no messages), so it is skipped.
        fireEvent.keyDown(menu, {key: 'ArrowDown'});
        expect(screen.getByRole('menuitem', {name: 'Delete conversation…'})).toHaveFocus();
        fireEvent.keyDown(menu, {key: 'End'});
        expect(screen.getByRole('menuitem', {name: 'About saved history'})).toHaveFocus();
        fireEvent.keyDown(menu, {key: 'ArrowDown'});
        expect(screen.getByRole('menuitem', {name: 'New conversation'})).toHaveFocus();
        fireEvent.keyDown(menu, {key: 'ArrowUp'});
        expect(screen.getByRole('menuitem', {name: 'About saved history'})).toHaveFocus();
        fireEvent.keyDown(menu, {key: 'Home'});
        expect(screen.getByRole('menuitem', {name: 'New conversation'})).toHaveFocus();
        // Unrelated keys leave focus and the menu alone.
        fireEvent.keyDown(menu, {key: 'a'});
        expect(screen.getByRole('menuitem', {name: 'New conversation'})).toHaveFocus();
        expect(screen.getByRole('menu')).toBeInTheDocument();
    });

    test('ArrowUp on More opens the menu on the last item; Tab closes it', () => {
        setup();
        fireEvent.keyDown(more(), {key: 'ArrowUp'});
        expect(screen.getByRole('menuitem', {name: 'About saved history'})).toHaveFocus();
        fireEvent.keyDown(screen.getByRole('menu'), {key: 'Tab'});
        expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });

    test('an outside press closes the menu; a press inside does not', () => {
        setup();
        fireEvent.click(more());
        fireEvent.mouseDown(screen.getByRole('menu'));
        expect(screen.getByRole('menu')).toBeInTheDocument();
        fireEvent.mouseDown(document.body);
        expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });

    test('New conversation asks inline, moves focus to the heading and Cancel restores focus', () => {
        const {props} = setup();
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-new'));
        expect(screen.getByRole('heading', {name: 'Start a new conversation?'})).toHaveFocus();
        expect(screen.getByTestId('confirm-panel')).toHaveTextContent(NEW_CONVERSATION_COPY);
        fireEvent.click(screen.getByTestId('confirm-cancel'));
        expect(screen.queryByTestId('confirm-panel')).not.toBeInTheDocument();
        expect(more()).toHaveFocus();
        expect(props.onNewConversation).not.toHaveBeenCalled();
    });

    test('Delete is gated behind its own confirmation and runs only on confirm', async () => {
        const {props} = setup();
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-delete'));
        expect(screen.getByTestId('confirm-panel')).toHaveTextContent(DELETE_CONVERSATION_COPY);
        expect(props.onDeleteConversation).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole('button', {name: 'Delete conversation'}));
        await waitFor(() => expect(props.onDeleteConversation).toHaveBeenCalledTimes(1));
        await waitFor(() => expect(screen.queryByTestId('confirm-panel')).not.toBeInTheDocument());
    });

    test('a failed confirmation keeps the panel, shows the error, and Try again retries', async () => {
        const onNewConversation = jest.fn()
            .mockResolvedValueOnce('Could not reset the conversation.')
            .mockResolvedValueOnce(null);
        setup({onNewConversation});
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-new'));
        fireEvent.click(screen.getByTestId('confirm-action'));
        expect(await screen.findByRole('alert')).toHaveTextContent('Could not reset the conversation.');
        const retry = screen.getByRole('button', {name: 'Try again'});
        await waitFor(() => expect(retry).toHaveFocus());
        fireEvent.click(retry);
        await waitFor(() => expect(screen.queryByTestId('confirm-panel')).not.toBeInTheDocument());
        expect(onNewConversation).toHaveBeenCalledTimes(2);
    });

    test('a double click on the confirm button submits once', async () => {
        let resolve: (v: null) => void = () => undefined;
        const onNewConversation = jest.fn(() => new Promise<null>((r) => { resolve = r; }));
        setup({onNewConversation});
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-new'));
        const confirm = screen.getByTestId('confirm-action');
        fireEvent.click(confirm);
        fireEvent.click(confirm);
        expect(onNewConversation).toHaveBeenCalledTimes(1);
        expect(confirm).toBeDisabled();
        resolve(null);
        await waitFor(() => expect(screen.queryByTestId('confirm-panel')).not.toBeInTheDocument());
    });

    test('Export runs without a confirmation, announces success and offers retry on failure', async () => {
        const onExport = jest.fn()
            .mockResolvedValueOnce('Could not export your conversation.')
            .mockResolvedValueOnce(null);
        setup({onExport});
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-export'));
        expect(await screen.findByTestId('export-error')).toHaveTextContent('Could not export your conversation.');
        fireEvent.click(screen.getByRole('button', {name: 'Try again'}));
        await waitFor(() => expect(screen.getByTestId('conversation-status')).toHaveTextContent('Conversation exported'));
        expect(screen.queryByTestId('export-error')).not.toBeInTheDocument();
    });

    test('New and Delete are disabled while a turn is pending, and a pending turn withdraws an open confirmation', () => {
        const {rerender, props} = setup();
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-delete'));
        expect(screen.getByTestId('confirm-panel')).toBeInTheDocument();
        rerender(<ConversationMenu {...props} pending/>);
        expect(screen.queryByTestId('confirm-panel')).not.toBeInTheDocument();
        fireEvent.click(more());
        expect(screen.getByTestId('conversation-new')).toBeDisabled();
        expect(screen.getByTestId('conversation-delete')).toBeDisabled();
    });

    test('without a conversation New, Export and Delete are disabled but About stays available', () => {
        setup({hasConversation: false});
        fireEvent.click(more());
        expect(screen.getByTestId('conversation-new')).toBeDisabled();
        expect(screen.getByTestId('conversation-export')).toBeDisabled();
        expect(screen.getByTestId('conversation-delete')).toBeDisabled();
        fireEvent.click(screen.getByTestId('conversation-about-history'));
        expect(screen.getByTestId('about-history-panel')).toBeInTheDocument();
        fireEvent.click(screen.getByTestId('about-history-close'));
        expect(screen.queryByTestId('about-history-panel')).not.toBeInTheDocument();
        expect(more()).toHaveFocus();
    });

    test('losing the conversation (purge) withdraws an open confirmation', () => {
        const {rerender, props} = setup();
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-new'));
        rerender(<ConversationMenu {...props} hasConversation={false}/>);
        expect(screen.queryByTestId('confirm-panel')).not.toBeInTheDocument();
    });
});
