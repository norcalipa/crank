// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {waitFor} from '@testing-library/react';
import {resetWorkspaceForTests} from './store';

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
});
