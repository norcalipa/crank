// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, act} from '@testing-library/react';
import * as React from 'react';

import {ScrollOwner, useTranscriptScroll} from './useTranscriptScroll';

function Harness({assistantCount, owner, extra = 0}: {assistantCount: number; owner: ScrollOwner; extra?: number}) {
    const {historyRef, showJumpToLatest, unreadCount, scrollToLatest, followNextAppend} = useTranscriptScroll({
        messagesLength: assistantCount + extra,
        assistantCount,
        pending: false,
        loading: false,
        scrollOwner: owner,
    });
    return (
        <div className="assistant-panel-body" data-testid="panel">
            <div ref={historyRef} data-testid="log"/>
            <span data-testid="state">{`${showJumpToLatest}:${unreadCount}`}</span>
            <button onClick={() => scrollToLatest('auto')}>latest</button>
            <button onClick={() => followNextAppend()}>follow</button>
        </div>
    );
}

const metrics = (el: HTMLElement, m: {scrollHeight: number; scrollTop: number; clientHeight: number}) => {
    Object.entries(m).forEach(([k, v]) => Object.defineProperty(el, k, {configurable: true, writable: true, value: v}));
};
const rect = (bottom: number) => ({bottom, top: bottom - 10, left: 0, right: 0, width: 0, height: 10, x: 0, y: 0, toJSON: () => ''});

describe('useTranscriptScroll', () => {
    beforeEach(() => {
        Element.prototype.scrollTo = jest.fn();
    });

    test('counts every reply that lands while the reader is away and resets on return', () => {
        const {rerender} = render(<Harness assistantCount={1} owner="transcript"/>);
        const log = screen.getByTestId('log');
        fireEvent.wheel(log);
        metrics(log, {scrollHeight: 1000, scrollTop: 0, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        rerender(<Harness assistantCount={3} owner="transcript"/>);
        expect(screen.getByTestId('state')).toHaveTextContent('true:2');
        metrics(log, {scrollHeight: 1000, scrollTop: 800, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
    });

    test('following the next append clears the pill now and pins the bottom after the commit', () => {
        const {rerender} = render(<Harness assistantCount={2} owner="transcript"/>);
        const log = screen.getByTestId('log');
        fireEvent.wheel(log);
        metrics(log, {scrollHeight: 1000, scrollTop: 100, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');

        fireEvent.click(screen.getByText('follow'));
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        // The new bubble grows the content; scroll lands after that commit.
        metrics(log, {scrollHeight: 1300, scrollTop: 100, clientHeight: 200});
        rerender(<Harness assistantCount={2} extra={1} owner="transcript"/>);
        expect(log.scrollTop).toBe(1300);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');

        // One-shot: a later append does not force the scroll again.
        metrics(log, {scrollHeight: 1600, scrollTop: 1300, clientHeight: 200});
        rerender(<Harness assistantCount={2} extra={2} owner="transcript"/>);
        expect(log.scrollTop).toBe(1300);
    });

    test('content growing during a follow-scroll does not disarm following; only an upward move does', () => {
        render(<Harness assistantCount={1} owner="transcript"/>);
        const log = screen.getByTestId('log');
        metrics(log, {scrollHeight: 1000, scrollTop: 800, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        // A reply stretches the content while the smooth scroll is still moving down.
        metrics(log, {scrollHeight: 1400, scrollTop: 850, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        // The reader moves up: following ends.
        fireEvent.wheel(log);
        metrics(log, {scrollHeight: 1400, scrollTop: 700, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
    });

    test('panel owner scrolls the panel body and reads "bottom" from the transcript edge', () => {
        render(<Harness assistantCount={1} owner="panel"/>);
        const log = screen.getByTestId('log');
        const panel = screen.getByTestId('panel');
        const panelScrollTo = jest.fn();
        const logScrollTo = jest.fn();
        panel.scrollTo = panelScrollTo;
        log.scrollTo = logScrollTo;
        metrics(panel, {scrollHeight: 2000, scrollTop: 100, clientHeight: 500});
        const logRect = jest.spyOn(log, 'getBoundingClientRect');
        jest.spyOn(panel, 'getBoundingClientRect').mockReturnValue(rect(500) as DOMRect);
        logRect.mockReturnValue(rect(900) as DOMRect);
        fireEvent.wheel(panel);
        fireEvent.scroll(panel);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        logRect.mockReturnValue(rect(520) as DOMRect);
        fireEvent.scroll(panel);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        act(() => { fireEvent.click(screen.getByText('latest')); });
        expect(panelScrollTo).toHaveBeenCalledWith({top: 2000, behavior: 'auto'});
        expect(logScrollTo).not.toHaveBeenCalled();
    });

    test('a layout shift that moves scrollTop without any input does not end following', () => {
        render(<Harness assistantCount={1} owner="transcript"/>);
        const log = screen.getByTestId('log');
        metrics(log, {scrollHeight: 1000, scrollTop: 800, clientHeight: 200});
        fireEvent.scroll(log);
        metrics(log, {scrollHeight: 1000, scrollTop: 500, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
    });

    test('entering panel mode re-reads the real position; leaving it returns to the latest turn', () => {
        const {rerender} = render(<Harness assistantCount={1} owner="transcript"/>);
        const log = screen.getByTestId('log');
        const panel = screen.getByTestId('panel');
        jest.spyOn(panel, 'getBoundingClientRect').mockReturnValue(rect(500) as DOMRect);
        jest.spyOn(log, 'getBoundingClientRect').mockReturnValue(rect(1500) as DOMRect);
        rerender(<Harness assistantCount={1} owner="panel"/>);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        jest.spyOn(log, 'getBoundingClientRect').mockReturnValue(rect(510) as DOMRect);
        fireEvent.scroll(panel);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        const scrollTo = jest.fn();
        log.scrollTo = scrollTo;
        rerender(<Harness assistantCount={1} owner="transcript"/>);
        expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({behavior: 'auto'}));
    });

    test('a scrollbar drag counts as the reader moving; a press on a child does not', () => {
        render(<Harness assistantCount={1} owner="transcript"/>);
        const log = screen.getByTestId('log');
        const child = document.createElement('button');
        log.appendChild(child);
        // A button press inside the log is not a drag, so nothing ends following.
        fireEvent.pointerDown(child);
        fireEvent.pointerUp(window);
        metrics(log, {scrollHeight: 1000, scrollTop: 100, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        // Dragging the scrollbar (a press on the scroller itself) does.
        fireEvent.pointerDown(log);
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        fireEvent.pointerUp(window);
    });

    test('a resized transcript stays at the end while following and is left alone otherwise', () => {
        const callbacks: Array<() => void> = [];
        const original = (window as unknown as {ResizeObserver?: unknown}).ResizeObserver;
        (window as unknown as {ResizeObserver: unknown}).ResizeObserver = class {
            constructor(cb: () => void) { callbacks.push(cb); }
            observe() { /* noop */ }
            disconnect() { /* noop */ }
        };
        try {
            render(<Harness assistantCount={1} owner="transcript"/>);
            const log = screen.getByTestId('log');
            const scrollTo = jest.fn();
            log.scrollTo = scrollTo;
            metrics(log, {scrollHeight: 1000, scrollTop: 0, clientHeight: 100});
            callbacks.forEach((cb) => cb());
            expect(scrollTo).toHaveBeenCalledWith({top: 1000, behavior: 'auto'});
            scrollTo.mockClear();
            callbacks.forEach((cb) => cb());
            expect(scrollTo).not.toHaveBeenCalled();
            fireEvent.wheel(log);
            metrics(log, {scrollHeight: 1000, scrollTop: 100, clientHeight: 200});
            fireEvent.scroll(log);
            metrics(log, {scrollHeight: 1000, scrollTop: 100, clientHeight: 150});
            callbacks.forEach((cb) => cb());
            expect(scrollTo).not.toHaveBeenCalled();
        } finally {
            (window as unknown as {ResizeObserver?: unknown}).ResizeObserver = original;
        }
    });
});
