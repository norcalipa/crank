// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {FUNDING_ROUND_LABELS, RTO_POLICY_LABELS, fundingRoundLabel, rtoPolicyLabel} from './labels';

describe('shared funding-round and RTO labels (issue #473)', () => {
    test('known codes map to the model words', () => {
        expect(fundingRoundLabel('X')).toBe('Series G or Later');
        expect(fundingRoundLabel('O')).toBe('Other Private');
        expect(fundingRoundLabel('P')).toBe('Public');
        expect(rtoPolicyLabel('O')).toBe('In-Office');
        expect(Object.keys(FUNDING_ROUND_LABELS)).toHaveLength(10);
        expect(Object.keys(RTO_POLICY_LABELS)).toEqual(['R', 'H', 'O']);
    });

    test('unknown codes pass through and empty codes use the fallback', () => {
        expect(fundingRoundLabel('Q')).toBe('Q');
        expect(rtoPolicyLabel('Z', 'Unknown')).toBe('Z');
        expect(fundingRoundLabel('')).toBe('');
        expect(fundingRoundLabel(null, 'Unknown')).toBe('Unknown');
        expect(rtoPolicyLabel(undefined, 'Unknown')).toBe('Unknown');
        // An inherited property name is not a code.
        expect(fundingRoundLabel('toString')).toBe('toString');
        expect(rtoPolicyLabel('constructor')).toBe('constructor');
    });
});
