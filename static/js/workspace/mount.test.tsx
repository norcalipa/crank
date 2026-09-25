// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {waitFor} from '@testing-library/react';
import {getWorkspaceSnapshot, resetWorkspaceForTests} from './store';

describe('surfaceFromPath', () => {
    test.each([
        ['/chat/', 'chat'],
        ['/help/', 'help'],
        ['/privacy/', 'help'],
        ['/organization/7/', 'company'],
        ['/', 'rankings'],
        ['/algo/3/', 'rankings'],
    ])('%s → %s', (pathname, expected) => {
        jest.isolateModules(() => {
            // eslint-disable-next-line @typescript-eslint/no-var-requires
            const {surfaceFromPath} = require('./mount');
            expect(surfaceFromPath(pathname)).toBe(expected);
        });
    });
});

describe('workspace mount', () => {
    beforeEach(() => {
        resetWorkspaceForTests();
        document.body.innerHTML = '';
        jest.resetModules();
    });

    test('host present → mounts the shell', async () => {
        const host = document.createElement('div');
        host.id = 'assistant-workspace';
        document.body.appendChild(host);
        jest.isolateModules(() => {
            require('./mount');
        });
        document.dispatchEvent(new Event('DOMContentLoaded'));
        await waitFor(() => expect(host.querySelector('[data-testid="assistant-launcher"]')).toBeTruthy());
    });

    test('a pinned host opens the assistant on load', async () => {
        const host = document.createElement('div');
        host.id = 'assistant-workspace';
        host.dataset.assistantPinned = 'true';
        document.body.appendChild(host);
        jest.isolateModules(() => {
            require('./mount');
        });
        document.dispatchEvent(new Event('DOMContentLoaded'));
        await waitFor(() => expect(host.querySelector('[data-testid="assistant-panel"]')).toBeTruthy());
    });

    test('host absent → no-op, no throw', () => {
        const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => undefined);
        jest.isolateModules(() => {
            require('./mount');
        });
        expect(() => document.dispatchEvent(new Event('DOMContentLoaded'))).not.toThrow();
        expect(document.body.querySelector('[data-testid="assistant-launcher"]')).toBeNull();
        expect(errorSpy).not.toHaveBeenCalled();
        errorSpy.mockRestore();
    });

    function mountPinned(dataset: Record<string, string>): HTMLElement {
        const host = document.createElement('div');
        host.id = 'assistant-workspace';
        host.dataset.assistantPinned = 'true';
        Object.assign(host.dataset, dataset);
        document.body.appendChild(host);
        jest.isolateModules(() => {
            require('./mount');
        });
        document.dispatchEvent(new Event('DOMContentLoaded'));
        return host;
    }

    test('seeds the company context and account from the pinned host before opening', () => {
        window.history.replaceState({}, '', '/chat/?company=12');
        mountPinned({authenticated: 'true', accountKey: 'alice', selectedCompanyId: '12'});
        const snapshot = getWorkspaceSnapshot();
        expect(snapshot.context).toMatchObject({surface: 'chat', organizationId: 12});
        expect(snapshot.account).toEqual({status: 'authenticated', key: 'alice'});
        expect(snapshot.visibility).toBe('open');
        window.history.replaceState({}, '', '/');
    });

    test('ignores an invalid selected company id and seeds an anonymous account', () => {
        mountPinned({authenticated: 'false', selectedCompanyId: 'abc'});
        const snapshot = getWorkspaceSnapshot();
        expect(snapshot.context?.organizationId).toBeUndefined();
        expect(snapshot.account).toEqual({status: 'anonymous', key: ''});
    });

    test('a non-pinned host leaves the account unknown until hydration', () => {
        const host = document.createElement('div');
        host.id = 'assistant-workspace';
        document.body.appendChild(host);
        jest.isolateModules(() => {
            require('./mount');
        });
        document.dispatchEvent(new Event('DOMContentLoaded'));
        expect(getWorkspaceSnapshot().account.status).toBe('unknown');
        document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail: {authenticated: true, username: 'alice'}}));
        expect(getWorkspaceSnapshot().account).toEqual({status: 'authenticated', key: 'alice'});
    });
});
