// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import type {EditorField} from './api';
import {buildPatch, emptyDraft, isDirty, toDraftValue} from './patch';

const field = (over: Partial<EditorField>): EditorField => ({
    path: 'compensation.minimum_salary', label: 'Minimum base salary', type: 'int', value: 0, set: false,
    supported: true, hard: false, hard_locked: false, editable: true, ...over,
});

describe('patch conversion', () => {
    test('toDraftValue per type', () => {
        expect(toDraftValue(field({}))).toBe('');
        expect(toDraftValue(field({set: true, value: 150000}))).toBe('150000');
        expect(toDraftValue(field({type: 'bool', value: true}))).toBe(true);
        expect(toDraftValue(field({type: 'str_list', value: ['a', 'b']}))).toBe('a, b');
        expect(toDraftValue(field({type: 'str_list', value: ['remote'], choices: ['remote']}))).toEqual(['remote']);
        expect(toDraftValue(field({type: 'str_list', value: null}))).toBe('');
    });

    test('no changes yields no patch and not dirty', () => {
        const fields = [field({})];
        expect(buildPatch(fields, {}, emptyDraft())).toBeNull();
        expect(isDirty(fields, {values: {'compensation.minimum_salary': ''}, hard: {}})).toBe(false);
    });

    test('numeric, invalid text, clear, bool and list edits', () => {
        const fields = [
            field({}),
            field({path: 'a.float', type: 'float'}),
            field({path: 'b.text', type: 'str', set: true, value: 'x'}),
            field({path: 'c.flag', type: 'bool'}),
            field({path: 'd.list', type: 'str_list', set: true, value: ['a']}),
            field({path: 'e.picks', type: 'str_list', choices: ['x', 'y'], value: ['x']}),
            field({path: 'f.list2', type: 'str_list', set: true, value: ['q']}),
        ];
        const patch = buildPatch(fields, {}, {
            values: {
                'compensation.minimum_salary': '150000',
                'a.float': 'abc',
                'b.text': '  ',
                'c.flag': true,
                'd.list': 'one, two,, ',
                'e.picks': ['x', 'y'],
                'f.list2': '',
            },
            hard: {},
        });
        expect(patch).toEqual({
            set: {
                'compensation.minimum_salary': 150000,
                'a.float': 'abc',
                'c.flag': true,
                'd.list': ['one', 'two'],
                'e.picks': ['x', 'y'],
            },
            remove: {'b.text': null, 'f.list2': null},
        });
        expect(buildPatch([fields[5]], {}, {values: {'e.picks': []}, hard: {}})).toEqual({remove: {'e.picks': null}});
    });

    test('requirement toggle builds a whole importance map and skips locked fields', () => {
        const fields = [field({}), field({path: 'locked', hard: true, hard_locked: true})];
        const draft = {values: {}, hard: {'compensation.minimum_salary': true, locked: false}};
        expect(isDirty(fields, draft)).toBe(true);
        expect(buildPatch(fields, {importance: {other: 1}}, draft)).toEqual({
            set: {importance: {other: 1, 'compensation.minimum_salary': 1}},
        });
        expect(buildPatch([field({hard: true})], {}, {values: {}, hard: {'compensation.minimum_salary': false}}))
            .toEqual({set: {importance: {'compensation.minimum_salary': 0}}});
    });
});
