// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {
    clearWorkspaceContext,
    closeAssistant,
    describeWorkspaceContext,
    getWorkspaceSnapshot,
    installWorkspaceBridge,
    markWorkspaceLoaded,
    minimizeAssistant,
    normalizeWorkspaceContext,
    openAssistant,
    replaceWorkspaceState,
    resetWorkspaceForTests,
    setWorkspaceAccount,
    setWorkspaceConversation,
    setWorkspaceContext,
    subscribeWorkspace,
} from './store';
import {WORKSPACE_CONTEXT_EVENT, WORKSPACE_OPEN_EVENT} from './types';

beforeEach(() => {
    resetWorkspaceForTests();
});

describe('workspace store', () => {
    test('initial snapshot', () => {
        expect(getWorkspaceSnapshot()).toEqual({
            visibility: 'closed',
            mode: 'sheet',
            context: null,
            loaded: false,
            contextRevision: 0,
            account: {status: 'unknown', key: ''},
            conversationId: null,
        });
    });

    test('openAssistant transitions and notifies once', () => {
        const listener = jest.fn();
        subscribeWorkspace(listener);
        openAssistant();
        expect(getWorkspaceSnapshot().visibility).toBe('open');
        expect(listener).toHaveBeenCalledTimes(1);
    });

    test('minimizeAssistant transitions only from open', () => {
        minimizeAssistant();
        expect(getWorkspaceSnapshot().visibility).toBe('closed');
        openAssistant();
        minimizeAssistant();
        expect(getWorkspaceSnapshot().visibility).toBe('minimized');
    });

    test('closeAssistant transitions and is a no-op when already closed', () => {
        const listener = jest.fn();
        subscribeWorkspace(listener);
        closeAssistant();
        expect(listener).not.toHaveBeenCalled();
        openAssistant();
        closeAssistant();
        expect(getWorkspaceSnapshot().visibility).toBe('closed');
        expect(listener).toHaveBeenCalledTimes(2);
    });

    test('setWorkspaceContext merges partials without clobbering unset keys', () => {
        setWorkspaceContext({surface: 'rankings', searchTerm: 'python'});
        setWorkspaceContext({page: 2});
        expect(getWorkspaceSnapshot().context).toEqual({
            surface: 'rankings',
            searchTerm: 'python',
            page: 2,
        });
    });

    test('openAssistant merges a context detail', () => {
        setWorkspaceContext({surface: 'jobs'});
        openAssistant({searchTerm: 'django'});
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'jobs', searchTerm: 'django'});
        expect(getWorkspaceSnapshot().visibility).toBe('open');
    });

    test('unsubscribe stops notifications', () => {
        const listener = jest.fn();
        const unsubscribe = subscribeWorkspace(listener);
        unsubscribe();
        openAssistant();
        expect(listener).not.toHaveBeenCalled();
    });

    test('markWorkspaceLoaded flips loaded exactly once', () => {
        const listener = jest.fn();
        subscribeWorkspace(listener);
        markWorkspaceLoaded();
        markWorkspaceLoaded();
        expect(getWorkspaceSnapshot().loaded).toBe(true);
        expect(listener).toHaveBeenCalledTimes(1);
    });

    test('the window singleton is shared across re-imports', () => {
        openAssistant();
        jest.resetModules();
        // eslint-disable-next-line @typescript-eslint/no-var-requires
        const again = require('./store');
        expect(again.getWorkspaceSnapshot().visibility).toBe('open');
    });
});

describe('normalizeWorkspaceContext', () => {
    test.each([
        ['no detail', undefined, {}],
        ['null', null, {}],
        ['a string', 'nope', {}],
        ['unknown keys', {surface: 'chat', bogus: 1}, {surface: 'chat'}],
        ['non-integer organizationId', {organizationId: '7'}, {}],
        ['valid organizationId', {organizationId: 7}, {organizationId: 7}],
        [
            'over-long organizationName',
            {organizationName: 'x'.repeat(250)},
            {organizationName: 'x'.repeat(200)},
        ],
        [
            'a valid detail',
            {surface: 'company', organizationId: 3, organizationName: 'Acme', page: 2},
            {surface: 'company', organizationId: 3, organizationName: 'Acme', page: 2},
        ],
    ])('%s', (_label, detail, expected) => {
        expect(normalizeWorkspaceContext(detail)).toEqual(expected);
    });
});

describe('installWorkspaceBridge', () => {
    test('open event opens the assistant with a normalized context', () => {
        const teardown = installWorkspaceBridge();
        window.dispatchEvent(new CustomEvent(WORKSPACE_OPEN_EVENT, {
            detail: {surface: 'jobs', bogus: 'dropped'},
        }));
        expect(getWorkspaceSnapshot().visibility).toBe('open');
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'jobs'});
        teardown();
    });

    test('context event merges context without opening', () => {
        const teardown = installWorkspaceBridge();
        window.dispatchEvent(new CustomEvent(WORKSPACE_CONTEXT_EVENT, {
            detail: {surface: 'help'},
        }));
        expect(getWorkspaceSnapshot().visibility).toBe('closed');
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'help'});
        teardown();
    });

    test('teardown removes both listeners', () => {
        const teardown = installWorkspaceBridge();
        teardown();
        window.dispatchEvent(new CustomEvent(WORKSPACE_OPEN_EVENT));
        window.dispatchEvent(new CustomEvent(WORKSPACE_CONTEXT_EVENT, {detail: {surface: 'help'}}));
        expect(getWorkspaceSnapshot().visibility).toBe('closed');
        expect(getWorkspaceSnapshot().context).toBeNull();
    });
});

describe('workspace store (issue #479 contract)', () => {
    test('jobId and comparisonIds are normalized', () => {
        expect(normalizeWorkspaceContext({jobId: 5, comparisonIds: [1, 2]}))
            .toEqual({jobId: 5, comparisonIds: [1, 2]});
        expect(normalizeWorkspaceContext({jobId: -1})).toEqual({});
        expect(normalizeWorkspaceContext({jobId: 1.5})).toEqual({});
        expect(normalizeWorkspaceContext({comparisonIds: [1, 1]})).toEqual({});
        expect(normalizeWorkspaceContext({comparisonIds: [1, 2, 3, 4, 5]})).toEqual({});
        expect(normalizeWorkspaceContext({comparisonIds: [1, 'x']})).toEqual({});
        expect(normalizeWorkspaceContext({comparisonIds: [0]})).toEqual({});
        expect(normalizeWorkspaceContext({comparisonIds: []})).toEqual({});
        expect(normalizeWorkspaceContext({comparisonIds: 'nope'})).toEqual({});
        const long = normalizeWorkspaceContext({organizationName: 'x'.repeat(500)});
        expect(long.organizationName).toHaveLength(200);
    });

    test('contextRevision bumps once per effective change and never on a no-op', () => {
        expect(getWorkspaceSnapshot().contextRevision).toBe(0);
        setWorkspaceContext({surface: 'rankings'});
        expect(getWorkspaceSnapshot().contextRevision).toBe(1);
        setWorkspaceContext({surface: 'rankings'});
        openAssistant({surface: 'rankings'});
        openAssistant();
        expect(getWorkspaceSnapshot().contextRevision).toBe(1);
        setWorkspaceContext({organizationId: 3});
        expect(getWorkspaceSnapshot().contextRevision).toBe(2);
        replaceWorkspaceState('open', getWorkspaceSnapshot().context);
        expect(getWorkspaceSnapshot().contextRevision).toBe(2);
    });

    test('clearWorkspaceContext keeps the surface, bumps the revision, and is idempotent', () => {
        clearWorkspaceContext();
        expect(getWorkspaceSnapshot().contextRevision).toBe(0);
        setWorkspaceContext({surface: 'company', organizationId: 3, organizationName: 'A', jobId: 2, comparisonIds: [1], searchTerm: 'q'});
        const before = getWorkspaceSnapshot().contextRevision;
        clearWorkspaceContext();
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'company', searchTerm: 'q'});
        expect(getWorkspaceSnapshot().contextRevision).toBe(before + 1);
        clearWorkspaceContext();
        expect(getWorkspaceSnapshot().contextRevision).toBe(before + 1);
    });

    test('account and conversation setters notify once and ignore no-ops', () => {
        const listener = jest.fn();
        subscribeWorkspace(listener);
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        setWorkspaceAccount({status: 'authenticated', key: 'alice'});
        expect(listener).toHaveBeenCalledTimes(1);
        setWorkspaceConversation(9);
        setWorkspaceConversation(9);
        expect(listener).toHaveBeenCalledTimes(2);
        expect(getWorkspaceSnapshot().account).toEqual({status: 'authenticated', key: 'alice'});
        expect(getWorkspaceSnapshot().conversationId).toBe(9);
    });

    test('replaceWorkspaceState replaces (not merges) visibility, context and conversation', () => {
        setWorkspaceContext({surface: 'company', organizationId: 3});
        replaceWorkspaceState('minimized', null);
        expect(getWorkspaceSnapshot()).toMatchObject({visibility: 'minimized', context: null, conversationId: null});
    });

    test('a version-1 singleton is replaced by a version-2 store', () => {
        window.__crankWorkspace__ = {
            version: 1,
            snapshot: {visibility: 'open', mode: 'sheet', context: null, loaded: true} as any,
            listeners: new Set(),
        };
        expect(getWorkspaceSnapshot().contextRevision).toBe(0);
        expect(getWorkspaceSnapshot().visibility).toBe('closed');
    });

    test('describeWorkspaceContext labels company, job and comparison contexts', () => {
        expect(describeWorkspaceContext(null)).toBe('');
        expect(describeWorkspaceContext({surface: 'rankings'})).toBe('');
        expect(describeWorkspaceContext({surface: 'company', organizationName: 'Acme'})).toBe('About Acme');
        expect(describeWorkspaceContext({surface: 'company', organizationId: 4})).toBe('About company #4');
        expect(describeWorkspaceContext({surface: 'jobs', jobId: 8})).toBe('About job #8');
        expect(describeWorkspaceContext({surface: 'comparison', comparisonIds: [1, 2]})).toBe('Comparing 2 companies');
    });
});
