// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {
    closeAssistant,
    getWorkspaceSnapshot,
    installWorkspaceBridge,
    markWorkspaceLoaded,
    minimizeAssistant,
    normalizeWorkspaceContext,
    openAssistant,
    resetWorkspaceForTests,
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
