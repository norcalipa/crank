// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {clearIntent, purgePrivateClientState, readIntent, writeIntent} from './authIntent';

describe('authIntent', () => {
    beforeEach(() => {
        window.localStorage.clear();
        window.sessionStorage.clear();
    });

    test('writeIntent/readIntent round-trip', () => {
        writeIntent({route: '/chat/', companyId: 7});
        expect(readIntent()).toEqual({route: '/chat/', companyId: 7});
    });

    test('readIntent returns null when nothing is stored', () => {
        expect(readIntent()).toBeNull();
    });

    test('readIntent returns null for corrupt JSON without throwing', () => {
        window.sessionStorage.setItem('crank:auth-intent', '{not json');
        expect(() => readIntent()).not.toThrow();
        expect(readIntent()).toBeNull();
    });

    test('readIntent returns null when the stored value has no route', () => {
        window.sessionStorage.setItem('crank:auth-intent', JSON.stringify({companyId: 3}));
        expect(readIntent()).toBeNull();
    });

    test('readIntent normalizes a non-numeric companyId to null', () => {
        window.sessionStorage.setItem('crank:auth-intent', JSON.stringify({route: '/', companyId: 'x'}));
        expect(readIntent()).toEqual({route: '/', companyId: null});
    });

    test('clearIntent removes only the intent key', () => {
        writeIntent({route: '/chat/', companyId: null});
        window.sessionStorage.setItem('unrelated-key', 'keep-me');
        clearIntent();
        expect(readIntent()).toBeNull();
        expect(window.sessionStorage.getItem('unrelated-key')).toBe('keep-me');
    });

    test('purgePrivateClientState removes every crank:jobsearch:* key and the intent, leaving unrelated keys', () => {
        writeIntent({route: '/chat/', companyId: 1});
        window.localStorage.setItem('crank:jobsearch:draft:pending', 'unsent text');
        window.localStorage.setItem('crank:jobsearch:draft:42', 'other draft');
        window.localStorage.setItem('crank:jobsearch:inflight:42:abc', '{}');
        window.localStorage.setItem('crank:last-account', 'someone');
        window.localStorage.setItem('unrelated-key', 'keep-me');

        purgePrivateClientState();

        expect(readIntent()).toBeNull();
        expect(window.localStorage.getItem('crank:jobsearch:draft:pending')).toBeNull();
        expect(window.localStorage.getItem('crank:jobsearch:draft:42')).toBeNull();
        expect(window.localStorage.getItem('crank:jobsearch:inflight:42:abc')).toBeNull();
        // crank:last-account is a distinct key outside the crank:jobsearch:
        // prefix; the purge helper never touches it (it is cleared
        // explicitly by the sign-out call site, not by this prefix sweep).
        expect(window.localStorage.getItem('crank:last-account')).toBe('someone');
        expect(window.localStorage.getItem('unrelated-key')).toBe('keep-me');
    });

    describe('storage-unavailable tolerance', () => {
        function breakStorage(storage: 'localStorage' | 'sessionStorage', method: 'getItem' | 'setItem' | 'removeItem') {
            const original = window[storage];
            const failing: Record<string, unknown> = {
                getItem: original.getItem.bind(original),
                setItem: original.setItem.bind(original),
                removeItem: original.removeItem.bind(original),
                clear: original.clear.bind(original),
                key: original.key.bind(original),
                get length() { return original.length; },
            };
            failing[method] = () => { throw new Error('storage unavailable'); };
            Object.defineProperty(window, storage, {value: failing, configurable: true});
            return () => {
                Object.defineProperty(window, storage, {value: original, configurable: true});
            };
        }

        test('writeIntent swallows a sessionStorage write failure', () => {
            const restore = breakStorage('sessionStorage', 'setItem');
            try {
                expect(() => writeIntent({route: '/', companyId: null})).not.toThrow();
            } finally {
                restore();
            }
        });

        test('readIntent swallows a sessionStorage read failure', () => {
            const restore = breakStorage('sessionStorage', 'getItem');
            try {
                expect(readIntent()).toBeNull();
            } finally {
                restore();
            }
        });

        test('clearIntent swallows a sessionStorage removal failure', () => {
            const restore = breakStorage('sessionStorage', 'removeItem');
            try {
                expect(() => clearIntent()).not.toThrow();
            } finally {
                restore();
            }
        });

        test('purgePrivateClientState swallows a localStorage failure', () => {
            const restore = breakStorage('localStorage', 'getItem');
            try {
                expect(() => purgePrivateClientState()).not.toThrow();
            } finally {
                restore();
            }
        });
    });
});
