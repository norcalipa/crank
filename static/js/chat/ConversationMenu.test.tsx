// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, waitFor} from '@testing-library/react';
import * as React from 'react';

import {CONFIRM_ARM_DELAY_MS} from './confirmArmDelay';
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

// The confirm button ignores presses that start less than CONFIRM_ARM_DELAY_MS
// after its confirmation opened. The guard reads two clocks: the event time of
// the clicks and pointerdown (their timeStamp, which jsdom takes from the real
// clock, so it is replaced here) and the time its handlers run (performance.now).
// `setClocks` moves both together; a case about the difference moves
// `handlerClock` alone with `skewHandler`.
let eventClock = 0;
let handlerClock = 0;
const setClocks = (ms: number) => { eventClock = ms; handlerClock = ms; };
const advance = (ms: number) => setClocks(eventClock + ms);
const skewHandler = (ms: number) => { handlerClock += ms; };
const armConfirm = () => advance(CONFIRM_ARM_DELAY_MS);

const realTimeStamp = Object.getOwnPropertyDescriptor(Event.prototype, 'timeStamp');

describe('ConversationMenu', () => {
    beforeEach(() => {
        setClocks(1000);
        jest.spyOn(performance, 'now').mockImplementation(() => handlerClock);
        Object.defineProperty(Event.prototype, 'timeStamp', {configurable: true, get: () => eventClock});
    });

    afterEach(() => {
        jest.restoreAllMocks();
        Object.defineProperty(Event.prototype, 'timeStamp', realTimeStamp!);
    });

    test('opens with focus on the first item and closes on Escape returning focus to More', () => {
        setup();
        fireEvent.click(more());
        expect(screen.getByRole('menuitem', {name: 'New conversation…'})).toHaveFocus();
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
        expect(screen.getByRole('menuitem', {name: 'About saved history'})).toHaveFocus();
        fireEvent.keyDown(menu, {key: 'End'});
        expect(screen.getByRole('menuitem', {name: 'Delete conversation…'})).toHaveFocus();
        fireEvent.keyDown(menu, {key: 'ArrowDown'});
        expect(screen.getByRole('menuitem', {name: 'New conversation…'})).toHaveFocus();
        fireEvent.keyDown(menu, {key: 'ArrowUp'});
        expect(screen.getByRole('menuitem', {name: 'Delete conversation…'})).toHaveFocus();
        fireEvent.keyDown(menu, {key: 'Home'});
        expect(screen.getByRole('menuitem', {name: 'New conversation…'})).toHaveFocus();
        // Unrelated keys leave focus and the menu alone.
        fireEvent.keyDown(menu, {key: 'a'});
        expect(screen.getByRole('menuitem', {name: 'New conversation…'})).toHaveFocus();
        expect(screen.getByRole('menu')).toBeInTheDocument();
    });

    test('ArrowUp on More opens the menu on the last item; Tab closes it', () => {
        setup();
        fireEvent.keyDown(more(), {key: 'ArrowUp'});
        expect(screen.getByRole('menuitem', {name: 'Delete conversation…'})).toHaveFocus();
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
        armConfirm();
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
        armConfirm();
        fireEvent.click(screen.getByTestId('confirm-action'));
        expect(await screen.findByRole('alert')).toHaveTextContent('Could not reset the conversation.');
        const retry = screen.getByRole('button', {name: 'Try again'});
        await waitFor(() => expect(retry).toHaveFocus());
        fireEvent.click(retry);
        await waitFor(() => expect(screen.queryByTestId('confirm-panel')).not.toBeInTheDocument());
        expect(onNewConversation).toHaveBeenCalledTimes(2);
    });

    test('a failed delete offers an explicit "Try deleting again"', async () => {
        setup({onDeleteConversation: jest.fn().mockResolvedValue('Could not delete the conversation.')});
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-delete'));
        armConfirm();
        fireEvent.click(screen.getByTestId('confirm-action'));
        expect(await screen.findByRole('button', {name: 'Try deleting again'})).toBeInTheDocument();
    });

    test('the confirm button is described by the consequences and the failure, so a screen reader hears them', async () => {
        setup({onDeleteConversation: jest.fn().mockResolvedValue('Could not delete the conversation.')});
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-delete'));
        const confirm = screen.getByTestId('confirm-action');
        expect(confirm).toHaveAccessibleDescription(DELETE_CONVERSATION_COPY);
        armConfirm();
        fireEvent.click(confirm);
        await screen.findByRole('alert');
        expect(screen.getByTestId('confirm-action')).toHaveAccessibleDescription(
            `${DELETE_CONVERSATION_COPY} Could not delete the conversation.`,
        );
    });

    test('the confirm action sits beside Cancel in the pinned row; only Export first scrolls with the copy', () => {
        setup();
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-delete'));
        const pinned = screen.getByTestId('confirm-cancel').closest('.chat-confirm-dismiss')!;
        expect(Array.from(pinned.querySelectorAll('button')).map((b) => b.dataset.testid)).toEqual(['confirm-action', 'confirm-cancel']);
        expect(screen.getByTestId('confirm-export-first').closest('.chat-confirm-dismiss')).toBeNull();
        fireEvent.click(screen.getByTestId('confirm-cancel'));
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-new'));
        expect(screen.getByTestId('confirm-panel').querySelector('.chat-confirm-actions')).toBeNull();
        expect(screen.getByTestId('confirm-action').closest('.chat-confirm-dismiss')).not.toBeNull();
    });

    test('a double click on the confirm button submits once', async () => {
        let resolve: (v: null) => void = () => undefined;
        const onNewConversation = jest.fn(() => new Promise<null>((r) => { resolve = r; }));
        setup({onNewConversation});
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-new'));
        const confirm = screen.getByTestId('confirm-action');
        armConfirm();
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
        fireEvent.click(screen.getByRole('button', {name: 'Try export again'}));
        await waitFor(() => expect(screen.getByTestId('conversation-status')).toHaveTextContent('Conversation exported'));
        expect(screen.queryByTestId('export-error')).not.toBeInTheDocument();
    });

    test('an export error focuses retry without scrolling the page, and can be dismissed', async () => {
        setup({onExport: jest.fn().mockResolvedValue('Could not export your conversation.')});
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-export'));
        await screen.findByTestId('export-error');
        expect(screen.getByRole('button', {name: 'Try export again'})).toHaveFocus();
        fireEvent.click(screen.getByTestId('export-error-dismiss'));
        expect(screen.queryByTestId('export-error')).not.toBeInTheDocument();
        expect(more()).toHaveFocus();
    });

    describe('revealing the focused retry above the pinned dismiss row', () => {
        const box = (top: number, bottom: number) => ({top, bottom, left: 0, right: 0, width: 0, height: bottom - top, x: 0, y: top, toJSON: () => ''}) as DOMRect;
        // The stack spans 0-200 with its dismiss row pinned at 150-200. The focused retry and
        // the context element sit at natural (unscrolled) positions and move with scrollTop.
        let scrollTops: WeakMap<object, number>;
        beforeEach(() => {
            scrollTops = new WeakMap();
            Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
                configurable: true,
                get(this: HTMLElement) { return scrollTops.get(this) ?? 0; },
                set(this: HTMLElement, v: number) { scrollTops.set(this, v); },
            });
        });
        afterEach(() => {
            delete (HTMLElement.prototype as {scrollTop?: unknown}).scrollTop;
            jest.restoreAllMocks();
        });
        const layOut = (target: number, context: number, contextSelector: string) => {
            const real = HTMLElement.prototype.getBoundingClientRect;
            jest.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
                const stack = document.querySelector<HTMLElement>('.chat-panel-stack');
                if (this === stack) return box(0, 200);
                if (this.classList.contains('chat-confirm-dismiss')) return box(150, 200);
                const scrolled = stack ? stack.scrollTop : 0;
                if (this === document.activeElement) return box(target - scrolled, target - scrolled + 30);
                if (this.matches(contextSelector)) return box(context - scrolled, context - scrolled + 30);
                return real.call(this);
            });
        };

        test('a failure hidden under the pinned row is scrolled up; the pinned retry itself needs no scroll', async () => {
            layOut(160, 160, '#chat-confirm-failure');
            setup({onDeleteConversation: jest.fn().mockResolvedValue('Could not delete the conversation.')});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-delete'));
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-action'));
            await screen.findByRole('alert');
            const stack = document.querySelector('.chat-panel-stack') as HTMLElement;
            // The failure's bottom (190) is 40px under the row's top (150), plus 4px of air.
            await waitFor(() => expect(stack.scrollTop).toBe(44));
            expect(screen.getByTestId('confirm-action')).toHaveFocus();
        });

        test('a failure that already clears the pinned row leaves the stack where it is', async () => {
            layOut(160, 20, '#chat-confirm-failure');
            setup({onDeleteConversation: jest.fn().mockResolvedValue('Could not delete the conversation.')});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-delete'));
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-action'));
            await screen.findByRole('alert');
            expect((document.querySelector('.chat-panel-stack') as HTMLElement).scrollTop).toBe(0);
        });

        test('an export error retry is revealed the same way', async () => {
            layOut(160, 0, '[data-testid="export-error"]');
            setup({onExport: jest.fn().mockResolvedValue('Could not export your conversation.')});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-export'));
            await screen.findByTestId('export-error');
            await waitFor(() => expect((document.querySelector('.chat-panel-stack') as HTMLElement).scrollTop).toBe(40));
        });
    });

    describe('a press that starts right after a confirmation opens is ignored', () => {
        const openConfirm = (testId: 'conversation-new' | 'conversation-delete') => {
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId(testId));
        };
        const confirmButton = () => screen.getByTestId('confirm-action');
        // A mouse or touch press: pointerdown, then (a click after) the release.
        const down = () => fireEvent.pointerDown(confirmButton());
        const release = () => fireEvent.click(confirmButton(), {detail: 1});
        const press = () => { down(); release(); };
        // A keyboard or assistive-technology activation: a click with no press (detail 0).
        const activate = () => fireEvent.click(confirmButton());
        const expectIgnored = (called: jest.Mock) => {
            expect(called).not.toHaveBeenCalled();
            expect(screen.getByTestId('confirm-panel')).toBeInTheDocument();
            expect(screen.queryByRole('alert')).not.toBeInTheDocument();
            expect(confirmButton()).toBeEnabled();
        };
        const kinds = [
            ['conversation-delete', 'onDeleteConversation', 'onNewConversation'],
            ['conversation-new', 'onNewConversation', 'onDeleteConversation'],
        ] as const;

        test('the delay is 500 ms', () => {
            expect(CONFIRM_ARM_DELAY_MS).toBe(500);
        });

        test.each(kinds)('%s: ignored with no time between, at 499 ms, honoured at 500 ms', async (testId, called, other) => {
            const {props} = setup();
            openConfirm(testId);
            press();
            expectIgnored(props[called] as jest.Mock);
            advance(499);
            press();
            expectIgnored(props[called] as jest.Mock);
            advance(1);
            press();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
            expect(props[other]).not.toHaveBeenCalled();
            await waitFor(() => expect(screen.queryByTestId('confirm-panel')).not.toBeInTheDocument());
        });

        test.each(kinds)('%s: a press that goes down early is ignored however late it is released', (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(100);
            down();
            advance(500);
            release();
            expectIgnored(props[called] as jest.Mock);
        });

        test.each(kinds)('%s: a press down at 499 ms and released at 800 ms is ignored', (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(499);
            down();
            advance(301);
            release();
            expectIgnored(props[called] as jest.Mock);
        });

        test.each(kinds)('%s: a press down at 500 ms and released at 800 ms is honoured', async (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(500);
            down();
            advance(300);
            release();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
        });

        test.each(kinds)('%s: a click made early is ignored however late it is handled', (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(150);
            // The click is stamped at 150 ms and the page gets to it 617 ms after opening.
            skewHandler(467);
            release();
            expectIgnored(props[called] as jest.Mock);
        });

        test.each(kinds)('%s: a keyboard click made early is ignored however late it is handled', (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(150);
            skewHandler(467);
            activate();
            expectIgnored(props[called] as jest.Mock);
        });

        test.each(kinds)('%s: two presses 550 ms apart by their event times and handled 2 ms apart are ignored', (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            // A frozen page: both presses were made 550 ms apart and are handled 2 ms apart.
            advance(550);
            skewHandler(-548);
            press();
            expectIgnored(props[called] as jest.Mock);
        });

        test.each(kinds)('%s: a press that went down early and was released elsewhere does not block a keyboard click', async (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(100);
            down();
            advance(600);
            activate();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
        });

        test.each(kinds)('%s: a press that went down early and was released elsewhere does not block a later press', async (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(100);
            down();
            advance(600);
            press();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
        });

        test.each(kinds)('%s: a pointer click with no pointerdown is judged by its own time', async (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(499);
            release();
            expectIgnored(props[called] as jest.Mock);
            advance(1);
            release();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
        });

        test.each(kinds)('%s: the delay starts again after Cancel and a new open', async (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-cancel'));
            openConfirm(testId);
            press();
            expectIgnored(props[called] as jest.Mock);
            armConfirm();
            press();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
        });

        test.each(kinds)('%s: a press start left over from the previous confirmation is not carried into the next', async (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(100);
            down();
            fireEvent.click(screen.getByTestId('confirm-cancel'));
            advance(100);
            openConfirm(testId);
            armConfirm();
            release();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
        });

        test('opening the other confirmation starts the delay again', async () => {
            const {props} = setup();
            openConfirm('conversation-new');
            armConfirm();
            openConfirm('conversation-delete');
            press();
            expect(props.onDeleteConversation).not.toHaveBeenCalled();
            expect(props.onNewConversation).not.toHaveBeenCalled();
            armConfirm();
            press();
            await waitFor(() => expect(props.onDeleteConversation).toHaveBeenCalledTimes(1));
        });

        test.each(kinds)('%s: the delay counts from choosing the item, not from opening More', async (testId, called) => {
            const {props} = setup();
            fireEvent.click(more());
            advance(700);
            fireEvent.click(screen.getByTestId(testId));
            press();
            expectIgnored(props[called] as jest.Mock);
            armConfirm();
            press();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
        });

        test.each(kinds)('%s: choosing the same item again while its confirmation is open starts the delay again', async (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(600);
            openConfirm(testId);
            press();
            expectIgnored(props[called] as jest.Mock);
            armConfirm();
            press();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
        });

        test.each(kinds)('%s: a keyboard or assistive activation is judged by its own click: ignored at 499 ms, honoured at 500 ms', async (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            activate();
            expectIgnored(props[called] as jest.Mock);
            expect(screen.getByRole('heading', {name: testId === 'conversation-delete' ? 'Delete conversation?' : 'Start a new conversation?'})).toBeInTheDocument();
            advance(499);
            activate();
            expectIgnored(props[called] as jest.Mock);
            advance(1);
            activate();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
        });

        test('the confirmation still moves focus to its heading when it opens', () => {
            setup();
            openConfirm('conversation-delete');
            expect(screen.getByRole('heading', {name: 'Delete conversation?'})).toHaveFocus();
        });
    });

    test('the destructive item sits below a separator, after About', () => {
        setup();
        fireEvent.click(more());
        const items = screen.getAllByRole('menuitem').map((el) => el.textContent);
        expect(items).toEqual(['New conversation…', 'Export conversation', 'About saved history', 'Delete conversation…']);
        expect(screen.getByRole('separator')).toBeInTheDocument();
    });

    test('Export first runs the export from the delete confirmation and leaves it open', async () => {
        const {props} = setup();
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-delete'));
        fireEvent.click(screen.getByTestId('confirm-export-first'));
        await waitFor(() => expect(screen.getByTestId('conversation-status')).toHaveTextContent('Conversation exported'));
        expect(props.onExport).toHaveBeenCalledTimes(1);
        expect(screen.getByTestId('confirm-panel')).toBeInTheDocument();
        expect(more()).not.toHaveFocus();
    });

    test('Export first is disabled without messages and absent from the new-conversation panel', () => {
        setup({hasMessages: false});
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-delete'));
        expect(screen.getByTestId('confirm-export-first')).toBeDisabled();
        fireEvent.click(screen.getByTestId('confirm-cancel'));
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-new'));
        expect(screen.queryByTestId('confirm-export-first')).not.toBeInTheDocument();
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
