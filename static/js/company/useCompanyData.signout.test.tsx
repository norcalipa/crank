// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {act, renderHook} from '@testing-library/react';

import {clearProvenanceCache} from '../provenanceCache';
import {useCompanyData} from './useCompanyData';

const correction = (id: number) => ({
    id, field_key: 'rto_policy', field_label: 'RTO policy', proposed_value: `value ${id}`,
    status: 'pending', status_label: 'pending',
});

describe('useCompanyData sign-out and sign-in again', () => {
    let pending: {url: string; resolve: (response: unknown) => void}[];
    let errorSpy: jest.SpyInstance;

    const settle = async (fragment: string, body: unknown) => {
        const index = pending.findIndex(item => item.url.includes(fragment));
        expect(index).toBeGreaterThanOrEqual(0);
        const [item] = pending.splice(index, 1);
        await act(async () => {
            item.resolve({ok: true, status: 200, json: () => Promise.resolve(body)});
        });
    };

    beforeEach(() => {
        pending = [];
        clearProvenanceCache();
        global.fetch = jest.fn().mockImplementation((url: string) => new Promise(resolve => {
            pending.push({url, resolve});
        }));
        errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        errorSpy.mockRestore();
    });

    // The list is tagged with the company id, not the account, so the reset at
    // sign-out is the only thing that keeps the earlier session's suggestions
    // out of the render in which the next session signs in.
    test('no render after sign-out exposes the earlier session\'s pending suggestions', async () => {
        const renders: {signedIn: boolean; pending: unknown[]}[] = [];
        const {rerender} = renderHook(({signedIn}) => {
            const data = useCompanyData(1, signedIn);
            renders.push({signedIn, pending: data.pendingCorrections});
            return data;
        }, {initialProps: {signedIn: true}});
        await settle('/company-corrections/', {corrections: [correction(7)]});
        expect(renders[renders.length - 1].pending).toEqual([correction(7)]);

        const signedOutAt = renders.length;
        rerender({signedIn: false});
        rerender({signedIn: true});
        // The second session has not answered yet; then it answers with a different list.
        expect(renders.length).toBeGreaterThan(signedOutAt + 1);
        await settle('/company-corrections/', {corrections: [correction(8)]});

        const after = renders.slice(signedOutAt);
        expect(after.filter(entry => entry.signedIn).length).toBeGreaterThanOrEqual(2);
        for (const entry of after.slice(0, -1)) expect(entry.pending).toEqual([]);
        expect(after[after.length - 1].pending).toEqual([correction(8)]);
    });
});
