// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {createLatestGuard} from './requests';

describe('createLatestGuard', () => {
    test('only the newest request is latest and the prior one is aborted', () => {
        const guard = createLatestGuard();
        const first = guard.begin();
        const second = guard.begin();
        expect(first.isLatest()).toBe(false);
        expect(first.signal.aborted).toBe(true);
        expect(second.isLatest()).toBe(true);
        expect(second.signal.aborted).toBe(false);
        expect(second.token).toBeGreaterThan(first.token);
    });

    test('out-of-order resolution keeps the newer result', async () => {
        const guard = createLatestGuard();
        let shown = '';
        const run = async (label: string, delay: number) => {
            const request = guard.begin();
            await new Promise((resolve) => setTimeout(resolve, delay));
            if (request.isLatest()) shown = label;
        };
        await Promise.all([run('older', 20), run('newer', 1)]);
        expect(shown).toBe('newer');
    });

    test('cancel aborts the in-flight request and makes it stale', () => {
        const guard = createLatestGuard();
        const request = guard.begin();
        guard.cancel();
        expect(request.signal.aborted).toBe(true);
        expect(request.isLatest()).toBe(false);
        guard.cancel();
    });
});
