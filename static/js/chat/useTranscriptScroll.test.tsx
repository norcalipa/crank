// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, act} from '@testing-library/react';
import * as React from 'react';

import {ScrollOwner, useTranscriptScroll} from './useTranscriptScroll';

function Harness({assistantCount, owner}: {assistantCount: number; owner: ScrollOwner}) {
    const {historyRef, showJumpToLatest, unreadCount, scrollToLatest} = useTranscriptScroll({
        messagesLength: assistantCount,
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
});
