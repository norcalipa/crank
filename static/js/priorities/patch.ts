// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Draft ↔ patch conversion for the inline editor (issue #480). Field types
// and choices come from the server's editor metadata; nothing here restates
// the preference schema.

import type {EditorField, PreferencePatch} from './api';

export type DraftValue = string | boolean | string[];

export interface Draft {
    values: Record<string, DraftValue>;
    // path -> true (requirement) / false (preference)
    hard: Record<string, boolean>;
}

export const emptyDraft = (): Draft => ({values: {}, hard: {}});

export function toDraftValue(field: EditorField): DraftValue {
    const value = field.value;
    if (field.type === 'bool') {
        return value === true;
    }
    if (field.type === 'str_list') {
        const list = Array.isArray(value) ? value.map(String) : [];
        return field.choices ? list : list.join(', ');
    }
    if (!field.set || value === null || value === undefined) {
        return '';
    }
    return String(value);
}

function sameDraft(a: DraftValue, b: DraftValue): boolean {
    return JSON.stringify(a) === JSON.stringify(b);
}

function parseList(raw: string): string[] {
    return raw.split(',').map((item) => item.trim()).filter(Boolean);
}

// Sends numeric text as a number when it parses, otherwise as the raw string
// so the server reports a field error rather than the client guessing.
function parseScalar(field: EditorField, raw: string): unknown {
    if (field.type === 'int' || field.type === 'float') {
        const n = Number(raw);
        return raw.trim() !== '' && Number.isFinite(n) ? n : raw;
    }
    return raw;
}

export function isDirty(fields: EditorField[], draft: Draft): boolean {
    return fields.some((field) => {
        const edited = draft.values[field.path];
        if (edited !== undefined && !sameDraft(edited, toDraftValue(field))) {
            return true;
        }
        const hard = draft.hard[field.path];
        return hard !== undefined && hard !== field.hard;
    });
}

export function buildPatch(
    fields: EditorField[], preferences: Record<string, unknown>, draft: Draft,
): PreferencePatch | null {
    const set: Record<string, unknown> = {};
    const remove: Record<string, unknown> = {};
    for (const field of fields) {
        const edited = draft.values[field.path];
        if (edited === undefined || sameDraft(edited, toDraftValue(field))) {
            continue;
        }
        if (field.type === 'bool') {
            set[field.path] = edited;
        } else if (field.type === 'str_list') {
            const list = Array.isArray(edited) ? edited : parseList(String(edited));
            if (list.length === 0) {
                remove[field.path] = null;
            } else {
                set[field.path] = list;
            }
        } else if (String(edited).trim() === '') {
            remove[field.path] = null;
        } else {
            set[field.path] = parseScalar(field, String(edited));
        }
    }
    const current = (preferences.importance && typeof preferences.importance === 'object'
        ? preferences.importance : {}) as Record<string, number>;
    const importance = {...current};
    let importanceChanged = false;
    for (const field of fields) {
        const hard = draft.hard[field.path];
        if (hard === undefined || hard === field.hard || field.hard_locked) {
            continue;
        }
        importance[field.path] = hard ? 1.0 : 0.0;
        importanceChanged = true;
    }
    if (importanceChanged) {
        set.importance = importance;
    }
    const patch: PreferencePatch = {};
    if (Object.keys(set).length) patch.set = set;
    if (Object.keys(remove).length) patch.remove = remove;
    return patch.set || patch.remove ? patch : null;
}
