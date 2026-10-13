// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {isCompanyId, parseCompanyId} from './companyId';

describe('company ids (issue #486)', () => {
    test.each([1, 7, 2147483647])('%p is an id', (value) => {
        expect(isCompanyId(value)).toBe(true);
        expect(parseCompanyId(value)).toBe(value);
        expect(parseCompanyId(String(value))).toBe(value);
    });

    test.each([0, -1, 1.5, Number.NaN, Infinity, 2 ** 31, '1', null, undefined, {}])('%p is not an id', (value) => {
        expect(isCompanyId(value)).toBe(false);
    });

    test.each(['abc', '1.5', '-1', '0', '1/../2', '', '1e3', ' 1', '1 ', '01', '+1', '0x10', '99999999999', '١٢'])(
        'the text %p is not an id', (raw) => {
            expect(parseCompanyId(raw)).toBeNull();
        });

    test.each([1.5, 0, -3, null, undefined, {}, ['1']])('the value %p is not parsed into an id', (raw) => {
        expect(parseCompanyId(raw)).toBeNull();
    });
});
