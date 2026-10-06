// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {installWorkspacePersistence} from './persistence';
import {
    getWorkspaceSnapshot,
    openAssistant,
    replaceWorkspaceState,
    resetWorkspaceForTests,
    setPrioritiesRevision,
    setWorkspaceAccount,
    setWorkspaceContext,
} from './store';
import {WORKSPACE_SESSION_KEY} from './types';

let teardown: (() => void) | undefined;

function hydrate(detail: {authenticated: boolean; username: string | null; unobserved?: boolean}): void {
    document.dispatchEvent(new CustomEvent('crank:auth-hydrated', {detail}));
}

function record(): any {
    const raw = window.sessionStorage.getItem(WORKSPACE_SESSION_KEY);
    return raw ? JSON.parse(raw) : null;
}

function seedRecord(overrides: Record<string, unknown> = {}): void {
    window.sessionStorage.setItem(WORKSPACE_SESSION_KEY, JSON.stringify({
        v: 1,
        account: {status: 'authenticated', key: 'alice'},
        visibility: 'open',
        context: {organizationId: 7, organizationName: 'Acme', searchTerm: 'leak'},
        savedAt: 1,
        ...overrides,
    }));
}

beforeEach(() => {
    resetWorkspaceForTests();
    window.sessionStorage.clear();
    setWorkspaceContext({surface: 'rankings'});
});

afterEach(() => {
    teardown?.();
    teardown = undefined;
    jest.restoreAllMocks();
});

describe('workspace persistence', () => {
    test('does not restore while the account is unknown, then restores after hydration', () => {
        seedRecord();
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'rankings'});
        hydrate({authenticated: true, username: 'alice'});
        const snapshot = getWorkspaceSnapshot();
        expect(snapshot.context).toEqual({surface: 'rankings', organizationId: 7, organizationName: 'Acme'});
        expect(snapshot.visibility).toBe('minimized');
    });

    test('restores immediately when the account is already known and matches', () => {
        seedRecord({visibility: 'minimized'});
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().visibility).toBe('minimized');
        expect(getWorkspaceSnapshot().context?.organizationId).toBe(7);
    });

    test('open restores as open in docked mode', () => {
        window.matchMedia = ((query: string) => ({
            matches: query === '(min-width: 1280px)' || query === '(min-width: 768px)',
        })) as unknown as typeof window.matchMedia;
        seedRecord();
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().visibility).toBe('open');
        delete (window as any).matchMedia;
    });

    test('a pinned page that already opened the panel is never downgraded', () => {
        seedRecord({visibility: 'minimized'});
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        openAssistant();
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().visibility).toBe('open');
    });

    test('a record with nothing to restore leaves the store alone', () => {
        resetWorkspaceForTests();
        seedRecord({context: {}, visibility: 'closed'});
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().context).toBeNull();
    });

    test('account mismatch deletes the record and restores nothing', () => {
        seedRecord();
        teardown = installWorkspacePersistence();
        hydrate({authenticated: true, username: 'bob'});
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'rankings'});
        expect(getWorkspaceSnapshot().visibility).toBe('closed');
        expect(record()?.account.key).not.toBe('alice');
    });

    test('the persisted record stores a digest, restores for the same account, and drops for another', () => {
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        openAssistant({surface: 'company', organizationId: 3, organizationName: 'Zed'});
        const key = record()?.account.key;
        expect(key).toMatch(/^d:[0-9a-f]{16}$/);
        expect(key).not.toContain('alice');
        teardown();
        teardown = undefined;
        resetWorkspaceForTests();
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().context?.organizationId).toBe(3);
        teardown();
        resetWorkspaceForTests();
        setWorkspaceAccount({status: 'authenticated', key: 'bob'});
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().context?.organizationId).toBeUndefined();
    });

    test('anonymous hydration against an authenticated record deletes it', () => {
        seedRecord();
        teardown = installWorkspacePersistence();
        hydrate({authenticated: false, username: null});
        expect(record()?.account?.status).not.toBe('authenticated');
        expect(getWorkspaceSnapshot().context?.organizationId).toBeUndefined();
    });

    test('an account switch while running resets the store', () => {
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        openAssistant({surface: 'company', organizationId: 3, organizationName: 'Zed'});
        hydrate({authenticated: true, username: 'bob'});
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'company'});
        expect(getWorkspaceSnapshot().visibility).toBe('closed');
        expect(getWorkspaceSnapshot().account.key).toBe('bob');
        expect(record()?.account.key).toMatch(/^d:[0-9a-f]{16}$/);
        expect(JSON.stringify(record())).not.toContain('bob');
        expect(record()?.context).toEqual({});
    });

    test('crank:private-state-purged clears the record, context and visibility', () => {
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        openAssistant({organizationId: 3});
        expect(record()?.context.organizationId).toBe(3);
        setPrioritiesRevision(4);
        document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
        expect(record()).toBeNull();
        expect(getWorkspaceSnapshot().prioritiesRevision).toBeNull();
        expect(getWorkspaceSnapshot().visibility).toBe('closed');
        expect(getWorkspaceSnapshot().context?.organizationId).toBeUndefined();
        expect(getWorkspaceSnapshot().account.status).toBe('unknown');
    });

    test('writes only visibility, entity context and the account stamp', () => {
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        openAssistant({
            surface: 'jobs', organizationId: 3, organizationName: 'Zed', jobId: 9,
            comparisonIds: [1, 2], searchTerm: 'secret', page: 4,
        });
        const written = record();
        expect(Object.keys(written).sort()).toEqual(['account', 'context', 'savedAt', 'v', 'visibility']);
        expect(written.context).toEqual({
            organizationId: 3, organizationName: 'Zed', jobId: 9, comparisonIds: [1, 2],
        });
        expect(JSON.stringify(written)).not.toContain('secret');
        expect(JSON.stringify(written)).not.toContain('conversation');
    });

    test('nothing is written while the account is unknown', () => {
        teardown = installWorkspacePersistence();
        openAssistant({organizationId: 3});
        expect(record()).toBeNull();
    });

    test('malformed or foreign records are ignored', () => {
        for (const bad of ['not json', JSON.stringify({v: 2}), JSON.stringify({v: 1, account: {status: 'x', key: ''}}),
            JSON.stringify({v: 1, account: {status: 'anonymous', key: ''}, visibility: 'weird'})]) {
            window.sessionStorage.setItem(WORKSPACE_SESSION_KEY, bad);
            setWorkspaceAccount({status: 'anonymous', key: ''});
            const stop = installWorkspacePersistence();
            expect(getWorkspaceSnapshot().visibility).toBe('closed');
            stop();
        }
    });

    test('a record without savedAt still restores', () => {
        seedRecord({savedAt: 'x'});
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().context?.organizationId).toBe(7);
    });

    test('storage that throws degrades to memory', () => {
        jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
        jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
        jest.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('denied'); });
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        expect(() => openAssistant({organizationId: 3})).not.toThrow();
        expect(getWorkspaceSnapshot().context?.organizationId).toBe(3);
        expect(() => document.dispatchEvent(new CustomEvent('crank:private-state-purged'))).not.toThrow();
    });

    test('a failed whoami (unobserved) never re-stamps the record or drops the context (issue #479)', () => {
        seedRecord();
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        const before = record();
        hydrate({authenticated: false, username: null, unobserved: true});
        expect(getWorkspaceSnapshot().account).toEqual({status: 'authenticated', key: 'alice'});
        expect(getWorkspaceSnapshot().context?.organizationId).toBe(7);
        expect(record()).toEqual(before);
        // A later real anonymous hydration is still treated as a sign-out.
        hydrate({authenticated: false, username: null});
        expect(getWorkspaceSnapshot().account.status).toBe('anonymous');
        expect(getWorkspaceSnapshot().context?.organizationId).toBeUndefined();
    });

    test('an unobserved hydration leaves an unknown account unknown', () => {
        teardown = installWorkspacePersistence();
        hydrate({authenticated: false, username: null, unobserved: true});
        expect(getWorkspaceSnapshot().account.status).toBe('unknown');
        expect(record()).toBeNull();
    });

    test('install is idempotent and hydration without detail is ignored', () => {
        teardown = installWorkspacePersistence();
        const second = installWorkspacePersistence();
        second();
        document.dispatchEvent(new CustomEvent('crank:auth-hydrated'));
        expect(getWorkspaceSnapshot().account.status).toBe('unknown');
    });
});

describe('workspace persistence review fixes (issue #479)', () => {
    beforeEach(() => window.localStorage.clear());

    test.each([
        ['/chat/?company=12', 'chat'],
        ['/?company=12', 'rankings'],
    ] as const)('explicit page company wins over a conflicting persisted entity on %s', (url, surface) => {
        window.history.replaceState(null, '', url);
        seedRecord({context: {organizationId: 7, organizationName: 'Acme'}});
        setWorkspaceContext({surface, organizationId: 12});
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().context).toEqual({surface, organizationId: 12});
        window.history.replaceState(null, '', '/');
    });

    test('persisted entity group is applied whole when the page names no entity', () => {
        seedRecord({context: {organizationId: 7, organizationName: 'Acme', jobId: 3}});
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().context)
            .toEqual({surface: 'rankings', organizationId: 7, organizationName: 'Acme', jobId: 3});
    });

    test('a page entity with the same company id keeps the persisted name; another id drops it', () => {
        const record = (organizationId: number) => window.sessionStorage.setItem(WORKSPACE_SESSION_KEY, JSON.stringify({
            v: 1,
            account: {status: 'authenticated', key: 'alice'},
            visibility: 'open',
            context: {organizationId, organizationName: 'Acme'},
            savedAt: 1,
        }));
        record(7);
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        setWorkspaceContext({surface: 'chat', organizationId: 7});
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().context)
            .toEqual({surface: 'chat', organizationId: 7, organizationName: 'Acme'});
        teardown();
        resetWorkspaceForTests();
        window.sessionStorage.clear();

        record(7);
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        setWorkspaceContext({surface: 'chat', organizationId: 12});
        teardown = installWorkspacePersistence();
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'chat', organizationId: 12});
    });

    test('an account switch here does not write the cross-tab signal itself (app-nav announces after hydration)', () => {
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        hydrate({authenticated: true, username: 'bob'});
        expect(window.localStorage.getItem('crank:account-epoch')).toBeNull();
    });

    test('a purge keeps a pinned /chat/ assistant open and resets its entity context', () => {
        const host = document.createElement('div');
        host.id = 'assistant-workspace';
        host.dataset.assistantPinned = 'true';
        document.body.appendChild(host);
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        replaceWorkspaceState('open', {surface: 'chat', organizationId: 7, organizationName: 'Acme'});
        document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
        expect(getWorkspaceSnapshot().visibility).toBe('open');
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'chat'});
        host.remove();
    });

    test('a purge still closes the assistant on non-pinned pages', () => {
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        replaceWorkspaceState('open', {surface: 'rankings', organizationId: 7});
        document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
        expect(getWorkspaceSnapshot().visibility).toBe('closed');
    });

    test('a broken localStorage does not break the account switch', () => {
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        const spy = jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
            throw new Error('quota');
        });
        expect(() => hydrate({authenticated: true, username: 'bob'})).not.toThrow();
        spy.mockRestore();
    });
});
