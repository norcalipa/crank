// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, act} from '@testing-library/react';
import * as React from 'react';

import {useTranscriptScroll} from './useTranscriptScroll';

function Harness({assistantCount, extra = 0, loading = false, contentKey, band = false, log = true, block, footerKey}: {assistantCount: number; extra?: number; loading?: boolean; contentKey?: string; log?: boolean; band?: boolean; block?: 'collapsed' | 'open'; footerKey?: unknown}) {
    const {historyRef, showJumpToLatest, unreadCount, scrollToLatest, followNextAppend} = useTranscriptScroll({
        messagesLength: assistantCount + extra,
        assistantCount,
        pending: false,
        loading,
        contentKey,
        footerKey,
    });
    return (
        <div className="assistant-panel-body" data-testid="panel">
            {block && (
                <div className="priorities-sidebar">
                    <button data-testid="toggle">Priorities</button>
                    {block === 'open' && <div className="priorities-scroll"/>}
                </div>
            )}
            <section>
                <div className="card-header" data-testid="header"/>
                {log && (
                    <div ref={historyRef} data-testid="log">
                        <article data-testid="a1"/>
                        <article data-testid="a2"/>
                        <button data-testid="older-link">older</button>
                    </div>
                )}
                <div className="chat-footer" data-testid="footer">
                    {band ? <form data-testid="band"><textarea data-testid="composer"/></form> : <textarea data-testid="composer"/>}
                </div>
            </section>
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

// A write that does not move the scroller arms no window, so tests that need an
// armed one start from a scroller that is away from the bottom and follow it.
const armFollow = (scroller: HTMLElement) => {
    metrics(scroller, {scrollHeight: 2000, scrollTop: 0, clientHeight: 200});
    act(() => { fireEvent.click(screen.getByText('latest')); });
};

describe('useTranscriptScroll', () => {
    let now = 1_000_000;
    beforeEach(() => {
        now = 1_000_000;
        jest.spyOn(Date, 'now').mockImplementation(() => now);
        Element.prototype.scrollTo = jest.fn();
    });
    afterEach(() => jest.restoreAllMocks());

    test('counts every reply that lands while the reader is away and resets on return', () => {
        const {rerender} = render(<Harness assistantCount={1}/>);
        const log = screen.getByTestId('log');
        fireEvent.wheel(log);
        metrics(log, {scrollHeight: 1000, scrollTop: 0, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        rerender(<Harness assistantCount={3}/>);
        expect(screen.getByTestId('state')).toHaveTextContent('true:2');
        metrics(log, {scrollHeight: 1000, scrollTop: 800, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
    });

    test('following the next append clears the pill now and pins the bottom after the commit', () => {
        const {rerender} = render(<Harness assistantCount={2}/>);
        const log = screen.getByTestId('log');
        fireEvent.wheel(log);
        metrics(log, {scrollHeight: 1000, scrollTop: 100, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');

        fireEvent.click(screen.getByText('follow'));
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        // The new bubble grows the content; scroll lands after that commit.
        metrics(log, {scrollHeight: 1300, scrollTop: 100, clientHeight: 200});
        rerender(<Harness assistantCount={2} extra={1}/>);
        expect(log.scrollTop).toBe(1300);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');

        // One-shot: a later append does not force the scroll again.
        metrics(log, {scrollHeight: 1600, scrollTop: 1300, clientHeight: 200});
        rerender(<Harness assistantCount={2} extra={2}/>);
        expect(log.scrollTop).toBe(1300);
    });

    test('the programmatic window ends once the scroll reaches its target, so a later input-less scroll stops following', () => {
        const {rerender} = render(<Harness assistantCount={2}/>);
        const log = screen.getByTestId('log');
        metrics(log, {scrollHeight: 1000, scrollTop: 800, clientHeight: 200});
        fireEvent.scroll(log);
        fireEvent.click(screen.getByText('follow'));
        metrics(log, {scrollHeight: 1300, scrollTop: 800, clientHeight: 200});
        rerender(<Harness assistantCount={2} extra={1}/>);
        // Our own write lands on the bottom (target 1100): the window closes there.
        metrics(log, {scrollHeight: 1300, scrollTop: 1100, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        // 250ms later, find-in-page jumps to an older message with no input event.
        now += 250;
        metrics(log, {scrollHeight: 1300, scrollTop: 300, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
    });

    test('content growing during a follow-scroll does not disarm following; only an upward move does', () => {
        render(<Harness assistantCount={1}/>);
        const log = screen.getByTestId('log');
        armFollow(log);
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

    test('a write that leaves the scroller where it is arms no window', () => {
        render(<Harness assistantCount={1}/>);
        const log = screen.getByTestId('log');
        const scrollTo = jest.fn();
        log.scrollTo = scrollTo;
        // Already at the end (target 1500): following it again changes nothing.
        metrics(log, {scrollHeight: 2000, scrollTop: 1500, clientHeight: 500});
        act(() => { fireEvent.click(screen.getByText('latest')); });
        expect(scrollTo).toHaveBeenCalledWith({top: 2000, behavior: 'auto'});
        // 250ms later a jump to an older turn (no input event) must read as the reader leaving.
        now += 250;
        metrics(log, {scrollHeight: 2000, scrollTop: 600, clientHeight: 500});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
    });

    test('our own smooth scroll, still moving, does not end following; once it settles any move away does', () => {
        render(<Harness assistantCount={1}/>);
        const log = screen.getByTestId('log');
        armFollow(log);
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
        const {rerender} = render(<Harness assistantCount={1} extra={1}/>);
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
        rerender(<Harness assistantCount={2} extra={2}/>);
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

    test('focus landing in the composer does not end a write that is still settling', () => {
        render(<Harness assistantCount={1}/>);
        const log = screen.getByTestId('log');
        armFollow(log);
        // The composer takes focus (load focus) before the write's own scroll event arrives.
        fireEvent.focusIn(screen.getByTestId('composer'));
        // Content landed meanwhile, so the end is out of view when the clamped scroll reports.
        metrics(log, {scrollHeight: 2000, scrollTop: 16, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
    });

    test('typing in a control is not scroll intent, scroll keys outside controls are', () => {
        render(<Harness assistantCount={1}/>);
        const log = screen.getByTestId('log');
        armFollow(log);
        // Mid-way through our own scroll: it has not reached its target yet.
        metrics(log, {scrollHeight: 2000, scrollTop: 500, clientHeight: 200});
        fireEvent.scroll(log);
        const link = screen.getByTestId('older-link');
        fireEvent.keyDown(link, {key: 'a'});
        fireEvent.keyDown(link, {key: 'ArrowUp'});
        fireEvent.keyDown(log, {key: 'Enter'});
        // A layout shift pushes the end out of view with no reader scroll.
        metrics(log, {scrollHeight: 2400, scrollTop: 500, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        fireEvent.keyDown(log, {key: 'PageUp'});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
    });

    test('the first scroll after history loads jumps instead of smooth-scrolling', () => {
        const {rerender} = render(<Harness assistantCount={2} loading/>);
        const log = screen.getByTestId('log');
        const scrollTo = jest.fn();
        log.scrollTo = scrollTo;
        metrics(log, {scrollHeight: 3000, scrollTop: 0, clientHeight: 200});
        rerender(<Harness assistantCount={2}/>);
        expect(scrollTo).toHaveBeenLastCalledWith({top: 3000, behavior: 'auto'});
        scrollTo.mockClear();
        rerender(<Harness assistantCount={2} extra={1}/>);
        expect(scrollTo).toHaveBeenLastCalledWith({top: 3000, behavior: 'smooth'});
    });

    test('a reader who asked for reduced motion gets a jump, not a smooth scroll, when a message arrives', () => {
        const matchMedia = window.matchMedia;
        window.matchMedia = jest.fn().mockReturnValue({matches: true}) as unknown as typeof window.matchMedia;
        try {
            const {rerender} = render(<Harness assistantCount={2}/>);
            const log = screen.getByTestId('log');
            const scrollTo = jest.fn();
            log.scrollTo = scrollTo;
            metrics(log, {scrollHeight: 3000, scrollTop: 0, clientHeight: 200});
            rerender(<Harness assistantCount={2} extra={1}/>);
            expect(scrollTo).toHaveBeenLastCalledWith({top: 3000, behavior: 'auto'});
        } finally {
            window.matchMedia = matchMedia;
        }
    });

    test('a viewport resize after the transcript has gone hides the jump pill', () => {
        const {rerender} = render(<Harness assistantCount={1}/>);
        const log = screen.getByTestId('log');
        fireEvent.wheel(log);
        metrics(log, {scrollHeight: 1000, scrollTop: 0, clientHeight: 200});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        rerender(<Harness assistantCount={1} log={false}/>);
        act(() => { fireEvent(window, new Event('resize')); });
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
    });

    test('a viewport resize keeps a following reader at the end and leaves an away reader in place', () => {
        render(<Harness assistantCount={1}/>);
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
        render(<Harness assistantCount={1}/>);
        const log = screen.getByTestId('log');
        armFollow(log);
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
            render(<Harness assistantCount={1}/>);
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

    describe('the scroll event that comes with a new size of the transcript (a keyboard, a longer draft)', () => {
        const callbacks: Array<() => void> = [];
        const original = (window as unknown as {ResizeObserver?: unknown}).ResizeObserver;
        beforeEach(() => {
            callbacks.length = 0;
            (window as unknown as {ResizeObserver: unknown}).ResizeObserver = class {
                constructor(cb: () => void) { callbacks.push(cb); }
                observe() { /* noop */ }
                disconnect() { /* noop */ }
            };
        });
        afterEach(() => {
            (window as unknown as {ResizeObserver?: unknown}).ResizeObserver = original;
        });

        test('does not end following: the reader is still at the end once the resize step has run', () => {
            render(<Harness assistantCount={1}/>);
            const log = screen.getByTestId('log');
            const scrollTo = jest.fn();
            log.scrollTo = scrollTo;
            metrics(log, {scrollHeight: 1000, scrollTop: 800, clientHeight: 200});
            callbacks.forEach((cb) => cb());
            scrollTo.mockClear();
            // The box shrinks by 300px and the browser reports a scroll before the observer does.
            metrics(log, {scrollHeight: 1000, scrollTop: 800, clientHeight: 100});
            fireEvent.scroll(log);
            expect(screen.getByTestId('state')).toHaveTextContent('false:0');
            callbacks.forEach((cb) => cb());
            expect(scrollTo).toHaveBeenCalledWith({top: 1000, behavior: 'auto'});
        });

        test('is still the reader\'s when they have just given input, or once the size has settled', () => {
            render(<Harness assistantCount={1}/>);
            const log = screen.getByTestId('log');
            metrics(log, {scrollHeight: 1000, scrollTop: 800, clientHeight: 200});
            callbacks.forEach((cb) => cb());
            // A wheel and a changed box together: the reader scrolled.
            fireEvent.wheel(log);
            metrics(log, {scrollHeight: 1000, scrollTop: 300, clientHeight: 100});
            fireEvent.scroll(log);
            expect(screen.getByTestId('state')).toHaveTextContent('true:0');
            // Input long ago does not count, but a settled size gives no excuse either.
            now += 1000;
            metrics(log, {scrollHeight: 1000, scrollTop: 800, clientHeight: 100});
            callbacks.forEach((cb) => cb());
            metrics(log, {scrollHeight: 1000, scrollTop: 300, clientHeight: 100});
            fireEvent.scroll(log);
            expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        });
    });

    test('falls back to writing scrollTop where scrollTo is missing', () => {
        render(<Harness assistantCount={1}/>);
        const log = screen.getByTestId('log');
        (log as unknown as {scrollTo: unknown}).scrollTo = undefined;
        metrics(log, {scrollHeight: 900, scrollTop: 0, clientHeight: 200});
        act(() => { fireEvent.click(screen.getByText('latest')); });
        expect(log.scrollTop).toBe(900);
    });

    test('content arriving under the last message is followed once, by a jump, only while the reader is at the end', () => {
        const {rerender} = render(<Harness assistantCount={2} contentKey="a"/>);
        const log = screen.getByTestId('log');
        const scrollTo = jest.fn();
        log.scrollTo = scrollTo;
        metrics(log, {scrollHeight: 1200, scrollTop: 800, clientHeight: 200});
        rerender(<Harness assistantCount={2} contentKey="b"/>);
        expect(scrollTo).toHaveBeenCalledTimes(1);
        expect(scrollTo).toHaveBeenLastCalledWith({top: 1200, behavior: 'auto'});
        // The same key again follows nothing.
        scrollTo.mockClear();
        rerender(<Harness assistantCount={2} contentKey="b"/>);
        expect(scrollTo).not.toHaveBeenCalled();
        // A reader 49px above the end (one past the threshold) is not moved.
        fireEvent.wheel(log);
        metrics(log, {scrollHeight: 1200, scrollTop: 951, clientHeight: 200});
        fireEvent.scroll(log);
        rerender(<Harness assistantCount={2} contentKey="c"/>);
        expect(scrollTo).not.toHaveBeenCalled();
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
        // 48px above the end still counts as following.
        metrics(log, {scrollHeight: 1200, scrollTop: 952, clientHeight: 200});
        fireEvent.scroll(log);
        rerender(<Harness assistantCount={2} contentKey="d"/>);
        expect(scrollTo).toHaveBeenLastCalledWith({top: 1200, behavior: 'auto'});
    });

    test('late content is not followed while history loads or in an empty conversation', () => {
        const {rerender} = render(<Harness assistantCount={2} loading contentKey="a"/>);
        const log = screen.getByTestId('log');
        const scrollTo = jest.fn();
        log.scrollTo = scrollTo;
        metrics(log, {scrollHeight: 1200, scrollTop: 0, clientHeight: 200});
        rerender(<Harness assistantCount={2} loading contentKey="b"/>);
        expect(scrollTo).not.toHaveBeenCalled();
        rerender(<Harness assistantCount={0} contentKey="c"/>);
        expect(scrollTo).not.toHaveBeenCalled();
    });

    test('an empty conversation never shows the jump pill, however far its short log scrolls', () => {
        const {rerender} = render(<Harness assistantCount={0}/>);
        const log = screen.getByTestId('log');
        fireEvent.wheel(log);
        metrics(log, {scrollHeight: 400, scrollTop: 0, clientHeight: 60});
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('false:0');
        // With messages the same position is away from the end.
        rerender(<Harness assistantCount={1}/>);
        fireEvent.wheel(log);
        fireEvent.scroll(log);
        expect(screen.getByTestId('state')).toHaveTextContent('true:0');
    });

    describe('a viewport resize with the composer focused (panel body scrolling as a last resort)', () => {
        const bottoms = (footerBottom: number, panelBottom: number) => {
            jest.spyOn(screen.getByTestId('footer'), 'getBoundingClientRect').mockReturnValue(rect(footerBottom) as DOMRect);
            jest.spyOn(screen.getByTestId('panel'), 'getBoundingClientRect').mockReturnValue(rect(panelBottom) as DOMRect);
        };

        test('scrolls the panel body by the amount the footer is cut off', () => {
            render(<Harness assistantCount={1}/>);
            const panel = screen.getByTestId('panel');
            panel.scrollTop = 10;
            bottoms(460, 380);
            screen.getByTestId('composer').focus();
            act(() => { fireEvent(window, new Event('resize')); });
            expect(panel.scrollTop).toBe(90);
        });

        test('measures the composer band, which reaches past the footer into the panel\'s padding', () => {
            render(<Harness assistantCount={1} band/>);
            const panel = screen.getByTestId('panel');
            bottoms(380, 380);
            jest.spyOn(screen.getByTestId('band'), 'getBoundingClientRect').mockReturnValue(rect(396) as DOMRect);
            screen.getByTestId('composer').focus();
            act(() => { fireEvent(window, new Event('resize')); });
            expect(panel.scrollTop).toBe(16);
        });

        test('does nothing when the footer is not cut off', () => {
            render(<Harness assistantCount={1}/>);
            const panel = screen.getByTestId('panel');
            bottoms(380, 380);
            screen.getByTestId('composer').focus();
            act(() => { fireEvent(window, new Event('resize')); });
            expect(panel.scrollTop).toBe(0);
        });

        test('keeps the composer whole when nothing in it has the focus and the priorities row is collapsed', () => {
            render(<Harness assistantCount={1} block="collapsed"/>);
            const panel = screen.getByTestId('panel');
            bottoms(460, 380);
            screen.getByTestId('older-link').focus();
            act(() => { fireEvent(window, new Event('resize')); });
            expect(panel.scrollTop).toBe(80);
        });

        test('does nothing when the focus is in the priorities block', () => {
            render(<Harness assistantCount={1} block="collapsed"/>);
            const panel = screen.getByTestId('panel');
            bottoms(460, 380);
            screen.getByTestId('toggle').focus();
            act(() => { fireEvent(window, new Event('resize')); });
            expect(panel.scrollTop).toBe(0);
        });

        test('an open priorities block keeps its place unless the reader is typing', () => {
            render(<Harness assistantCount={1} block="open"/>);
            const panel = screen.getByTestId('panel');
            bottoms(460, 380);
            act(() => { fireEvent(window, new Event('resize')); });
            expect(panel.scrollTop).toBe(0);
            screen.getByTestId('composer').focus();
            act(() => { fireEvent(window, new Event('resize')); });
            expect(panel.scrollTop).toBe(80);
        });

        test('measures against the visual viewport when a keyboard covers the page without resizing it', () => {
            const original = Object.getOwnPropertyDescriptor(window, 'visualViewport');
            Object.defineProperty(window, 'visualViewport', {
                configurable: true,
                value: {height: 300, offsetTop: 20, addEventListener: jest.fn(), removeEventListener: jest.fn()},
            });
            try {
                render(<Harness assistantCount={1}/>);
                const panel = screen.getByTestId('panel');
                bottoms(460, 380);
                screen.getByTestId('composer').focus();
                act(() => { fireEvent(window, new Event('resize')); });
                expect(panel.scrollTop).toBe(140);
            } finally {
                if (original) Object.defineProperty(window, 'visualViewport', original);
                else delete (window as unknown as {visualViewport?: unknown}).visualViewport;
            }
        });

        test('a pinch zoom shrinks the visual viewport without hiding the composer: a scaled one is not measured', () => {
            const original = Object.getOwnPropertyDescriptor(window, 'visualViewport');
            const viewport = {height: 300, offsetTop: 20, scale: 2, addEventListener: jest.fn(), removeEventListener: jest.fn()};
            Object.defineProperty(window, 'visualViewport', {configurable: true, value: viewport});
            try {
                render(<Harness assistantCount={1}/>);
                const panel = screen.getByTestId('panel');
                bottoms(460, 380);
                screen.getByTestId('composer').focus();
                act(() => { fireEvent(window, new Event('resize')); });
                // Only the panel's own bottom limits the composer: 80, not 140.
                expect(panel.scrollTop).toBe(80);
                viewport.scale = 1;
                panel.scrollTop = 0;
                act(() => { fireEvent(window, new Event('resize')); });
                expect(panel.scrollTop).toBe(140);
            } finally {
                if (original) Object.defineProperty(window, 'visualViewport', original);
                else delete (window as unknown as {visualViewport?: unknown}).visualViewport;
            }
        });

        test('listens to the visual viewport itself, and lets go of it on unmount', () => {
            const original = Object.getOwnPropertyDescriptor(window, 'visualViewport');
            const viewport = {height: 300, offsetTop: 0, addEventListener: jest.fn(), removeEventListener: jest.fn()};
            Object.defineProperty(window, 'visualViewport', {configurable: true, value: viewport});
            try {
                const {unmount} = render(<Harness assistantCount={1}/>);
                const panel = screen.getByTestId('panel');
                bottoms(460, 380);
                screen.getByTestId('composer').focus();
                const [, handler] = viewport.addEventListener.mock.calls.find(([type]) => type === 'resize')!;
                act(() => { handler(); });
                expect(panel.scrollTop).toBe(160);
                unmount();
                expect(viewport.removeEventListener).toHaveBeenCalledWith('resize', handler);
            } finally {
                if (original) Object.defineProperty(window, 'visualViewport', original);
                else delete (window as unknown as {visualViewport?: unknown}).visualViewport;
            }
        });

        test('leaves the panel where it is when the composer band ends above the visible bottom', () => {
            render(<Harness assistantCount={1}/>);
            const panel = screen.getByTestId('panel');
            panel.scrollTop = 50;
            bottoms(300, 380);
            screen.getByTestId('composer').focus();
            act(() => { fireEvent(window, new Event('resize')); });
            expect(panel.scrollTop).toBe(50);
        });

        describe('when the footer grows (a failed send, a status notice, a longer draft)', () => {
            const observers: Array<{cb: () => void; target: Element | null; disconnected: boolean}> = [];
            const original = (window as unknown as {ResizeObserver?: unknown}).ResizeObserver;
            beforeEach(() => {
                observers.length = 0;
                (window as unknown as {ResizeObserver: unknown}).ResizeObserver = class {
                    private entry: {cb: () => void; target: Element | null; disconnected: boolean};
                    constructor(cb: () => void) { this.entry = {cb, target: null, disconnected: false}; observers.push(this.entry); }
                    observe(target: Element) { this.entry.target = target; }
                    disconnect() { this.entry.disconnected = true; }
                };
            });
            afterEach(() => {
                (window as unknown as {ResizeObserver?: unknown}).ResizeObserver = original;
            });
            const footerObservers = () => observers.filter((o) => o.target === screen.getByTestId('footer'));

            test('a reader typing keeps the composer on screen', () => {
                render(<Harness assistantCount={1}/>);
                const panel = screen.getByTestId('panel');
                bottoms(424, 380);
                screen.getByTestId('composer').focus();
                expect(footerObservers()).toHaveLength(1);
                act(() => { footerObservers()[0].cb(); });
                expect(panel.scrollTop).toBe(44);
            });

            test('a notice on load, with nothing focused, leaves the composer whole', () => {
                render(<Harness assistantCount={1} block="collapsed"/>);
                const panel = screen.getByTestId('panel');
                bottoms(437, 380);
                act(() => { footerObservers()[0].cb(); });
                expect(panel.scrollTop).toBe(57);
            });

            test('an empty conversation is observed once its history has loaded', () => {
                const {rerender} = render(<Harness assistantCount={0} loading log={false}/>);
                expect(footerObservers()).toHaveLength(0);
                rerender(<Harness assistantCount={0}/>);
                expect(footerObservers()).toHaveLength(1);
            });

            test('observes nothing while there is no transcript', () => {
                render(<Harness assistantCount={1} log={false}/>);
                expect(footerObservers()).toHaveLength(0);
            });

            test('the footer is observed again when it is mounted again, and released on unmount', () => {
                const {rerender, unmount} = render(<Harness assistantCount={1} footerKey="error"/>);
                const first = footerObservers()[0];
                rerender(<Harness assistantCount={1} footerKey={null}/>);
                expect(first.disconnected).toBe(true);
                const second = footerObservers().filter((o) => !o.disconnected);
                expect(second).toHaveLength(1);
                unmount();
                expect(second[0].disconnected).toBe(true);
            });
        });

        test('does nothing outside the assistant panel', () => {
            render(<Harness assistantCount={1}/>);
            const panel = screen.getByTestId('panel');
            panel.className = '';
            bottoms(460, 380);
            screen.getByTestId('composer').focus();
            act(() => { fireEvent(window, new Event('resize')); });
            expect(panel.scrollTop).toBe(0);
        });
    });
});
