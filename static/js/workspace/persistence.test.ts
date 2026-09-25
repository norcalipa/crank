// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {installWorkspacePersistence} from './persistence';
import {
    getWorkspaceSnapshot,
    openAssistant,
    resetWorkspaceForTests,
    setWorkspaceAccount,
    setWorkspaceContext,
} from './store';
import {WORKSPACE_SESSION_KEY} from './types';

let teardown: (() => void) | undefined;

function hydrate(detail: {authenticated: boolean; username: string | null}): void {
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
        expect(record()?.account.key).toBe('bob');
        expect(record()?.context).toEqual({});
    });

    test('crank:private-state-purged clears the record, context and visibility', () => {
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        teardown = installWorkspacePersistence();
        openAssistant({organizationId: 3});
        expect(record()?.context.organizationId).toBe(3);
        document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
        expect(record()).toBeNull();
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

    test('install is idempotent and hydration without detail is ignored', () => {
        teardown = installWorkspacePersistence();
        const second = installWorkspacePersistence();
        second();
        document.dispatchEvent(new CustomEvent('crank:auth-hydrated'));
        expect(getWorkspaceSnapshot().account.status).toBe('unknown');
    });
});
