// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Pins the privacy-critical account gate in isolation (issues #465, #479, #527).
import '@testing-library/jest-dom';
import {render, screen, act} from '@testing-library/react';
import * as React from 'react';

import {LAST_ACCOUNT_KEY} from './storage';
import {useAccountGate} from './useAccountGate';

const onPurge = jest.fn();

function Probe({accountKey = '', isAuthenticated = true}: {accountKey?: string; isAuthenticated?: boolean}) {
    const gate = useAccountGate({isAuthenticated, accountKey, workspaceMode: undefined, onPurge: () => onPurge()});
    return <span data-testid="auth">{String(gate.effectiveAuthenticated)}</span>;
}

const hydrate = (detail: Record<string, unknown>) => act(() => {
    document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail}));
});

describe('useAccountGate', () => {
    beforeEach(() => {
        onPurge.mockClear();
        window.localStorage.clear();
    });

    test('reconciles the trusted account key synchronously on first render, purging the previous account\'s drafts', () => {
        window.localStorage.setItem(LAST_ACCOUNT_KEY, 'u:previous');
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'previous account text');
        render(<Probe accountKey="u:current"/>);
        expect(window.localStorage.getItem('crank:jobsearch:draft:pending')).toBeNull();
        expect(window.localStorage.getItem(LAST_ACCOUNT_KEY)).not.toBe('u:previous');
    });

    test('crank:private-state-purged discards in-memory state', () => {
        render(<Probe accountKey="u:current"/>);
        act(() => { document.dispatchEvent(new CustomEvent('crank:private-state-purged')); });
        expect(onPurge).toHaveBeenCalledTimes(1);
    });

    test('an unobserved (failed whoami) hydration changes nothing: no auth flip, no purge (issue #527)', () => {
        render(<Probe accountKey="u:current"/>);
        hydrate({authenticated: false, username: null, unobserved: true});
        expect(screen.getByTestId('auth')).toHaveTextContent('true');
        expect(onPurge).not.toHaveBeenCalled();
    });

    test('an observed sign-out hydration flips the effective auth state without purging by itself', () => {
        render(<Probe accountKey="u:current"/>);
        hydrate({authenticated: false, username: null});
        expect(screen.getByTestId('auth')).toHaveTextContent('false');
        expect(onPurge).not.toHaveBeenCalled();
    });

    test('hydration for a different account purges; the same account does not', () => {
        render(<Probe accountKey="u:current"/>);
        hydrate({authenticated: true, username: 'u:current'});
        expect(onPurge).not.toHaveBeenCalled();
        hydrate({authenticated: true, username: 'u:other'});
        expect(onPurge).toHaveBeenCalledTimes(1);
    });
});
