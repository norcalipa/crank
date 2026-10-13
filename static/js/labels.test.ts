// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {FUNDING_ROUND_LABELS, RTO_POLICY_LABELS, formatScopeWords, fundingRoundLabel, requirementLabel, rtoPolicyLabel} from './labels';

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

describe('scope in words (issue #486, AC 19)', () => {
    test('a fact with no scope is company-wide', () => {
        expect(formatScopeWords(null)).toBe('Company-wide');
        expect(formatScopeWords(undefined)).toBe('Company-wide');
        expect(formatScopeWords({})).toBe('Company-wide');
        expect(formatScopeWords({countries: [], role_families: []})).toBe('Company-wide');
        expect(formatScopeWords({countries: [''], role_families: [null]})).toBe('Company-wide');
    });

    test('a scoped fact names its scope in words, never the raw keys', () => {
        expect(formatScopeWords({countries: ['US']})).toBe('Location: US');
        expect(formatScopeWords({countries: ['US', 'CA'], role_families: ['Engineering']}))
            .toBe('Location: US, CA; Roles: Engineering');
        expect(formatScopeWords({countries: 'US'})).toBe('Location: US');
        expect(formatScopeWords({role_families: [7]})).toBe('Roles: 7');
        const text = formatScopeWords({countries: ['US'], role_families: ['Engineering'], level: 'role'});
        expect(text).not.toMatch(/countries|role_families/);
        expect(text).toBe('Location: US; Roles: Engineering; Limited scope');
        expect(formatScopeWords({level: 'role', value: 'Engineering'})).toBe('Limited scope');
    });
});

describe('requirement labels', () => {
    test('known paths read as words, unknown ones by their last segment', () => {
        expect(requirementLabel('funding_stage')).toBe('Funding stage');
        expect(requirementLabel('work_location.max_in_office_days')).toBe('In-office days');
        expect(requirementLabel('something.new_thing')).toBe('new_thing');
        expect(requirementLabel('toString')).toBe('toString');
        expect(requirementLabel('')).toBe('');
    });
});
