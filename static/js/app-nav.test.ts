// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
/**
 * app-nav.js is a standalone script (not a webpack entry, see
 * webpack.config.js) loaded directly by templates/base.html on every page,
 * so it cannot import the TS modules under test elsewhere in this
 * directory. These tests `require()` it fresh per test via
 * jest.isolateModules so its module-level whoami hydration (which fires
 * immediately on load) can be observed under a controlled fetch mock.
 */

function flushMicrotasks(times = 4): Promise<void> {
    let chain: Promise<unknown> = Promise.resolve();
    for (let i = 0; i < times; i++) {
        chain = chain.then(() => Promise.resolve());
    }
    return chain as Promise<void>;
}

describe('app-nav (issue #465 private-state purge)', () => {
    beforeEach(() => {
        document.body.innerHTML = '';
        window.localStorage.clear();
        window.sessionStorage.clear();
        global.fetch = jest.fn().mockResolvedValue({ok: false});
    });

    afterEach(() => {
        jest.resetModules();
        jest.restoreAllMocks();
    });

    test('applyAuthState dispatches crank:auth-hydrated with the hydrated username in detail', async () => {
        (global.fetch as jest.Mock).mockImplementation((url: string) => {
            if (String(url).includes('/api/account/whoami/')) {
                return Promise.resolve({
                    ok: true,
                    json: () => Promise.resolve({authenticated: true, username: 'alice'}),
                });
            }
            return Promise.resolve({ok: true});
        });
        let received: {authenticated?: boolean; username?: string | null} | null = null;
        document.addEventListener('crank:auth-hydrated', ((e: CustomEvent) => {
            received = e.detail;
        }) as EventListener);

        jest.isolateModules(() => {
            require('./app-nav.js');
        });
        await flushMicrotasks();

        expect(received).toEqual({authenticated: true, username: 'alice'});
    });

    test('applyAuthState dispatches a null username when anonymous', async () => {
        (global.fetch as jest.Mock).mockResolvedValue({ok: false});
        let received: {authenticated?: boolean; username?: string | null} | null = null;
        document.addEventListener('crank:auth-hydrated', ((e: CustomEvent) => {
            received = e.detail;
        }) as EventListener);

        jest.isolateModules(() => {
            require('./app-nav.js');
        });
        await flushMicrotasks();

        expect(received).toEqual({authenticated: false, username: null});
    });

    test('signing out purges every crank:jobsearch:* key, the auth intent, and crank:last-account before the request', async () => {
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'secret draft');
        window.localStorage.setItem('crank:jobsearch:inflight:1:a', '{}');
        window.localStorage.setItem('crank:last-account', 'alice');
        window.localStorage.setItem('unrelated-key', 'keep-me');
        window.sessionStorage.setItem('crank:auth-intent', '{"route":"/chat/"}');

        document.body.innerHTML = '<form data-nav-js-logout action="/accounts/logout/">'
            + '<button type="submit">Logout</button></form>';

        const purgeListener = jest.fn();
        document.addEventListener('crank:private-state-purged', purgeListener);

        // window.location.href assignment is not implemented in jsdom
        // navigation; stub it so the redirect in the fetch .then()/.catch()
        // does not print a noisy (non-fatal) jsdom warning.
        const originalHref = window.location.href;
        Object.defineProperty(window, 'location', {
            value: {...window.location, href: originalHref},
            writable: true,
        });

        jest.isolateModules(() => {
            require('./app-nav.js');
        });
        await flushMicrotasks();

        const form = document.querySelector('form[data-nav-js-logout]') as HTMLFormElement;
        const submitEvent = new Event('submit', {bubbles: true, cancelable: true});
        form.dispatchEvent(submitEvent);

        expect(purgeListener).toHaveBeenCalledTimes(1);
        expect(window.localStorage.getItem('crank:jobsearch:draft:pending')).toBeNull();
        expect(window.localStorage.getItem('crank:jobsearch:inflight:1:a')).toBeNull();
        expect(window.localStorage.getItem('crank:last-account')).toBeNull();
        expect(window.sessionStorage.getItem('crank:auth-intent')).toBeNull();
        expect(window.localStorage.getItem('unrelated-key')).toBe('keep-me');
    });

    test('purgePrivateClientState tolerates a broken localStorage without throwing', async () => {
        document.body.innerHTML = '<form data-nav-js-logout action="/accounts/logout/">'
            + '<button type="submit">Logout</button></form>';

        jest.isolateModules(() => {
            require('./app-nav.js');
        });
        await flushMicrotasks();

        const original = window.localStorage;
        const failing: Storage = {
            ...original,
            get length() { return original.length; },
            key: original.key.bind(original),
            getItem: () => { throw new Error('storage unavailable'); },
            removeItem: () => { throw new Error('storage unavailable'); },
        };
        Object.defineProperty(window, 'localStorage', {value: failing, configurable: true});
        try {
            const form = document.querySelector('form[data-nav-js-logout]') as HTMLFormElement;
            const submitEvent = new Event('submit', {bubbles: true, cancelable: true});
            expect(() => form.dispatchEvent(submitEvent)).not.toThrow();
        } finally {
            Object.defineProperty(window, 'localStorage', {value: original, configurable: true});
        }
    });
});
