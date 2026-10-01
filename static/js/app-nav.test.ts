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
    const storageListeners: EventListener[] = [];
    const pageshowListeners: EventListener[] = [];
    const realAddEventListener = window.addEventListener.bind(window);

    beforeEach(() => {
        // Each fresh require() registers another window `storage` listener;
        // track them so one test's module never handles the next test's event.
        jest.spyOn(window, 'addEventListener').mockImplementation(
            (type: string, listener: EventListenerOrEventListenerObject, options?: boolean | AddEventListenerOptions) => {
                if (type === 'storage') {
                    storageListeners.push(listener as EventListener);
                }
                if (type === 'pageshow') {
                    pageshowListeners.push(listener as EventListener);
                }
                realAddEventListener(type, listener, options);
            },
        );
        document.body.innerHTML = '';
        window.localStorage.clear();
        window.sessionStorage.clear();
        global.fetch = jest.fn().mockResolvedValue({ok: false});
    });

    afterEach(() => {
        storageListeners.splice(0).forEach((listener) => window.removeEventListener('storage', listener));
        pageshowListeners.splice(0).forEach((listener) => window.removeEventListener('pageshow', listener));
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

        expect(received).toEqual({authenticated: true, username: 'alice', unobserved: false});
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

        expect(received).toEqual({authenticated: false, username: null, unobserved: true});
    });

    test('signing out purges every crank:jobsearch:* key, the auth intent, and crank:last-account before the request', async () => {
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'secret draft');
        window.localStorage.setItem('crank:jobsearch:inflight:1:a', '{}');
        window.localStorage.setItem('crank:last-account', 'alice');
        window.localStorage.setItem('unrelated-key', 'keep-me');
        window.sessionStorage.setItem('crank:auth-intent', '{"route":"/chat/"}');
        window.sessionStorage.setItem('crank:workspace:v1', '{"v":1}');

        // Mirrors the cached auth-neutral shell's form in
        // templates/_navigation.html: every logout form carries
        // data-nav-logout-form; only the cached shell's also carries
        // data-nav-js-logout.
        document.body.innerHTML = '<form data-nav-logout-form data-nav-js-logout action="/accounts/logout/">'
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
        expect(window.sessionStorage.getItem('crank:workspace:v1')).toBeNull();
        expect(window.localStorage.getItem('unrelated-key')).toBe('keep-me');
    });

    test('a server-rendered logout form purges without hijacking its native CSRF POST', async () => {
        // The regression this covers: purge handling used to bind only to
        // form[data-nav-js-logout], which _navigation.html emits for the
        // cached auth-neutral shell alone. An authenticated, server-rendered
        // page such as /chat/ carries its own {% csrf_token %} and posts
        // natively, so its logout left every private artefact in storage and
        // never told the mounted chat to abort (issue #465 AC-9).
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'secret draft');
        window.localStorage.setItem('crank:jobsearch:draft:7', 'per-conversation draft');
        window.localStorage.setItem('crank:last-account', 'alice');
        window.localStorage.setItem('unrelated-key', 'keep-me');
        window.sessionStorage.setItem('crank:auth-intent', '{"route":"/chat/"}');

        // No data-nav-js-logout: exactly what an authenticated /chat/ renders.
        document.body.innerHTML = '<form data-nav-logout-form method="post" action="/accounts/logout/">'
            + '<input type="hidden" name="csrfmiddlewaretoken" value="tok">'
            + '<button type="submit">Logout</button></form>';

        const purgeListener = jest.fn();
        document.addEventListener('crank:private-state-purged', purgeListener);

        jest.isolateModules(() => {
            require('./app-nav.js');
        });
        await flushMicrotasks();
        (global.fetch as jest.Mock).mockClear();

        const form = document.querySelector('form[data-nav-logout-form]') as HTMLFormElement;
        const submitEvent = new Event('submit', {bubbles: true, cancelable: true});
        form.dispatchEvent(submitEvent);

        expect(purgeListener).toHaveBeenCalledTimes(1);
        expect(window.localStorage.getItem('crank:jobsearch:draft:pending')).toBeNull();
        expect(window.localStorage.getItem('crank:jobsearch:draft:7')).toBeNull();
        expect(window.localStorage.getItem('crank:last-account')).toBeNull();
        expect(window.sessionStorage.getItem('crank:auth-intent')).toBeNull();
        expect(window.localStorage.getItem('unrelated-key')).toBe('keep-me');
        // The native POST must still happen: not prevented, and not replaced
        // by the cached shell's token-less fetch.
        expect(submitEvent.defaultPrevented).toBe(false);
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('purgePrivateClientState tolerates a broken localStorage without throwing', async () => {
        document.body.innerHTML = '<form data-nav-logout-form data-nav-js-logout action="/accounts/logout/">'
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

    function whoamiAs(getUser: () => string | null): void {
        (global.fetch as jest.Mock).mockImplementation((url: string) => {
            if (String(url).includes('/api/account/whoami/')) {
                const user = getUser();
                return Promise.resolve({
                    ok: true,
                    json: () => Promise.resolve(user ? {authenticated: true, username: user} : {authenticated: false}),
                });
            }
            return Promise.resolve({ok: true});
        });
    }

    function loadAppNav(): void {
        jest.isolateModules(() => {
            require('./app-nav.js');
        });
    }

    test('submitting logout does not announce before the server has processed it', async () => {
        whoamiAs(() => 'alice');
        document.body.innerHTML = '<form data-nav-logout-form method="post" action="/logout/"></form>';
        loadAppNav();
        await flushMicrotasks();
        const form = document.querySelector('form') as HTMLFormElement;
        form.dispatchEvent(new Event('submit', {cancelable: true, bubbles: true}));
        await flushMicrotasks();
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();
    });

    test('a delayed logout POST sends no signal, and the signal follows the anonymous hydration after it', async () => {
        let user: string | null = 'alice';
        whoamiAs(() => user);
        let releaseLogout: () => void = () => undefined;
        const base = (global.fetch as jest.Mock).getMockImplementation() as (url: string) => Promise<unknown>;
        (global.fetch as jest.Mock).mockImplementation((url: string, init?: {method?: string}) => {
            if (init?.method === 'POST') {
                return new Promise((resolve) => {
                    releaseLogout = () => { user = null; resolve({ok: true}); };
                });
            }
            return base(url);
        });
        Object.defineProperty(window, 'location', {value: {...window.location, href: '/x/'}, writable: true});
        document.body.innerHTML = '<form data-nav-logout-form data-nav-js-logout action="/accounts/logout/"></form>';
        loadAppNav();
        await flushMicrotasks();
        expect(window.sessionStorage.getItem('crank:nav-account-seen')).toMatch(/^u:[0-9a-f]{16}$/);
        document.querySelector('form')!.dispatchEvent(new Event('submit', {cancelable: true, bubbles: true}));
        await flushMicrotasks();
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();
        releaseLogout();
        await flushMicrotasks();
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();
        // The next page load's hydration observes the completed logout.
        jest.resetModules();
        loadAppNav();
        await flushMicrotasks();
        const nonce = window.localStorage.getItem('crank:account-epoch');
        expect(nonce).toBeTruthy();
        expect(nonce).not.toContain('alice');
        expect(nonce).not.toMatch(/^\d+:/);
        expect(window.sessionStorage.getItem('crank:nav-account-seen')).toBe('anon');
    });

    test('signing in as another account announces once the hydration observes it', async () => {
        let user: string | null = 'alice';
        whoamiAs(() => user);
        loadAppNav();
        await flushMicrotasks();
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();
        user = 'bob';
        jest.resetModules();
        loadAppNav();
        await flushMicrotasks();
        const nonce = window.localStorage.getItem('crank:account-epoch');
        expect(nonce).toBeTruthy();
        expect(nonce).not.toMatch(/alice|bob/);
    });

    test('anonymous to signed-in (login) announces; an unchanged account does not', async () => {
        let user: string | null = null;
        whoamiAs(() => user);
        loadAppNav();
        await flushMicrotasks();
        jest.resetModules();
        loadAppNav();
        await flushMicrotasks();
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();
        user = 'bob';
        jest.resetModules();
        loadAppNav();
        await flushMicrotasks();
        expect(window.localStorage.getItem('crank:account-epoch')).toBeTruthy();
    });

    test('a failed whoami is not an account change', async () => {
        whoamiAs(() => 'alice');
        loadAppNav();
        await flushMicrotasks();
        (global.fetch as jest.Mock).mockRejectedValue(new Error('offline'));
        jest.resetModules();
        loadAppNav();
        await flushMicrotasks();
        (global.fetch as jest.Mock).mockResolvedValue({ok: false});
        jest.resetModules();
        loadAppNav();
        await flushMicrotasks();
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();
        expect(window.sessionStorage.getItem('crank:nav-account-seen')).toMatch(/^u:[0-9a-f]{16}$/);
    });

    test('a broken sessionStorage does not break hydration or announce', async () => {
        whoamiAs(() => 'alice');
        const original = window.sessionStorage;
        Object.defineProperty(window, 'sessionStorage', {
            value: {getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); }, removeItem: () => undefined},
            configurable: true,
        });
        try {
            loadAppNav();
            await flushMicrotasks();
            expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();
        } finally {
            Object.defineProperty(window, 'sessionStorage', {value: original, configurable: true});
        }
    });

    test('a broken localStorage does not stop the announcement path', async () => {
        let user: string | null = 'alice';
        whoamiAs(() => user);
        loadAppNav();
        await flushMicrotasks();
        user = 'bob';
        const spy = jest.spyOn(Storage.prototype, 'setItem').mockImplementation((key: string) => {
            if (key === 'crank:account-epoch') throw new Error('quota');
        });
        jest.resetModules();
        expect(() => loadAppNav()).not.toThrow();
        await flushMicrotasks();
        spy.mockRestore();
    });

    test('another tab changing the account resets this tab own state, keeps shared drafts, and re-hydrates', async () => {
        (global.fetch as jest.Mock).mockImplementation((url: string) => {
            if (String(url).includes('/api/account/whoami/')) {
                return Promise.resolve({
                    ok: true,
                    json: () => Promise.resolve({authenticated: true, username: 'bob'}),
                });
            }
            return Promise.resolve({ok: true});
        });
        jest.isolateModules(() => {
            require('./app-nav.js');
        });
        await flushMicrotasks();
        window.sessionStorage.setItem('crank:workspace:v1', '{"v":1}');
        window.sessionStorage.setItem('crank:auth-intent', '{}');
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'alice draft');
        const purged = jest.fn();
        const hydrated = jest.fn();
        document.addEventListener('crank:private-state-purged', purged);
        document.addEventListener('crank:auth-hydrated', ((e: CustomEvent) => hydrated(e.detail)) as EventListener);

        window.dispatchEvent(new StorageEvent('storage', {key: 'crank:account-epoch', newValue: 'n1'}));
        await flushMicrotasks();

        expect(purged).toHaveBeenCalledTimes(1);
        expect(window.sessionStorage.getItem('crank:workspace:v1')).toBeNull();
        expect(window.sessionStorage.getItem('crank:auth-intent')).toBeNull();
        // Shared localStorage belongs to the tab that changed the account:
        // the #465 sign-in handoff draft must survive another tab's login.
        expect(window.localStorage.getItem('crank:jobsearch:draft:pending')).toBe('alice draft');
        expect(hydrated).toHaveBeenCalledWith({authenticated: true, username: 'bob', unobserved: false});
        // The receiving tab never re-announces, even though its hydrated
        // account differs from the one it last saw.
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();
        expect(window.sessionStorage.getItem('crank:nav-account-seen')).toMatch(/^u:[0-9a-f]{16}$/);
        document.removeEventListener('crank:private-state-purged', purged);
    });

    test('the seen account is a digest, never the username, and a legacy raw value migrates without a purge', async () => {
        whoamiAs(() => 'alice');
        loadAppNav();
        await flushMicrotasks();
        const digest = window.sessionStorage.getItem('crank:nav-account-seen');
        expect(digest).toMatch(/^u:[0-9a-f]{16}$/);
        expect(digest).not.toContain('alice');

        window.sessionStorage.setItem('crank:nav-account-seen', 'u:alice');
        jest.resetModules();
        loadAppNav();
        await flushMicrotasks();
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();
        expect(window.sessionStorage.getItem('crank:nav-account-seen')).toBe(digest);

        // The migrated value is stable: another load announces nothing.
        jest.resetModules();
        loadAppNav();
        await flushMicrotasks();
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();

        // A legacy raw value for a different account is a real change.
        window.sessionStorage.setItem('crank:nav-account-seen', 'u:carol');
        jest.resetModules();
        loadAppNav();
        await flushMicrotasks();
        expect(window.localStorage.getItem('crank:account-epoch')).toBeTruthy();
    });

    test('the account epoch falls back to a random nonce without crypto.randomUUID', async () => {
        let user: string | null = 'alice';
        whoamiAs(() => user);
        loadAppNav();
        await flushMicrotasks();
        user = 'bob';
        const original = window.crypto.randomUUID;
        Object.defineProperty(window.crypto, 'randomUUID', {value: undefined, configurable: true});
        try {
            jest.resetModules();
            loadAppNav();
            await flushMicrotasks();
        } finally {
            Object.defineProperty(window.crypto, 'randomUUID', {value: original, configurable: true});
        }
        expect(window.localStorage.getItem('crank:account-epoch')).toMatch(/^[0-9a-z]{4,}$/);
    });

    function pageshow(persisted: boolean): void {
        const event = new Event('pageshow') as Event & {persisted: boolean};
        Object.defineProperty(event, 'persisted', {value: persisted});
        window.dispatchEvent(event);
    }

    test('a bfcache restore (pageshow.persisted) of a stale account purges tab state and re-hydrates', async () => {
        let user: string | null = 'alice';
        whoamiAs(() => user);
        loadAppNav();
        await flushMicrotasks();
        const whoamiCalls = () => (global.fetch as jest.Mock).mock.calls.filter(([u]) => String(u).includes('whoami')).length;
        const before = whoamiCalls();
        const purged = jest.fn();
        const hydrated = jest.fn();
        document.addEventListener('crank:private-state-purged', purged);
        document.addEventListener('crank:auth-hydrated', ((e: CustomEvent) => hydrated(e.detail)) as EventListener);
        window.sessionStorage.setItem('crank:workspace:v1', '{"v":1}');

        // A non-persisted pageshow (ordinary load) does nothing.
        pageshow(false);
        await flushMicrotasks();
        expect(whoamiCalls()).toBe(before);

        // Same account: re-check only, no purge.
        pageshow(true);
        await flushMicrotasks();
        expect(whoamiCalls()).toBe(before + 1);
        expect(purged).not.toHaveBeenCalled();

        // Another document of this tab observed a different account.
        window.sessionStorage.setItem('crank:nav-account-seen', 'anon');
        user = 'bob';
        pageshow(true);
        await flushMicrotasks();
        expect(purged).toHaveBeenCalledTimes(1);
        expect(window.sessionStorage.getItem('crank:workspace:v1')).toBeNull();
        expect(whoamiCalls()).toBe(before + 2);
        expect(hydrated).toHaveBeenLastCalledWith({authenticated: true, username: 'bob', unobserved: false});
        // The receiving document is quiet: it never re-announces.
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();

        document.removeEventListener('crank:private-state-purged', purged);
    });

    test('a pageshow restore with unreadable sessionStorage still re-checks without throwing', async () => {
        whoamiAs(() => null);
        loadAppNav();
        await flushMicrotasks();
        jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
        expect(() => pageshow(true)).not.toThrow();
        await flushMicrotasks();
    });

    test('storage events for unrelated keys are ignored', async () => {
        jest.isolateModules(() => {
            require('./app-nav.js');
        });
        await flushMicrotasks();
        const purged = jest.fn();
        document.addEventListener('crank:private-state-purged', purged);
        window.dispatchEvent(new StorageEvent('storage', {key: 'something-else', newValue: 'x'}));
        expect(purged).not.toHaveBeenCalled();
        document.removeEventListener('crank:private-state-purged', purged);
    });
});
