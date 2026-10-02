// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, act} from '@testing-library/react';
import * as React from 'react';

import {ScrollOwner, useTranscriptScroll} from './useTranscriptScroll';

function Harness({assistantCount, owner, extra = 0, loading = false}: {assistantCount: number; owner: ScrollOwner; extra?: number; loading?: boolean}) {
    const {historyRef, showJumpToLatest, unreadCount, scrollToLatest, followNextAppend, captureAnchor} = useTranscriptScroll({
        messagesLength: assistantCount + extra,
        assistantCount,
        pending: false,
        loading,
        scrollOwner: owner,
    });
    return (
        <div className="assistant-panel-body" data-testid="panel">
            <section>
                <div className="card-header" data-testid="header"/>
                <div ref={historyRef} data-testid="log">
                    <article data-testid="a1"/>
                    <article data-testid="a2"/>
                    <button data-testid="older-link">older</button>
                </div>
                <div className="chat-footer" data-testid="footer"><textarea data-testid="composer"/></div>
            </section>
            <span data-testid="state">{`${showJumpToLatest}:${unreadCount}`}</span>
            <button onClick={() => scrollToLatest('auto')}>latest</button>
            <button onClick={() => followNextAppend()}>follow</button>
            <button onClick={() => captureAnchor()}>capture</button>
        </div>
    );
}

const metrics = (el: HTMLElement, m: {scrollHeight: number; scrollTop: number; clientHeight: number}) => {
    Object.entries(m).forEach(([k, v]) => Object.defineProperty(el, k, {configurable: true, writable: true, value: v}));
};
const rect = (bottom: number) => ({bottom, top: bottom - 10, left: 0, right: 0, width: 0, height: 10, x: 0, y: 0, toJSON: () => ''});

describe('useTranscriptScroll', () => {
    let now = 1_000_000;
    beforeEach(() => {
        now = 1_000_000;
        jest.spyOn(Date, 'now').mockImplementation(() => now);
        Element.prototype.scrollTo = jest.fn();
    });
    afterEach(() => jest.restoreAllMocks());

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
        jest.spyOn(screen.getByTestId('footer'), 'getBoundingClientRect').mockReturnValue(rect(500) as DOMRect);
        logRect.mockReturnValue(rect(900) as DOMRect);
        fireEvent.wheel(panel);
        fireEvent.scroll(panel);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        logRect.mockReturnValue(rect(500) as DOMRect);
        fireEvent.scroll(panel);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        act(() => { fireEvent.click(screen.getByText('latest')); });
        expect(panelScrollTo).toHaveBeenCalledWith({top: 2000, behavior: 'auto'});
        expect(logScrollTo).not.toHaveBeenCalled();
    });

    test('our own smooth scroll, still moving, does not end following; once it settles any move away does', () => {
        render(<Harness assistantCount={1} owner="transcript"/>);
        const log = screen.getByTestId('log');
        metrics(log, {scrollHeight: 1000, scrollTop: 800, clientHeight: 200});
        fireEvent.scroll(log);
        metrics(log, {scrollHeight: 1000, scrollTop: 500, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        now += 5000;
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
    });

    test('scrolling away by Tab focus, find-in-page or a screen reader (no wheel, touch or scroll key) stops following', () => {
        const {rerender} = render(<Harness assistantCount={1} extra={1} owner="transcript"/>);
        const log = screen.getByTestId('log');
        const scrollTo = jest.fn();
        log.scrollTo = scrollTo;
        metrics(log, {scrollHeight: 2000, scrollTop: 1800, clientHeight: 200});
        fireEvent.scroll(log);
        now += 5000;
        fireEvent.keyDown(screen.getByTestId('older-link'), {key: 'Tab', shiftKey: true});
        fireEvent.focusIn(screen.getByTestId('older-link'));
        metrics(log, {scrollHeight: 2000, scrollTop: 400, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        scrollTo.mockClear();
        rerender(<Harness assistantCount={2} extra={2} owner="transcript"/>);
        expect(scrollTo).not.toHaveBeenCalled();
        expect(screen.getByTestId('state')).toHaveTextContent('true:1');

        // Same with no input event at all (find-in-page, assistive tech).
        metrics(log, {scrollHeight: 2000, scrollTop: 1800, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        now += 5000;
        metrics(log, {scrollHeight: 2000, scrollTop: 300, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
    });

    test('switching the scroll owner restores the reader\'s place instead of landing at the oldest messages', () => {
        const {rerender} = render(<Harness assistantCount={2} owner="transcript"/>);
        const log = screen.getByTestId('log');
        const panel = screen.getByTestId('panel');
        metrics(log, {scrollHeight: 2000, scrollTop: 600, clientHeight: 200});
        now += 5000;
        fireEvent.wheel(log);
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        // a1 is scrolled out above the view; a2 sits 20px below the top of the log.
        jest.spyOn(log, 'getBoundingClientRect').mockReturnValue({...rect(300), top: 100} as DOMRect);
        const a1 = jest.spyOn(screen.getByTestId('a1'), 'getBoundingClientRect');
        const a2 = jest.spyOn(screen.getByTestId('a2'), 'getBoundingClientRect');
        a1.mockReturnValue({...rect(90), top: 10} as DOMRect);
        // (log top is 100, so a1 is above the view)
        a2.mockReturnValue({...rect(220), top: 120} as DOMRect);
        fireEvent.click(screen.getByText('capture'));

        // In panel mode the view starts below the pinned header (bottom 160); a2 now sits at 500.
        metrics(panel, {scrollHeight: 3000, scrollTop: 0, clientHeight: 500});
        jest.spyOn(panel, 'getBoundingClientRect').mockReturnValue({...rect(600), top: 0} as DOMRect);
        jest.spyOn(screen.getByTestId('header'), 'getBoundingClientRect').mockReturnValue(rect(160) as DOMRect);
        a2.mockReturnValue({...rect(520), top: 500} as DOMRect);
        rerender(<Harness assistantCount={2} owner="panel"/>);
        expect(panel.scrollTop).toBe(500 - 160 - 20);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
    });

    test('a reader at the bottom has no anchor to restore and keeps following across the switch', () => {
        const {rerender} = render(<Harness assistantCount={1} owner="transcript"/>);
        const panel = screen.getByTestId('panel');
        panel.scrollTo = jest.fn();
        fireEvent.click(screen.getByText('capture'));
        rerender(<Harness assistantCount={1} owner="panel"/>);
        expect(panel.scrollTo).toHaveBeenCalledWith({top: expect.any(Number), behavior: 'auto'});
    });

    test('entering panel mode keeps a following reader at the end and gives an away reader the pill', () => {
        const {rerender} = render(<Harness assistantCount={1} owner="transcript"/>);
        const log = screen.getByTestId('log');
        const panel = screen.getByTestId('panel');
        const scrollTo = jest.fn();
        panel.scrollTo = scrollTo;
        rerender(<Harness assistantCount={1} owner="panel"/>);
        expect(scrollTo).toHaveBeenCalledWith({top: expect.any(Number), behavior: 'auto'});
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        log.scrollTo = jest.fn();
        rerender(<Harness assistantCount={1} owner="transcript"/>);
        expect(log.scrollTo).toHaveBeenCalledWith(expect.objectContaining({behavior: 'auto'}));

        fireEvent.wheel(log);
        metrics(log, {scrollHeight: 1000, scrollTop: 0, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        scrollTo.mockClear();
        rerender(<Harness assistantCount={1} owner="panel"/>);
        expect(scrollTo).not.toHaveBeenCalled();
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
    });

    test('panel "bottom" is measured to the pinned composer band, not the panel edge', () => {
        render(<Harness assistantCount={1} owner="panel"/>);
        const log = screen.getByTestId('log');
        const panel = screen.getByTestId('panel');
        jest.spyOn(panel, 'getBoundingClientRect').mockReturnValue(rect(600) as DOMRect);
        jest.spyOn(screen.getByTestId('footer'), 'getBoundingClientRect').mockReturnValue(rect(600) as DOMRect);
        // The footer rect top is 590; the transcript end at 700 sits behind the band.
        jest.spyOn(log, 'getBoundingClientRect').mockReturnValue(rect(700) as DOMRect);
        fireEvent.wheel(panel);
        fireEvent.scroll(panel);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        jest.spyOn(log, 'getBoundingClientRect').mockReturnValue(rect(600) as DOMRect);
        fireEvent.scroll(panel);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
    });

    test('typing in the composer is not scroll intent, scroll keys outside controls are', () => {
        render(<Harness assistantCount={1} owner="panel"/>);
        const log = screen.getByTestId('log');
        const panel = screen.getByTestId('panel');
        jest.spyOn(panel, 'getBoundingClientRect').mockReturnValue(rect(600) as DOMRect);
        jest.spyOn(screen.getByTestId('footer'), 'getBoundingClientRect').mockReturnValue(rect(600) as DOMRect);
        const logRect = jest.spyOn(log, 'getBoundingClientRect').mockReturnValue(rect(600) as DOMRect);
        fireEvent.scroll(panel);
        const composer = screen.getByTestId('composer');
        fireEvent.keyDown(composer, {key: 'a'});
        fireEvent.keyDown(composer, {key: 'ArrowUp'});
        fireEvent.keyDown(panel, {key: 'Enter'});
        // A layout shift pushes the end out of view with no reader scroll.
        logRect.mockReturnValue(rect(900) as DOMRect);
        fireEvent.scroll(panel);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        fireEvent.keyDown(panel, {key: 'PageUp'});
        fireEvent.scroll(panel);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
    });

    test('the first scroll after history loads jumps instead of smooth-scrolling', () => {
        const {rerender} = render(<Harness assistantCount={2} owner="transcript" loading/>);
        const log = screen.getByTestId('log');
        const scrollTo = jest.fn();
        log.scrollTo = scrollTo;
        metrics(log, {scrollHeight: 3000, scrollTop: 0, clientHeight: 200});
        rerender(<Harness assistantCount={2} owner="transcript"/>);
        expect(scrollTo).toHaveBeenLastCalledWith({top: 3000, behavior: 'auto'});
        scrollTo.mockClear();
        rerender(<Harness assistantCount={2} extra={1} owner="transcript"/>);
        expect(scrollTo).toHaveBeenLastCalledWith({top: 3000, behavior: 'smooth'});
    });

    test('a new message in panel mode jumps to the end instead of smooth-scrolling', () => {
        const {rerender} = render(<Harness assistantCount={2} owner="panel"/>);
        const panel = screen.getByTestId('panel');
        const scrollTo = jest.fn();
        panel.scrollTo = scrollTo;
        metrics(panel, {scrollHeight: 3000, scrollTop: 0, clientHeight: 300});
        rerender(<Harness assistantCount={2} extra={1} owner="panel"/>);
        expect(scrollTo).toHaveBeenLastCalledWith({top: 3000, behavior: 'auto'});
    });

    test('a viewport resize keeps a following reader at the end and leaves an away reader in place', () => {
        render(<Harness assistantCount={1} owner="transcript"/>);
        const log = screen.getByTestId('log');
        const scrollTo = jest.fn();
        log.scrollTo = scrollTo;
        // Keyboard opens: the scroller shrinks first, so the end looks far away.
        metrics(log, {scrollHeight: 1000, scrollTop: 700, clientHeight: 100});
        act(() => { fireEvent(window, new Event('resize')); });
        expect(scrollTo).toHaveBeenCalledWith({top: 1000, behavior: 'auto'});
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');

        fireEvent.wheel(log);
        metrics(log, {scrollHeight: 1000, scrollTop: 100, clientHeight: 200});
        fireEvent.scroll(log);
        scrollTo.mockClear();
        act(() => { fireEvent(window, new Event('resize')); });
        expect(scrollTo).not.toHaveBeenCalled();
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        // The reader is back at the end after the resize: following resumes.
        metrics(log, {scrollHeight: 1000, scrollTop: 790, clientHeight: 200});
        act(() => { fireEvent(window, new Event('resize')); });
        expect(scrollTo).toHaveBeenCalledWith({top: 1000, behavior: 'auto'});
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
    });

    test('a scrollbar drag cuts a smooth follow-scroll short and ends following', () => {
        render(<Harness assistantCount={1} owner="transcript"/>);
        const log = screen.getByTestId('log');
        metrics(log, {scrollHeight: 1000, scrollTop: 100, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        fireEvent.pointerDown(log);
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
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
