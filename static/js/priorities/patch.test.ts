// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import type {EditorField} from './api';
import {buildPatch, patchFitsEditor, conflictingPaths, emptyDraft, isDirty, patchToDraft, toDraftValue} from './patch';

const field = (over: Partial<EditorField>): EditorField => ({
    path: 'compensation.minimum_salary', label: 'Minimum base salary', type: 'int', value: 0, set: false,
    supported: true, hard: false, hard_locked: false, editable: true, ...over,
});

describe('patch conversion', () => {
    test('toDraftValue per type', () => {
        expect(toDraftValue(field({}))).toBe('');
        expect(toDraftValue(field({set: true, value: 150000}))).toBe('150000');
        expect(toDraftValue(field({type: 'bool', value: true, set: true}))).toBe('true');
        expect(toDraftValue(field({type: 'bool', value: false, set: true}))).toBe('false');
        expect(toDraftValue(field({type: 'bool', value: null}))).toBe('');
        expect(toDraftValue(field({type: 'str_list', value: ['a', 'b, c']}))).toEqual(['a', 'b, c']);
        expect(toDraftValue(field({type: 'str_list', value: ['remote'], choices: ['remote']}))).toEqual(['remote']);
        expect(toDraftValue(field({type: 'str_list', value: null}))).toEqual([]);
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
                'c.flag': 'true',
                'd.list': ['one', 'San Francisco, CA'],
                'e.picks': ['x', 'y'],
                'f.list2': [],
            },
            hard: {},
        });
        expect(patch).toEqual({
            set: {
                'compensation.minimum_salary': 150000,
                'a.float': 'abc',
                'c.flag': true,
                'd.list': ['one', 'San Francisco, CA'],
                'e.picks': ['x', 'y'],
                'f.list2': [],
            },
            remove: {'b.text': null},
        });
        // An emptied list is a set of [] (the server's list `remove` must name items).
        expect(buildPatch([fields[5]], {}, {values: {'e.picks': []}, hard: {}})).toEqual({set: {'e.picks': []}});
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

describe('list entries keep their commas (issue #480 review)', () => {
    const list = field({path: 'exclusions.locations', type: 'str_list', set: true, value: ['New York, NY']});

    test('adding an entry keeps the saved ones whole and sends no stray tokens', () => {
        const patch = buildPatch([list], {}, {values: {'exclusions.locations': ['New York, NY', 'San Francisco, CA']}, hard: {}});
        expect(patch).toEqual({set: {'exclusions.locations': ['New York, NY', 'San Francisco, CA']}});
        expect(JSON.stringify(patch)).not.toContain('"CA"');
        expect(buildPatch([list], {}, {values: {'exclusions.locations': ['New York, NY']}, hard: {}})).toBeNull();
    });
});

describe('boolean tri-state', () => {
    const flag = field({path: 'c.flag', type: 'bool', set: true, value: true});

    test('Not set removes the value instead of saving an explicit No', () => {
        expect(buildPatch([flag], {}, {values: {'c.flag': ''}, hard: {}})).toEqual({remove: {'c.flag': null}});
        expect(buildPatch([flag], {}, {values: {'c.flag': 'false'}, hard: {}})).toEqual({set: {'c.flag': false}});
    });
});

describe('patchToDraft', () => {
    const fields = [
        field({}),
        field({path: 'x.flag', type: 'bool'}),
        field({path: 'x.list', type: 'str_list', set: true, value: ['a']}),
        field({path: 'x.text', type: 'str', set: true, value: 'y'}),
        field({path: 'work_location.modes', type: 'str_list', hard: false}),
        field({path: 'priorities', type: 'float_map'}),
    ];

    test('loads set, remove and importance changes into editor values', () => {
        const draft = patchToDraft(fields, {
            set: {
                'compensation.minimum_salary': 200000, 'x.flag': false, 'x.list': ['a', 'b, c'], 'unknown.path': 1,
                priorities: {a: 1}, importance: {'work_location.modes': 1, 'compensation.minimum_salary': 0, bogus: 1, bad: 'x'},
            },
            remove: {'x.text': null, 'x.list2': null, priorities: null},
        });
        expect(draft).toEqual({
            values: {
                'compensation.minimum_salary': '200000', 'x.flag': 'false', 'x.list': ['a', 'b, c'], 'x.text': '',
            },
            hard: {'work_location.modes': true},
        });
        expect(patchToDraft(fields, null)).toEqual(emptyDraft());
        expect(patchToDraft(fields, {remove: {'x.list': null}}).values['x.list']).toEqual([]);
        expect(patchToDraft(fields, {remove: {'x.list': ['a']}}).values['x.list']).toEqual([]);
        expect(patchToDraft(fields, {set: {'x.text': null}}).values['x.text']).toBe('');
    });

    test('a one-item removal keeps the rest of the saved list', () => {
        const list = field({path: 'culture', type: 'str_list', set: true, value: ['kind', 'open']});
        expect(patchToDraft([list], {remove: {culture: ['kind']}}).values.culture).toEqual(['open']);
        expect(patchToDraft([list], {remove: {culture: ['kind', 'open']}}).values.culture).toEqual([]);
        const unset = field({path: 'culture', type: 'str_list'});
        expect(patchToDraft([unset], {remove: {culture: ['kind']}}).values.culture).toEqual([]);
        const odd = field({path: 'culture', type: 'str_list', value: 'nope'});
        expect(patchToDraft([odd], {remove: {culture: ['kind']}}).values.culture).toEqual([]);
    });

    test('patchFitsEditor is false when part of a proposal cannot be shown', () => {
        expect(patchFitsEditor(null)).toBe(true);
        expect(patchFitsEditor({set: {'x.text': 'a', importance: {'x.text': 1}}, remove: {'x.list': ['a']}})).toBe(true);
        expect(patchFitsEditor({set: {'priorities.culture': 0.5}})).toBe(false);
        expect(patchFitsEditor({set: {work_location: {modes: []}}})).toBe(false);
        expect(patchFitsEditor({remove: {'priorities.culture': null}})).toBe(false);
        expect(patchFitsEditor({remove: {priorities: null}})).toBe(false);
    });

    test('conflictingPaths names edited criteria that changed in the latest document', () => {
        const after = [field({value: 90000, set: true}), field({path: 'x.flag', type: 'bool'}), field({path: 'work_location.modes', type: 'str_list', hard: true})];
        const before = [field({}), field({path: 'x.flag', type: 'bool'}), field({path: 'work_location.modes', type: 'str_list', hard: false})];
        const draft = {values: {'compensation.minimum_salary': '150000'}, hard: {'work_location.modes': false}};
        expect(conflictingPaths(before, after, draft)).toEqual(['compensation.minimum_salary', 'work_location.modes']);
        expect(conflictingPaths(before, after, emptyDraft())).toEqual([]);
    });
});
