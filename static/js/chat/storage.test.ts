// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {readComposerDraftTs} from './storage';

describe('readComposerDraftTs', () => {
    afterEach(() => {
        jest.restoreAllMocks();
        window.localStorage.clear();
    });

    test('reads the stored timestamp and defaults to 0', () => {
        expect(readComposerDraftTs(7)).toBe(0);
        window.localStorage.setItem('crank:jobsearch:draftts:7', '1234');
        expect(readComposerDraftTs(7)).toBe(1234);
    });

    test('returns 0 when storage is unavailable', () => {
        jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
        expect(readComposerDraftTs(7)).toBe(0);
    });
});
