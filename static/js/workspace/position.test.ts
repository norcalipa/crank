// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {
    installPositionTracking,
    readResultPosition,
    restoreResultPosition,
    saveResultPosition,
} from './position';

beforeEach(() => {
    window.history.replaceState(null, '', '/');
    window.scrollTo = jest.fn() as unknown as typeof window.scrollTo;
    Object.defineProperty(window, 'scrollY', {configurable: true, value: 240});
});

describe('result position', () => {
    test('save merges into existing history.state without clobbering keys', () => {
        window.history.replaceState({other: 1}, '');
        saveResultPosition('row-3');
        expect(window.history.state).toEqual({other: 1, crankPosition: {scrollY: 240, anchor: 'row-3', anchorOffset: 0}});
    });

    test('save works when history.state is empty', () => {
        saveResultPosition();
        expect(window.history.state).toEqual({crankPosition: {scrollY: 240, anchor: null, anchorOffset: 0}});
    });

    test('save swallows a throwing history API', () => {
        const spy = jest.spyOn(window.history, 'replaceState').mockImplementation(() => {
            throw new Error('denied');
        });
        expect(() => saveResultPosition()).not.toThrow();
        spy.mockRestore();
    });

    test('restore prefers the anchor, else scrollY; missing position is a no-op', () => {
        expect(restoreResultPosition(() => null)).toBe(false);
        expect(readResultPosition()).toBeNull();
        saveResultPosition('row-3');
        const el = document.createElement('div');
        expect(restoreResultPosition(() => el)).toBe(true);
        expect(window.scrollTo).toHaveBeenCalledTimes(1);
        expect(restoreResultPosition(() => null)).toBe(true);
        expect(window.scrollTo).toHaveBeenLastCalledWith({top: 240, behavior: 'instant'});
    });

    test('anchor restore lands the anchor at its saved viewport offset, not centered', () => {
        saveResultPosition('row-3', 120.4);
        expect(readResultPosition()).toEqual({scrollY: 240, anchor: 'row-3', anchorOffset: 120});
        const el = document.createElement('div');
        el.getBoundingClientRect = () => ({top: 600} as DOMRect);
        restoreResultPosition(() => el);
        expect(window.scrollTo).toHaveBeenCalledWith({top: 720, behavior: 'instant'});
    });

    test('malformed positions are ignored', () => {
        window.history.replaceState({crankPosition: {scrollY: 'x'}}, '');
        expect(readResultPosition()).toBeNull();
        window.history.replaceState({crankPosition: {scrollY: 5, anchor: 7}}, '');
        expect(readResultPosition()).toEqual({scrollY: 5, anchor: null, anchorOffset: 0});
    });

    test('tracking saves the anchor id and offset when the getter returns both', () => {
        const stop = installPositionTracking(() => ({id: 'a2', offset: 33}));
        window.dispatchEvent(new Event('pagehide'));
        expect(window.history.state.crankPosition).toEqual({scrollY: 240, anchor: 'a2', anchorOffset: 33});
        stop();
    });

    test('tracking saves on throttled scroll and pagehide, and tears down', () => {
        jest.useFakeTimers();
        const stop = installPositionTracking(() => 'a1');
        window.dispatchEvent(new Event('scroll'));
        window.dispatchEvent(new Event('scroll'));
        jest.advanceTimersByTime(250);
        expect(window.history.state.crankPosition.anchor).toBe('a1');
        window.history.replaceState(null, '');
        window.dispatchEvent(new Event('pagehide'));
        expect(window.history.state.crankPosition.scrollY).toBe(240);
        window.dispatchEvent(new Event('scroll'));
        stop();
        window.history.replaceState(null, '');
        jest.advanceTimersByTime(250);
        expect(window.history.state).toBeNull();
        jest.useRealTimers();
    });
});
