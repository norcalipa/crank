// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {
    applyWorkspaceFilters,
    buildWireContext,
    clearWorkspaceContext,
    closeAssistant,
    describeWorkspaceContext,
    getWorkspaceSnapshot,
    installWorkspaceBridge,
    markWorkspaceLoaded,
    minimizeAssistant,
    normalizeWorkspaceContext,
    openAssistant,
    registerFilterTarget,
    replaceWorkspaceState,
    resetPriorities,
    resetWorkspaceForTests,
    setPrioritiesEditorOpen,
    setPrioritiesRevision,
    setWorkspaceAccount,
    setWorkspaceConversation,
    setWorkspaceContext,
    setWorkspaceEntity,
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
            userOpened: false,
            prioritiesRevision: null,
            prioritiesEditorOpenIn: null,
            prioritiesEditorSeed: null,
        });
    });

    test('priorities signals are additive, deduplicated and reset together (issue #480)', () => {
        const listener = jest.fn();
        subscribeWorkspace(listener);
        setPrioritiesRevision(3);
        setPrioritiesRevision(3);
        setPrioritiesEditorOpen('main');
        setPrioritiesEditorOpen('main');
        expect(listener).toHaveBeenCalledTimes(2);
        expect(getWorkspaceSnapshot()).toMatchObject({prioritiesRevision: 3, prioritiesEditorOpenIn: 'main'});
        resetPriorities();
        expect(getWorkspaceSnapshot()).toMatchObject({prioritiesRevision: null, prioritiesEditorOpenIn: null});
    });

    test('the editor seed travels with the open host and is dropped on close and reset (issue #480)', () => {
        const seed = {set: {'compensation.minimum_salary': 200000}};
        setPrioritiesEditorOpen('sidebar', seed);
        expect(getWorkspaceSnapshot()).toMatchObject({prioritiesEditorOpenIn: 'sidebar', prioritiesEditorSeed: seed});
        setPrioritiesEditorOpen('sidebar', seed);
        setPrioritiesEditorOpen(null, seed);
        expect(getWorkspaceSnapshot()).toMatchObject({prioritiesEditorOpenIn: null, prioritiesEditorSeed: null});
        setPrioritiesEditorOpen('main', seed);
        resetPriorities();
        expect(getWorkspaceSnapshot().prioritiesEditorSeed).toBeNull();
    });

    test('userOpened is set by openAssistant and cleared by a wholesale replace (issue #479)', () => {
        openAssistant();
        expect(getWorkspaceSnapshot().userOpened).toBe(true);
        replaceWorkspaceState('open', null);
        expect(getWorkspaceSnapshot().userOpened).toBe(false);
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
        expect(describeWorkspaceContext({surface: 'company', organizationId: 4})).toBe('About this company');
        expect(describeWorkspaceContext({surface: 'jobs', jobId: 8})).toBe('About job #8');
        expect(describeWorkspaceContext({surface: 'comparison', comparisonIds: [1, 2]})).toBe('Comparing 2 companies');
    });
});

describe('clearWorkspaceContext URL handling (issue #479 review)', () => {
    afterEach(() => window.history.replaceState(null, '', '/'));

    test('drops company from the URL even when no context is stored', () => {
        window.history.replaceState(null, '', '/chat/?company=12');
        clearWorkspaceContext();
        expect(window.location.search).toBe('');
    });

    test('leaves a URL without an entity param untouched', () => {
        window.history.replaceState(null, '', '/chat/?page=2');
        clearWorkspaceContext();
        expect(window.location.search).toBe('?page=2');
    });

    test('a throwing history API never breaks the clear', () => {
        window.history.replaceState(null, '', '/chat/?company=12');
        const spy = jest.spyOn(window.history, 'replaceState').mockImplementation(() => {
            throw new Error('denied');
        });
        expect(() => clearWorkspaceContext()).not.toThrow();
        spy.mockRestore();
    });
});

describe('setWorkspaceEntity (issue #479 review round 2)', () => {
    test('replaces the whole entity group and keeps the rest', () => {
        setWorkspaceContext({surface: 'chat', organizationId: 7, organizationName: 'Acme', jobId: 3, comparisonIds: [1, 2]});
        setWorkspaceEntity({organizationId: 12});
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'chat', organizationId: 12});
    });

    test('an empty entity on an empty store leaves a null context', () => {
        setWorkspaceEntity({});
        expect(getWorkspaceSnapshot().context).toBeNull();
    });
});

describe('validated page context (issue #484)', () => {
    beforeEach(resetWorkspaceForTests);

    test('normalizes algorithm id, filters and the two version counters', () => {
        expect(normalizeWorkspaceContext({
            algorithmId: 3, resultGeneration: 0, preferenceRevision: 7,
            filters: {rtoPolicy: 'H', acceleratedVesting: true, extra: 1},
        })).toEqual({
            algorithmId: 3, resultGeneration: 0, preferenceRevision: 7,
            filters: {rtoPolicy: 'H', acceleratedVesting: true},
        });
    });

    test('drops hostile values for the new keys', () => {
        expect(normalizeWorkspaceContext({
            algorithmId: -1, resultGeneration: -2, preferenceRevision: 1.5,
            filters: {rtoPolicy: 'Z', acceleratedVesting: 'yes'},
        })).toEqual({filters: {}});
        expect(normalizeWorkspaceContext({algorithmId: '3', filters: 'R'})).toEqual({});
    });

    test('an empty filters object replaces earlier filters', () => {
        setWorkspaceContext({surface: 'rankings', filters: {rtoPolicy: 'R'}});
        setWorkspaceContext({filters: {}});
        expect(getWorkspaceSnapshot().context?.filters).toEqual({});
    });

    test('key order never bumps the revision; a real change does', () => {
        setWorkspaceContext({surface: 'rankings', page: 2, algorithmId: 1});
        const revision = getWorkspaceSnapshot().contextRevision;
        setWorkspaceContext({algorithmId: 1, page: 2, surface: 'rankings'});
        expect(getWorkspaceSnapshot().contextRevision).toBe(revision);
        setWorkspaceContext({page: 3});
        expect(getWorkspaceSnapshot().contextRevision).toBe(revision + 1);
    });

    test('buildWireContext is undefined without a context', () => {
        expect(buildWireContext(getWorkspaceSnapshot())).toBeUndefined();
    });

    test('buildWireContext is snake_case and omits names, search text and empty filters', () => {
        setWorkspaceContext({
            surface: 'rankings', organizationId: 5, organizationName: 'Secret Co', searchTerm: 'private',
            jobId: 9, comparisonIds: [1, 2], algorithmId: 3, page: 99999,
            filters: {rtoPolicy: 'R', acceleratedVesting: true},
            resultGeneration: 4, preferenceRevision: 8,
        });
        const snapshot = getWorkspaceSnapshot();
        expect(buildWireContext(snapshot)).toEqual({
            revision: snapshot.contextRevision, surface: 'rankings', organization_id: 5, job_id: 9,
            comparison_ids: [1, 2], algorithm_id: 3, page: 10000,
            filters: {rto_policy: 'R', accelerated_vesting: true},
            preference_revision: 8, result_generation: 4,
        });
        setWorkspaceContext({filters: {}, organizationId: 0, comparisonIds: [1, 2]});
        const wire = buildWireContext(getWorkspaceSnapshot());
        expect(wire).not.toHaveProperty('filters');
        expect(wire).not.toHaveProperty('organization_id');
        expect(JSON.stringify(wire)).not.toMatch(/Secret|private/);
    });

    test('buildWireContext keeps a minimal surface-only context', () => {
        setWorkspaceContext({surface: 'chat'});
        const snapshot = getWorkspaceSnapshot();
        expect(buildWireContext(snapshot)).toEqual({revision: snapshot.contextRevision, surface: 'chat'});
    });

    test('with no filter target, applying reports false', () => {
        expect(applyWorkspaceFilters({rtoPolicy: 'R'})).toBe(false);
    });

    test('the newest target wins, releasing it restores the previous one, and a decline reports false', () => {
        const first = jest.fn(() => true);
        const second = jest.fn(() => false);
        const releaseFirst = registerFilterTarget(first);
        const releaseSecond = registerFilterTarget(second);
        expect(applyWorkspaceFilters({rtoPolicy: 'H'})).toBe(false);
        expect(second).toHaveBeenCalledWith({rtoPolicy: 'H'});
        expect(first).not.toHaveBeenCalled();
        releaseSecond();
        expect(applyWorkspaceFilters({rtoPolicy: 'H'})).toBe(true);
        releaseFirst();
        expect(applyWorkspaceFilters({rtoPolicy: 'H'})).toBe(false);
    });
});
