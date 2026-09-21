// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import * as React from 'react';
import {act, render, screen} from '@testing-library/react';
import {useWorkspaceLayout} from './useWorkspaceLayout';
import {resetWorkspaceForTests} from './store';

type Listener = () => void;

function stubMatchMedia(width: number) {
    const listeners = new Set<Listener>();
    const mql = (query: string) => {
        const min = /min-width:\s*(\d+)px/.exec(query);
        const matches = min ? width >= Number(min[1]) : false;
        return {
            matches,
            media: query,
            addEventListener: (_: string, l: Listener) => listeners.add(l),
            removeEventListener: (_: string, l: Listener) => listeners.delete(l),
            addListener: (l: Listener) => listeners.add(l),
            removeListener: (l: Listener) => listeners.delete(l),
        } as unknown as MediaQueryList;
    };
    Object.defineProperty(window, 'matchMedia', {
        value: jest.fn(mql),
        writable: true,
        configurable: true,
    });
    return {
        resize(next: number) {
            width = next;
            listeners.forEach((l) => l());
        },
    };
}

const Probe: React.FC = () => {
    const mode = useWorkspaceLayout();
    return <div data-testid="mode">{mode}</div>;
};

beforeEach(() => {
    resetWorkspaceForTests();
});

describe('useWorkspaceLayout', () => {
    test.each([
        [320, 'sheet'],
        [375, 'sheet'],
        [390, 'sheet'],
        [767, 'sheet'],
        [768, 'drawer'],
        [1024, 'drawer'],
        [1279, 'drawer'],
        [1280, 'docked'],
        [1440, 'docked'],
    ])('width %ip → %s', (width, expected) => {
        stubMatchMedia(width);
        render(<Probe/>);
        expect(screen.getByTestId('mode')).toHaveTextContent(expected);
    });

    test('a matchMedia change re-renders with the new mode', () => {
        const stub = stubMatchMedia(375);
        render(<Probe/>);
        expect(screen.getByTestId('mode')).toHaveTextContent('sheet');
        act(() => stub.resize(1280));
        expect(screen.getByTestId('mode')).toHaveTextContent('docked');
    });

    test('legacy addListener MediaQueryList API is used when addEventListener is absent', () => {
        const stub = stubMatchMedia(375);
        // Strip the modern API so the hook takes the legacy branch.
        Object.defineProperty(window, 'matchMedia', {
            value: (query: string) => {
                const mql = (window.matchMedia as unknown as jest.Mock)(query);
                return mql;
            },
            writable: true,
            configurable: true,
        });
        const widthRef = {width: 375};
        const listeners = new Set<() => void>();
        Object.defineProperty(window, 'matchMedia', {
            value: (query: string) => {
                const min = /min-width:\s*(\d+)px/.exec(query);
                const matches = min ? widthRef.width >= Number(min[1]) : false;
                return {
                    matches,
                    media: query,
                    addListener: (l: () => void) => listeners.add(l),
                    removeListener: (l: () => void) => listeners.delete(l),
                } as unknown as MediaQueryList;
            },
            writable: true,
            configurable: true,
        });
        render(<Probe/>);
        expect(screen.getByTestId('mode')).toHaveTextContent('sheet');
        act(() => {
            widthRef.width = 1280;
            listeners.forEach((l) => l());
        });
        expect(screen.getByTestId('mode')).toHaveTextContent('docked');
        void stub;
    });

    test('absent matchMedia falls back to sheet without throwing', () => {
        Object.defineProperty(window, 'matchMedia', {
            value: undefined,
            writable: true,
            configurable: true,
        });
        render(<Probe/>);
        expect(screen.getByTestId('mode')).toHaveTextContent('sheet');
    });
});
