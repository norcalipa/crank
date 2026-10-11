// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {act, renderHook, waitFor} from '@testing-library/react';

import {clearProvenanceCache, getCachedProvenance} from '../provenanceCache';
import {getWorkspaceSnapshot, resetWorkspaceForTests, setWorkspaceAccount} from '../workspace/store';
import {COMPANY_CORRECTION_SAVED_EVENT, useCompanyData} from './useCompanyData';

interface Deferred {
    url: string;
    resolve: (response: unknown) => void;
    reject: (error: Error) => void;
}

const ok = (body: unknown) => ({ok: true, status: 200, json: () => Promise.resolve(body)});
const failed = (status: number, body: unknown = {detail: 'nope'}) => (
    {ok: false, status, json: () => Promise.resolve(body)}
);

const scoresOf = (name: string) => [{type__name: name, avg_score: 4.5}];
const provenanceOf = (id: number) => ({
    organization_id: id,
    organization_modified: '2025-01-15T10:00:00Z',
    organization_created: '2024-06-01T00:00:00Z',
    latest_observation: null,
    fields: [],
});
const correction = (id: number, status = 'pending') => ({
    id, field_key: 'rto_policy', field_label: 'RTO policy', proposed_value: `value ${id}`,
    status, status_label: status,
});

describe('useCompanyData', () => {
    let pending: Deferred[];
    let errorSpy: jest.SpyInstance;

    // Every request stays open until the test settles it, so each test decides
    // the order in which responses arrive.
    const settle = async (fragment: string, response: unknown) => {
        const index = pending.findIndex(item => item.url.includes(fragment));
        expect(index).toBeGreaterThanOrEqual(0);
        const [item] = pending.splice(index, 1);
        await act(async () => {
            if (response instanceof Error) item.reject(response);
            else item.resolve(response);
        });
    };
    const requested = (fragment: string) => (global.fetch as jest.Mock).mock.calls
        .filter(([url]) => String(url).includes(fragment)).length;

    beforeEach(() => {
        pending = [];
        clearProvenanceCache();
        global.fetch = jest.fn().mockImplementation((url: string) => new Promise((resolve, reject) => {
            pending.push({url, resolve, reject});
        }));
        errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        errorSpy.mockRestore();
    });

    test('loads scores, provenance and the pending suggestions for a company', async () => {
        const {result} = renderHook(() => useCompanyData(1, true));

        expect(result.current.scoresLoading).toBe(true);
        expect(result.current.provenanceLoading).toBe(true);
        expect(result.current.scores).toEqual([]);
        expect(result.current.provenance).toBeNull();
        expect(result.current.pendingCorrections).toEqual([]);
        expect((global.fetch as jest.Mock).mock.calls.map(([url]) => url)).toEqual([
            '/api/organizations/1/scores/',
            '/api/organizations/1/provenance/',
            '/api/company-corrections/?organization=1',
        ]);

        await settle('/scores/', ok(scoresOf('Culture')));
        await settle('/provenance/', ok(provenanceOf(1)));
        await settle('/company-corrections/', ok({corrections: [correction(7), correction(8, 'accepted')]}));

        expect(result.current.scoresLoading).toBe(false);
        expect(result.current.scores).toEqual(scoresOf('Culture'));
        expect(result.current.provenanceLoading).toBe(false);
        expect(result.current.provenance).toEqual(provenanceOf(1));
        expect(getCachedProvenance(1)).toEqual(provenanceOf(1));
        expect(result.current.pendingCorrections.map(item => item.id)).toEqual([7]);
        expect(errorSpy).not.toHaveBeenCalled();
    });

    test('a response without an ok flag is read as a success', async () => {
        const {result} = renderHook(() => useCompanyData(1, false));

        await settle('/scores/', {json: () => Promise.resolve(scoresOf('Culture'))});
        await settle('/provenance/', {json: () => Promise.resolve(provenanceOf(1))});

        expect(result.current.scores).toEqual(scoresOf('Culture'));
        expect(result.current.provenance).toEqual(provenanceOf(1));
    });

    test('everything is reset the moment the company id changes', async () => {
        const {result, rerender} = renderHook(({id}) => useCompanyData(id, true), {initialProps: {id: 1}});
        await settle('/scores/', ok(scoresOf('Culture')));
        await settle('/provenance/', ok(provenanceOf(1)));
        await settle('/company-corrections/', ok({corrections: [correction(7)]}));
        expect(result.current.scores).toHaveLength(1);
        expect(result.current.pendingCorrections).toHaveLength(1);

        rerender({id: 2});

        // Nothing of company 1 is returned for company 2, before any of
        // company 2's requests has answered.
        expect(result.current.scores).toEqual([]);
        expect(result.current.scoresLoading).toBe(true);
        expect(result.current.provenance).toBeNull();
        expect(result.current.provenanceLoading).toBe(true);
        expect(result.current.pendingCorrections).toEqual([]);
        expect(requested('/api/organizations/2/scores/')).toBe(1);
        expect(requested('/api/organizations/2/provenance/')).toBe(1);
        expect(requested('/api/company-corrections/?organization=2')).toBe(1);

        await settle('/organizations/2/scores/', ok(scoresOf('Leadership')));
        expect(result.current.scores).toEqual(scoresOf('Leadership'));
    });

    // The tests above read the hook after React has run its effects. These
    // record what every single render returned, including the one between an
    // id change and the effect that starts the new requests.
    describe('every render, not only the settled one', () => {
        interface Seen {
            id: number | null;
            signedIn: boolean;
            scores: unknown[];
            scoresLoading: boolean;
            provenance: unknown;
            provenanceLoading: boolean;
            pendingCorrections: unknown[];
        }
        const recordingHook = (
            initialProps: {id: number | null; signedIn: boolean}, options?: {skipScores?: boolean},
        ) => {
            const renders: Seen[] = [];
            const view = renderHook(({id, signedIn}) => {
                const data = useCompanyData(id, signedIn, options);
                renders.push({
                    id, signedIn, scores: data.scores, scoresLoading: data.scoresLoading,
                    provenance: data.provenance, provenanceLoading: data.provenanceLoading,
                    pendingCorrections: data.pendingCorrections,
                });
                return data;
            }, {initialProps});
            return {...view, renders};
        };

        test('no render for company 2 returns anything of company 1, and each one reports loading', async () => {
            const {renders, rerender} = recordingHook({id: 1, signedIn: true});
            // Loading from the very first render, before any effect has run.
            expect(renders[0]).toMatchObject({id: 1, scoresLoading: true, provenanceLoading: true});
            await settle('/scores/', ok(scoresOf('Culture')));
            await settle('/provenance/', ok(provenanceOf(1)));
            await settle('/company-corrections/', ok({corrections: [correction(7)]}));
            expect(renders[renders.length - 1]).toMatchObject({
                id: 1, scores: scoresOf('Culture'), provenance: provenanceOf(1), pendingCorrections: [correction(7)],
            });

            rerender({id: 2, signedIn: true});

            const forTwo = renders.filter(entry => entry.id === 2);
            // The render before the effects and the one their state updates cause.
            expect(forTwo.length).toBeGreaterThanOrEqual(2);
            for (const entry of forTwo) {
                expect(entry).toEqual({
                    id: 2, signedIn: true, scores: [], scoresLoading: true, provenance: null,
                    provenanceLoading: true, pendingCorrections: [],
                });
            }
        });

        test('with skipScores no render reports scores as loading, across a company change', async () => {
            const {renders, rerender} = recordingHook({id: 1, signedIn: false}, {skipScores: true});
            await settle('/provenance/', ok(provenanceOf(1)));
            rerender({id: 2, signedIn: false});
            await settle('/organizations/2/provenance/', ok(provenanceOf(2)));

            expect(renders.filter(entry => entry.id === 2).length).toBeGreaterThanOrEqual(2);
            expect(renders.map(entry => entry.scoresLoading)).toEqual(renders.map(() => false));
            expect(renders.map(entry => entry.scores)).toEqual(renders.map(() => []));
            expect(requested('/scores/')).toBe(0);
        });

        test('no signed-out render returns the pending suggestions loaded while signed in', async () => {
            const {renders, rerender} = recordingHook({id: 1, signedIn: true});
            await settle('/company-corrections/', ok({corrections: [correction(7)]}));
            expect(renders[renders.length - 1].pendingCorrections).toEqual([correction(7)]);

            rerender({id: 1, signedIn: false});

            const signedOut = renders.filter(entry => !entry.signedIn);
            expect(signedOut.length).toBeGreaterThanOrEqual(1);
            for (const entry of signedOut) expect(entry.pendingCorrections).toEqual([]);
        });
    });

    test('a non-OK scores response leaves the scores empty', async () => {
        const {result, rerender} = renderHook(({id}) => useCompanyData(id, false), {initialProps: {id: 1}});
        await settle('/scores/', ok(scoresOf('Culture')));
        expect(result.current.scores).toEqual(scoresOf('Culture'));

        rerender({id: 2});
        // The error body is an array on purpose: it must not be shown even
        // when it has the shape of a score list.
        await settle('/organizations/2/scores/', failed(500, scoresOf('Not a score')));

        expect(result.current.scores).toEqual([]);
        expect(result.current.scoresLoading).toBe(false);
        expect(errorSpy).toHaveBeenCalledWith('Error fetching organization scores:', expect.any(Error));
        expect(String(errorSpy.mock.calls[0][1])).toContain('Scores request failed (500)');
    });

    test('a scores body that is not a list, and a rejected request, both leave the scores empty', async () => {
        const {result, rerender} = renderHook(({id}) => useCompanyData(id, false), {initialProps: {id: 1}});
        await settle('/scores/', ok({detail: 'unexpected'}));
        expect(result.current.scores).toEqual([]);
        expect(result.current.scoresLoading).toBe(false);

        rerender({id: 2});
        await settle('/organizations/2/scores/', new Error('offline'));
        expect(result.current.scores).toEqual([]);
        expect(result.current.scoresLoading).toBe(false);
        expect(errorSpy).toHaveBeenCalledWith('Error fetching organization scores:', expect.any(Error));
    });

    test('responses for a previous company are ignored, whether they succeed or fail', async () => {
        const {result, rerender} = renderHook(({id}) => useCompanyData(id, true), {initialProps: {id: 1}});
        rerender({id: 2});
        await settle('/organizations/2/scores/', ok(scoresOf('Leadership')));
        await settle('/organizations/2/provenance/', ok(provenanceOf(2)));
        await settle('organization=2', ok({corrections: [correction(9)]}));

        // Company 1's answers arrive after company 2's.
        await settle('/organizations/1/scores/', ok(scoresOf('Culture')));
        await settle('/organizations/1/provenance/', ok(provenanceOf(1)));
        await settle('organization=1', ok({corrections: [correction(7)]}));

        expect(result.current.scores).toEqual(scoresOf('Leadership'));
        expect(result.current.provenance).toEqual(provenanceOf(2));
        expect(result.current.pendingCorrections.map(item => item.id)).toEqual([9]);
        expect(getCachedProvenance(1)).toBeUndefined();

        rerender({id: 3});
        rerender({id: 4});
        await settle('/organizations/3/scores/', new Error('late failure'));
        await settle('/organizations/3/provenance/', new Error('late failure'));
        await settle('organization=3', new Error('late failure'));

        expect(result.current.scoresLoading).toBe(true);
        expect(result.current.provenanceLoading).toBe(true);
        expect(errorSpy).not.toHaveBeenCalled();
    });

    test('unmounting cancels every request', async () => {
        const {result, unmount} = renderHook(() => useCompanyData(5, true));
        const before = result.current;
        unmount();

        await settle('/scores/', ok(scoresOf('Culture')));
        await settle('/provenance/', ok(provenanceOf(5)));
        await settle('/company-corrections/', ok({corrections: [correction(7)]}));

        expect(result.current).toBe(before);
        expect(getCachedProvenance(5)).toBeUndefined();
        expect(errorSpy).not.toHaveBeenCalled();
    });

    test('signed out, the pending-suggestions request is never made', async () => {
        const {result} = renderHook(() => useCompanyData(1, false));

        expect(requested('/api/company-corrections/')).toBe(0);
        expect(requested('/api/organizations/1/scores/')).toBe(1);
        expect(requested('/api/organizations/1/provenance/')).toBe(1);
        expect(result.current.pendingCorrections).toEqual([]);
    });

    test('signing out drops the pending suggestions; signing in requests them', async () => {
        const {result, rerender} = renderHook(
            ({signedIn}) => useCompanyData(1, signedIn), {initialProps: {signedIn: true}});
        await settle('/company-corrections/', ok({corrections: [correction(7)]}));
        expect(result.current.pendingCorrections).toHaveLength(1);

        rerender({signedIn: false});
        expect(result.current.pendingCorrections).toEqual([]);

        rerender({signedIn: true});
        expect(requested('/api/company-corrections/')).toBe(2);
        expect(result.current.pendingCorrections).toEqual([]);
        // Scores and provenance are not requested again for the same company.
        expect(requested('/scores/')).toBe(1);
        expect(requested('/provenance/')).toBe(1);
    });

    test('a non-OK, malformed or rejected pending-suggestions response hides the list', async () => {
        const {result, rerender} = renderHook(({id}) => useCompanyData(id, true), {initialProps: {id: 1}});
        await settle('/company-corrections/', failed(401));
        expect(result.current.pendingCorrections).toEqual([]);

        rerender({id: 2});
        await settle('organization=2', ok({corrections: 'nope'}));
        expect(result.current.pendingCorrections).toEqual([]);

        rerender({id: 3});
        await settle('organization=3', new Error('offline'));
        expect(result.current.pendingCorrections).toEqual([]);
        expect(errorSpy).not.toHaveBeenCalled();
    });

    test('a late failure for the previous company does not hide the loaded list', async () => {
        const {result, rerender} = renderHook(({id}) => useCompanyData(id, true), {initialProps: {id: 1}});
        rerender({id: 2});
        await settle('organization=2', ok({corrections: [correction(9)]}));

        await settle('organization=1', new Error('late failure'));

        expect(result.current.pendingCorrections.map(item => item.id)).toEqual([9]);
    });

    test('a non-OK answer that carries a corrections list shows nothing', async () => {
        const {result} = renderHook(() => useCompanyData(1, true));
        // The error body has the shape of a real answer on purpose: only the
        // status may decide that it is not shown.
        await settle('/company-corrections/', failed(403, {corrections: [correction(7)]}));

        expect(result.current.pendingCorrections).toEqual([]);
    });

    test('requests that fail after the unmount change nothing and log nothing', async () => {
        const {result, unmount} = renderHook(() => useCompanyData(5, true));
        const before = result.current;
        unmount();

        await settle('/scores/', new Error('late failure'));
        await settle('/provenance/', new Error('late failure'));
        await settle('/company-corrections/', new Error('late failure'));

        expect(result.current).toBe(before);
        expect(errorSpy).not.toHaveBeenCalled();
    });

    test('skipScores makes no scores request and never reports loading', async () => {
        const {result, rerender} = renderHook(
            ({skip}) => useCompanyData(1, false, {skipScores: skip}), {initialProps: {skip: true}});

        expect(requested('/scores/')).toBe(0);
        expect(result.current.scoresLoading).toBe(false);
        expect(result.current.scores).toEqual([]);

        rerender({skip: false});
        expect(requested('/scores/')).toBe(1);
        expect(result.current.scoresLoading).toBe(true);
    });

    test('no company: nothing is requested and everything is empty', async () => {
        const {result, rerender} = renderHook(
            ({id}: {id: number | null}) => useCompanyData(id, true), {initialProps: {id: null as number | null}});

        expect(global.fetch).not.toHaveBeenCalled();
        expect(result.current).toMatchObject({
            scores: [], scoresLoading: false, provenance: null, provenanceLoading: false,
            pendingCorrections: [],
        });

        rerender({id: 1});
        await settle('/scores/', ok(scoresOf('Culture')));
        await settle('/provenance/', ok(provenanceOf(1)));
        rerender({id: null});

        expect(result.current).toMatchObject({
            scores: [], scoresLoading: false, provenance: null, provenanceLoading: false,
            pendingCorrections: [],
        });
    });

    test('a failed provenance request can be retried; only a retry that fails is counted', async () => {
        const {result} = renderHook(() => useCompanyData(1, false));
        await settle('/provenance/', failed(503));
        expect(result.current.provenanceLoading).toBe(false);
        expect(result.current.provenance).toBeNull();
        // The first, automatic attempt is not a retry.
        expect(result.current.retryFailures).toBe(0);
        expect(String(errorSpy.mock.calls[0][1])).toContain('Provenance request failed (503)');

        act(() => result.current.retryProvenance());
        expect(result.current.provenanceLoading).toBe(true);
        expect(requested('/provenance/')).toBe(2);
        await settle('/provenance/', new Error('offline'));
        expect(result.current.retryFailures).toBe(1);

        act(() => result.current.retryProvenance());
        await settle('/provenance/', ok(provenanceOf(1)));
        expect(result.current.provenance).toEqual(provenanceOf(1));
        expect(result.current.retryFailures).toBe(1);
    });

    test('a retry started for one company is not counted against the next', async () => {
        const {result, rerender} = renderHook(({id}) => useCompanyData(id, false), {initialProps: {id: 1}});
        await settle('/provenance/', failed(500));
        act(() => result.current.retryProvenance());

        rerender({id: 2});
        await settle('/organizations/2/provenance/', failed(500));
        expect(result.current.retryFailures).toBe(0);
    });

    test('provenance already loaded stays while the same company is retried', async () => {
        const {result} = renderHook(() => useCompanyData(1, false));
        await settle('/provenance/', ok(provenanceOf(1)));

        act(() => result.current.retryProvenance());
        expect(result.current.provenanceLoading).toBe(true);
        expect(result.current.provenance).toEqual(provenanceOf(1));
        await waitFor(() => expect(requested('/provenance/')).toBe(2));
    });
    describe.each([['abc'], [1.5], [-1], [0], ['1/../2'], [''], [Number.NaN], [2 ** 31]])(
        'an id that is not a positive integer (%p)', (bad) => {
            test('requests nothing and shows nothing', () => {
                const {result} = renderHook(() => useCompanyData(bad as unknown as number, true));

                expect(global.fetch).not.toHaveBeenCalled();
                expect(result.current).toMatchObject({
                    scores: [], scoresLoading: false, provenance: null, provenanceLoading: false, pendingCorrections: [],
                });
            });
        });

    describe('private state (issue #486)', () => {
        const loadPending = async (isAuthenticated = true) => {
            const hook = renderHook(() => useCompanyData(1, isAuthenticated, {skipScores: true}));
            await settle('/provenance/', ok(provenanceOf(1)));
            if (isAuthenticated) {
                await settle('/company-corrections/', ok({corrections: [correction(7)]}));
                expect(hook.result.current.pendingCorrections.map(item => item.id)).toEqual([7]);
            }
            return hook;
        };

        beforeEach(() => {
            resetWorkspaceForTests();
        });

        test('crank:private-state-purged removes the pending suggestions while isAuthenticated stays true', async () => {
            const {result} = await loadPending();

            act(() => {
                document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
            });

            expect(result.current.pendingCorrections).toEqual([]);
            // The public evidence stays; only the account's rows go.
            expect(result.current.provenance).toEqual(provenanceOf(1));
        });

        test('a purge also drops a pending-suggestions answer that was still in flight', async () => {
            const {result} = renderHook(() => useCompanyData(1, true, {skipScores: true}));
            await settle('/provenance/', ok(provenanceOf(1)));

            act(() => {
                document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
            });
            await settle('/company-corrections/', ok({corrections: [correction(9)]}));

            expect(result.current.pendingCorrections).toEqual([]);
        });

        test('an account switch in the store removes the pending suggestions, whoever is signed in after it', async () => {
            act(() => setWorkspaceAccount({status: 'authenticated', key: 'alice'}));
            const {result} = await loadPending();

            act(() => setWorkspaceAccount({status: 'unknown', key: ''}));
            expect(result.current.pendingCorrections).toEqual([]);
            act(() => setWorkspaceAccount({status: 'authenticated', key: 'bob'}));
            expect(result.current.pendingCorrections).toEqual([]);
            expect(getWorkspaceSnapshot().account.key).toBe('bob');
        });

        test('a direct switch from one account to another also clears them', async () => {
            act(() => setWorkspaceAccount({status: 'authenticated', key: 'alice'}));
            const {result} = await loadPending();

            act(() => setWorkspaceAccount({status: 'authenticated', key: 'bob'}));

            expect(result.current.pendingCorrections).toEqual([]);
        });

        test('the first hydration (unknown to a known account) and an unrelated store change keep them', async () => {
            const {result} = await loadPending();

            act(() => setWorkspaceAccount({status: 'authenticated', key: 'alice'}));
            expect(result.current.pendingCorrections.map(item => item.id)).toEqual([7]);
            act(() => setWorkspaceAccount({status: 'authenticated', key: 'alice'}));
            expect(result.current.pendingCorrections.map(item => item.id)).toEqual([7]);
        });

        test('the listeners are released on unmount', async () => {
            const removeSpy = jest.spyOn(document, 'removeEventListener');
            const {unmount} = await loadPending(false);

            unmount();

            expect(removeSpy).toHaveBeenCalledWith('crank:private-state-purged', expect.any(Function));
            removeSpy.mockRestore();
        });
    });

    describe('a correction stored while the view stays open (issue #486)', () => {
        const saved = (organizationId: unknown) => act(() => {
            window.dispatchEvent(new CustomEvent(COMPANY_CORRECTION_SAVED_EVENT, {detail: {organizationId}}));
        });

        test('the pending suggestions are requested again and the new one appears without a flash of emptiness', async () => {
            const {result} = renderHook(() => useCompanyData(1, true, {skipScores: true}));
            await settle('/provenance/', ok(provenanceOf(1)));
            await settle('/company-corrections/', ok({corrections: [correction(7)]}));

            saved(1);
            expect(requested('/company-corrections/')).toBe(2);
            expect(result.current.pendingCorrections.map(item => item.id)).toEqual([7]);
            await settle('/company-corrections/', ok({corrections: [correction(7), correction(8)]}));

            expect(result.current.pendingCorrections.map(item => item.id)).toEqual([7, 8]);
        });

        test('another company\'s or a malformed event changes nothing; signed out, nothing is requested', async () => {
            renderHook(() => useCompanyData(1, true, {skipScores: true}));
            await settle('/provenance/', ok(provenanceOf(1)));
            await settle('/company-corrections/', ok({corrections: []}));

            saved(2);
            saved(undefined);
            expect(requested('/company-corrections/')).toBe(1);

            const signedOut = renderHook(() => useCompanyData(3, false, {skipScores: true}));
            saved(3);
            expect(requested('/organization=3')).toBe(0);
            expect(signedOut.result.current.pendingCorrections).toEqual([]);
        });
    });
});
