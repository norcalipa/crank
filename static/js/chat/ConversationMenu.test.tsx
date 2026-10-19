// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, waitFor, act} from '@testing-library/react';
import * as React from 'react';

import {CONFIRM_ARM_DELAY_MS} from './confirmArmDelay';
import {
    ConversationMenu,
    DROPPED,
    ConversationMenuProps,
    DELETE_CONVERSATION_COPY,
    NEW_CONVERSATION_COPY,
} from './ConversationMenu';
import {SESSION_ENDED_COPY} from '../session';

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

    describe('an action whose answer was dropped (issue #588)', () => {
        test('Export announces nothing and shows no error', async () => {
            const onExport = jest.fn().mockResolvedValue(DROPPED);
            setup({onExport});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-export'));
            await waitFor(() => expect(onExport).toHaveBeenCalledTimes(1));
            await act(async () => { await Promise.resolve(); });
            expect(screen.getByTestId('conversation-status')).toBeEmptyDOMElement();
            expect(screen.queryByTestId('export-error')).not.toBeInTheDocument();
        });

        test.each([
            ['conversation-new', 'onNewConversation'],
            ['conversation-delete', 'onDeleteConversation'],
        ] as const)('%s closes its confirmation without a failure', async (item, prop) => {
            const handler = jest.fn().mockResolvedValue(DROPPED);
            setup({[prop]: handler});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId(item));
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-action'));
            await waitFor(() => expect(screen.queryByTestId('confirm-panel')).not.toBeInTheDocument());
            expect(handler).toHaveBeenCalledTimes(1);
            expect(document.getElementById('chat-confirm-failure')).toBeNull();
            expect(screen.getByTestId('conversation-status')).toBeEmptyDOMElement();
        });
    });

    describe('revealing the focused retry inside the stack', () => {
        const box = (top: number, bottom: number) => ({top, bottom, left: 0, right: 0, width: 0, height: bottom - top, x: 0, y: top, toJSON: () => ''}) as DOMRect;
        // The stack spans 0-200 and pins nothing. The focused retry and the context
        // element sit at natural (unscrolled) positions and move with scrollTop.
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
                const scrolled = stack ? stack.scrollTop : 0;
                if (this === document.activeElement) return box(target - scrolled, target - scrolled + 30);
                if (this.matches(contextSelector)) return box(context - scrolled, context - scrolled + 30);
                return real.call(this);
            });
        };
        const failDelete = async () => {
            setup({onDeleteConversation: jest.fn().mockResolvedValue('Could not delete the conversation.')});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-delete'));
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-action'));
            await screen.findByRole('alert');
            return document.querySelector('.chat-panel-stack') as HTMLElement;
        };

        test('a retry below the stack is scrolled into view, with 4px of air', async () => {
            layOut(230, 230, '#chat-confirm-failure');
            const stack = await failDelete();
            // The retry's bottom (260) is 60px under the stack's bottom (200).
            await waitFor(() => expect(stack.scrollTop).toBe(64));
            expect(screen.getByTestId('confirm-action')).toHaveFocus();
        });

        test('a sentence above the stack comes down only as far as the retry stays visible', async () => {
            layOut(230, 20, '#chat-confirm-copy');
            const stack = await failDelete();
            // After the retry is revealed (64) the sentence (at 20) is 44px above the top, with 4px of room.
            await waitFor(() => expect(stack.scrollTop).toBe(60));
        });

        test('a retry and failure already inside the stack leave it where it is', async () => {
            layOut(100, 20, '#chat-confirm-failure');
            const stack = await failDelete();
            expect(stack.scrollTop).toBe(0);
        });

        test('after a failed Export first the export error sits above the confirmation, so revealing its retry never scrolls the sentence away', async () => {
            setup({onExport: jest.fn().mockResolvedValue('Could not export your conversation.')});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-delete'));
            armConfirm();
            fireEvent.click(screen.getByText('Export first'));
            const panel = await screen.findByTestId('export-error');
            const confirm = document.getElementById('chat-confirm-copy')!.closest('.chat-confirm-panel') as HTMLElement;
            expect(panel.compareDocumentPosition(confirm) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
            expect(panel.parentElement).toBe(confirm.parentElement);
        });

        test('an export error retry is revealed the same way', async () => {
            layOut(230, 0, '[data-testid="export-error"]');
            setup({onExport: jest.fn().mockResolvedValue('Could not export your conversation.')});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-export'));
            await screen.findByTestId('export-error');
            await waitFor(() => expect((document.querySelector('.chat-panel-stack') as HTMLElement).scrollTop).toBe(60));
        });
    });

    describe('a press that starts right after a confirmation opens is ignored', () => {
        const openConfirm = (testId: 'conversation-new' | 'conversation-delete') => {
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId(testId));
        };
        const confirmButton = () => screen.getByTestId('confirm-action');
        // A mouse or touch press: pointerdown, then (pointerup and a click after) the release.
        const down = () => fireEvent.pointerDown(confirmButton());
        const release = () => {
            fireEvent.pointerUp(confirmButton());
            fireEvent.click(confirmButton(), {detail: 1});
        };
        const press = () => { down(); release(); };
        // A click with no press (detail 0), which is what a key press on the focused
        // button sends. It is not what Firefox sends for an accessibility activation
        // (a detail 1 click with no pointerdown).
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

        test.each(kinds)('%s: a whole press made early is ignored however late it is handled', (testId, called) => {
            const {props} = setup();
            openConfirm(testId);
            advance(150);
            // The pointerdown and the click are stamped at 150 ms; the page gets to both 617 ms after opening.
            skewHandler(467);
            press();
            expectIgnored(props[called] as jest.Mock);
        });

        test.each(kinds)('%s: an opening click handled late, then a press 550 ms after it by event time and 2 ms after it by handler time, is ignored', (testId, called) => {
            const {props} = setup();
            // A frozen page: the opening click, made at 0 ms, is handled 600 ms late.
            skewHandler(600);
            openConfirm(testId);
            advance(550);
            // advance sets both clocks to the event clock plus 550, which discards the 600 ms
            // skew: the handler clock is then 50 ms short of the opening's, and the 52 puts it 2 ms past.
            skewHandler(52);
            press();
            expectIgnored(props[called] as jest.Mock);
        });

        test.each(kinds)('%s: an opening click handled late, then a press 500 ms after it by both clocks, is honoured', async (testId, called) => {
            const {props} = setup();
            // The opening click, made at 0 ms, is handled 600 ms late; the press is made and handled 500 ms after it.
            skewHandler(600);
            openConfirm(testId);
            advance(500);
            skewHandler(600);
            press();
            await waitFor(() => expect(props[called]).toHaveBeenCalledTimes(1));
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

        test.each(kinds)('%s: a detail 0 click (a key press) is judged by its own click: ignored at 499 ms, honoured at 500 ms', async (testId, called) => {
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

describe('ConversationMenu history follow-ups (issue #547)', () => {
    beforeEach(() => {
        setClocks(1000);
        jest.spyOn(performance, 'now').mockImplementation(() => handlerClock);
        Object.defineProperty(Event.prototype, 'timeStamp', {configurable: true, get: () => eventClock});
    });

    afterEach(() => {
        jest.restoreAllMocks();
        Object.defineProperty(Event.prototype, 'timeStamp', realTimeStamp!);
    });

    const held = <T,>() => {
        let settle: (value: T) => void = () => undefined;
        const promise = new Promise<T>((resolve) => { settle = resolve; });
        return {promise, settle: (value: T) => act(async () => { settle(value); await promise; })};
    };
    const openDelete = () => {
        fireEvent.click(more());
        fireEvent.click(screen.getByTestId('conversation-delete'));
    };

    describe('an export in flight', () => {
        test('Export first reads Exporting…, Delete is disabled, Cancel stays enabled, one status is announced', async () => {
            const exported = held<null>();
            const onExport = jest.fn(() => exported.promise);
            setup({onExport});
            openDelete();
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-export-first'));
            expect(screen.getByTestId('confirm-export-first')).toHaveTextContent('Exporting…');
            // aria-disabled, not disabled: the button keeps the keyboard focus it was pressed with.
            expect(screen.getByTestId('confirm-export-first')).toHaveAttribute('aria-disabled', 'true');
            expect(screen.getByTestId('confirm-action')).toBeDisabled();
            expect(screen.getByTestId('confirm-cancel')).toBeEnabled();
            expect(screen.getByTestId('conversation-status')).toHaveTextContent('Exporting conversation…');
            // The menu's Export item is disabled meanwhile.
            fireEvent.click(more());
            expect(screen.getByTestId('conversation-export')).toBeDisabled();
            expect(onExport).toHaveBeenCalledTimes(1);
            await exported.settle(null);
            expect(screen.getByTestId('confirm-action')).toBeEnabled();
            expect(screen.getByTestId('confirm-export-first')).toHaveTextContent('Export first');
            expect(screen.getByTestId('confirm-export-first')).toBeEnabled();
            expect(screen.getByTestId('confirm-export-first')).not.toHaveAttribute('aria-disabled');
            expect(screen.getByTestId('conversation-status')).toHaveTextContent('Conversation exported');
        });

        test('keyboard focus stays on Export first while it exports (visual review round 1, P1-3)', async () => {
            const exported = held<null>();
            setup({onExport: jest.fn(() => exported.promise)});
            openDelete();
            armConfirm();
            const exportFirst = screen.getByTestId('confirm-export-first');
            exportFirst.focus();
            fireEvent.click(exportFirst);
            expect(exportFirst).toHaveAttribute('aria-disabled', 'true');
            expect(document.activeElement).toBe(exportFirst);
            await exported.settle(null);
            expect(document.activeElement).toBe(exportFirst);
        });

        test('the action row follows the consequence and precedes Export first, so a short stack shows them together (round 1, P1-1/P1-2)', () => {
            setup();
            openDelete();
            const panel = screen.getByTestId('confirm-panel');
            const order = Array.from(panel.children).map((el) => el.className);
            expect(order).toEqual(['chat-confirm-heading', 'chat-confirm-copy', 'chat-confirm-dismiss', 'chat-confirm-actions']);
        });

        test('the ended-session failure turns "Sign in again" into a link back to this page (round 1, P2-3)', async () => {
            setup({onDeleteConversation: jest.fn(async () => SESSION_ENDED_COPY), signInUrl: '/accounts/login/'});
            openDelete();
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-action'));
            const link = await screen.findByTestId('confirm-sign-in');
            expect(link).toHaveTextContent('Sign in again');
            expect(link).toHaveAttribute('href', `/accounts/login/?next=${encodeURIComponent(window.location.pathname)}`);
            expect(screen.getByRole('alert')).toHaveTextContent(SESSION_ENDED_COPY);
        });

        test('an ended session without a sign-in address, and an ended export with one, show the copy as given', async () => {
            const {unmount} = setup({onDeleteConversation: jest.fn(async () => SESSION_ENDED_COPY)});
            openDelete();
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-action'));
            await screen.findByText(SESSION_ENDED_COPY);
            expect(screen.queryByTestId('confirm-sign-in')).toBeNull();
            unmount();
            setup({onExport: jest.fn(async () => SESSION_ENDED_COPY), signInUrl: '/accounts/login/'});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-export'));
            const panel = await screen.findByTestId('export-error');
            expect(panel).toContainElement(screen.getByTestId('confirm-sign-in'));
        });

        test('another failure shows plain text', async () => {
            setup({onDeleteConversation: jest.fn(async () => 'Could not delete the conversation.'), signInUrl: '/accounts/login/'});
            openDelete();
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-action'));
            await screen.findByText('Could not delete the conversation.');
            expect(screen.queryByTestId('confirm-sign-in')).toBeNull();
        });

        test('a second press while one is in flight starts no second request', async () => {
            const exported = held<null>();
            const onExport = jest.fn(() => exported.promise);
            setup({onExport});
            openDelete();
            const first = screen.getByTestId('confirm-export-first');
            // Both presses land before React has re-rendered the disabled button.
            act(() => { first.click(); first.click(); });
            expect(onExport).toHaveBeenCalledTimes(1);
            await exported.settle(null);
        });

        test('a failed export enables Delete again and shows the failure', async () => {
            const exported = held<string>();
            setup({onExport: jest.fn(() => exported.promise)});
            openDelete();
            fireEvent.click(screen.getByTestId('confirm-export-first'));
            expect(screen.getByTestId('confirm-action')).toBeDisabled();
            await exported.settle('Could not export your conversation.');
            expect(screen.getByTestId('confirm-action')).toBeEnabled();
            expect(screen.getByTestId('export-error')).toHaveTextContent('Could not export your conversation.');
            expect(screen.getByTestId('conversation-status')).toBeEmptyDOMElement();
        });

        test('a dropped export enables Delete again and shows nothing', async () => {
            const exported = held<typeof DROPPED>();
            setup({onExport: jest.fn(() => exported.promise)});
            openDelete();
            fireEvent.click(screen.getByTestId('confirm-export-first'));
            await exported.settle(DROPPED);
            expect(screen.getByTestId('confirm-action')).toBeEnabled();
            expect(screen.queryByTestId('export-error')).not.toBeInTheDocument();
            expect(screen.getByTestId('conversation-status')).toBeEmptyDOMElement();
        });
    });

    describe('the conversation going away', () => {
        test('withdraws the export failure and the export status with the other panels', async () => {
            const props: ConversationMenuProps = {
                title: <h2>Conversation</h2>, hasConversation: true, hasMessages: true, pending: false,
                onExport: jest.fn().mockResolvedValue('Could not export your conversation.'),
                onNewConversation: jest.fn(), onDeleteConversation: jest.fn(),
            };
            const {rerender} = render(<ConversationMenu {...props}/>);
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-export'));
            await screen.findByTestId('export-error');
            rerender(<ConversationMenu {...props} hasConversation={false} hasMessages={false}/>);
            expect(screen.queryByTestId('export-error')).not.toBeInTheDocument();
            expect(screen.getByTestId('conversation-status')).toBeEmptyDOMElement();
        });

        test('withdraws the "Conversation exported" announcement', async () => {
            const props: ConversationMenuProps = {
                title: <h2>Conversation</h2>, hasConversation: true, hasMessages: true, pending: false,
                onExport: jest.fn().mockResolvedValue(null),
                onNewConversation: jest.fn(), onDeleteConversation: jest.fn(),
            };
            const {rerender} = render(<ConversationMenu {...props}/>);
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-export'));
            await waitFor(() => expect(screen.getByTestId('conversation-status')).toHaveTextContent('Conversation exported'));
            rerender(<ConversationMenu {...props} hasConversation={false} hasMessages={false}/>);
            expect(screen.getByTestId('conversation-status')).toBeEmptyDOMElement();
        });

        test('an export still out when the account switches does not hold the next account\'s Delete', async () => {
            const old = held<string | null>();
            const next = held<string | null>();
            const onExport = jest.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
            const props: ConversationMenuProps = {
                title: <h2>Conversation</h2>, hasConversation: true, hasMessages: true, pending: false,
                onExport, onNewConversation: jest.fn(), onDeleteConversation: jest.fn(),
            };
            const {rerender} = render(<ConversationMenu {...props}/>);
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-export'));
            expect(screen.getByTestId('conversation-status')).toHaveTextContent('Exporting conversation…');
            // The account switch: the old conversation goes away, then the next account's own appears.
            rerender(<ConversationMenu {...props} hasConversation={false} hasMessages={false}/>);
            rerender(<ConversationMenu {...props}/>);
            openDelete();
            expect(screen.getByTestId('confirm-export-first')).toHaveTextContent('Export first');
            expect(screen.getByTestId('confirm-export-first')).not.toHaveAttribute('aria-disabled');
            expect(screen.getByTestId('confirm-action')).toBeEnabled();
            fireEvent.click(more());
            expect(screen.getByTestId('conversation-export')).toBeEnabled();
            // The next account's own export starts while the old one is still out.
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-export-first'));
            expect(onExport).toHaveBeenCalledTimes(2);
            expect(screen.getByTestId('confirm-export-first')).toHaveTextContent('Exporting…');
            // The old export settling now shows nothing in the next account's view, and releases nothing of the next one's.
            await old.settle('Could not export your conversation.');
            expect(screen.queryByTestId('export-error')).not.toBeInTheDocument();
            expect(screen.getByTestId('confirm-export-first')).toHaveTextContent('Exporting…');
            expect(screen.getByTestId('confirm-action')).toBeDisabled();
            expect(screen.getByTestId('conversation-status')).toHaveTextContent('Exporting conversation…');
            // A press while the next export is out starts no third request.
            fireEvent.click(screen.getByTestId('confirm-export-first'));
            expect(onExport).toHaveBeenCalledTimes(2);
            await next.settle(null);
            expect(screen.getByTestId('confirm-export-first')).toHaveTextContent('Export first');
        });

        test('Delete succeeding withdraws an earlier export failure', async () => {
            const onExport = jest.fn().mockResolvedValue('Could not export your conversation.');
            const Host = () => {
                const [has, setHas] = React.useState(true);
                return (
                    <ConversationMenu
                        title={<h2>Conversation</h2>} hasConversation={has} hasMessages={has} pending={false}
                        onExport={onExport} onNewConversation={jest.fn()}
                        onDeleteConversation={async () => { setHas(false); return null; }}
                    />
                );
            };
            render(<Host/>);
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-export'));
            await screen.findByTestId('export-error');
            openDelete();
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-action'));
            await waitFor(() => expect(screen.queryByTestId('confirm-panel')).not.toBeInTheDocument());
            expect(screen.queryByTestId('export-error')).not.toBeInTheDocument();
        });
    });

    describe('onConfirmationChange', () => {
        test('reports open on opening and closed on Cancel', () => {
            const onConfirmationChange = jest.fn();
            setup({onConfirmationChange});
            expect(onConfirmationChange).toHaveBeenLastCalledWith(false);
            openDelete();
            expect(onConfirmationChange).toHaveBeenLastCalledWith(true);
            fireEvent.click(screen.getByTestId('confirm-cancel'));
            expect(onConfirmationChange).toHaveBeenLastCalledWith(false);
        });

        test('stays open through a failure and closes on success', async () => {
            const onConfirmationChange = jest.fn();
            const onNewConversation = jest.fn().mockResolvedValueOnce('Could not reset the conversation.').mockResolvedValue(null);
            setup({onConfirmationChange, onNewConversation});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-new'));
            armConfirm();
            fireEvent.click(screen.getByTestId('confirm-action'));
            await screen.findByRole('alert');
            expect(onConfirmationChange).toHaveBeenLastCalledWith(true);
            fireEvent.click(screen.getByTestId('confirm-action'));
            await waitFor(() => expect(onConfirmationChange).toHaveBeenLastCalledWith(false));
        });

        test('closes when the conversation goes away', () => {
            const onConfirmationChange = jest.fn();
            const props: ConversationMenuProps = {
                title: <h2>Conversation</h2>, hasConversation: true, hasMessages: true, pending: false,
                onExport: jest.fn(), onNewConversation: jest.fn(), onDeleteConversation: jest.fn(), onConfirmationChange,
            };
            const {rerender} = render(<ConversationMenu {...props}/>);
            openDelete();
            expect(onConfirmationChange).toHaveBeenLastCalledWith(true);
            rerender(<ConversationMenu {...props} hasConversation={false}/>);
            expect(onConfirmationChange).toHaveBeenLastCalledWith(false);
        });

        test('About and an export failure are not confirmations', async () => {
            const onConfirmationChange = jest.fn();
            setup({onConfirmationChange, onExport: jest.fn().mockResolvedValue('Could not export your conversation.')});
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-about-history'));
            fireEvent.click(more());
            fireEvent.click(screen.getByTestId('conversation-export'));
            await screen.findByTestId('export-error');
            expect(onConfirmationChange).not.toHaveBeenCalledWith(true);
        });
    });

    test('opening a confirmation scrolls the stack to its heading', () => {
        const assigned: number[] = [];
        Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
            configurable: true,
            get() { return 50; },
            set(v: number) { assigned.push(v); },
        });
        try {
            setup();
            openDelete();
            expect(assigned).toContain(0);
            expect(screen.getByRole('heading', {name: 'Delete conversation?'})).toHaveFocus();
        } finally {
            delete (HTMLElement.prototype as {scrollTop?: unknown}).scrollTop;
        }
    });
});

describe('ConversationMenu sign-in link (issue #547 review)', () => {
    const endedProps = (signInUrl: string): ConversationMenuProps => ({
        title: <h2>Conversation</h2>, hasConversation: true, hasMessages: true, pending: false,
        onExport: jest.fn().mockResolvedValue(SESSION_ENDED_COPY),
        onNewConversation: jest.fn(), onDeleteConversation: jest.fn(), signInUrl,
    });
    const endedLink = async () => {
        fireEvent.click(screen.getByRole('button', {name: 'More'}));
        fireEvent.click(screen.getByTestId('conversation-export'));
        return screen.findByTestId('confirm-sign-in');
    };

    test('keeps a server-built sign-in address that already names where to return, company included', async () => {
        const given = '/accounts/login/?next=%2Fchat%2F%3Fcompany%3D5';
        render(<ConversationMenu {...endedProps(given)}/>);
        expect((await endedLink()).getAttribute('href')).toBe(given);
    });

    test('adds the current page as next when the address names none', async () => {
        window.history.pushState({}, '', '/jobs/?x=1');
        try {
            render(<ConversationMenu {...endedProps('/accounts/login/')}/>);
            expect((await endedLink()).getAttribute('href')).toBe('/accounts/login/?next=%2Fjobs%2F');
        } finally {
            window.history.pushState({}, '', '/');
        }
    });
});
