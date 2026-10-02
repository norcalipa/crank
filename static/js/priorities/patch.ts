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

// Booleans are tri-state on the server: '' (not set) / 'true' / 'false'.
export function toDraftValue(field: EditorField): DraftValue {
    const value = field.value;
    if (field.type === 'bool') {
        return field.set && typeof value === 'boolean' ? String(value) : '';
    }
    if (field.type === 'str_list') {
        return Array.isArray(value) ? value.map(String) : [];
    }
    if (!field.set || value === null || value === undefined) {
        return '';
    }
    return String(value);
}

function sameDraft(a: DraftValue, b: DraftValue): boolean {
    return JSON.stringify(a) === JSON.stringify(b);
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
            if (edited === '') {
                remove[field.path] = null;
            } else {
                set[field.path] = edited === 'true';
            }
        } else if (field.type === 'str_list') {
            // Each entry is saved exactly as entered: commas inside an entry are never split.
            // An emptied list is a `set` of []: the server's `remove` for a list must name items.
            set[field.path] = Array.isArray(edited) ? edited : [];
        } else if (String(edited).trim() === '') {
            remove[field.path] = null;
        } else {
            set[field.path] = parseScalar(field, String(edited));
        }
    }
    const importance = importanceFor(fields, preferences, draft);
    if (importance) {
        set.importance = importance;
    }
    const patch: PreferencePatch = {};
    if (Object.keys(set).length) patch.set = set;
    if (Object.keys(remove).length) patch.remove = remove;
    return patch.set || patch.remove ? patch : null;
}

function importanceFor(
    fields: EditorField[], preferences: Record<string, unknown>, draft: Draft,
): Record<string, number> | null {
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
    return importanceChanged ? importance : null;
}

const asDraftText = (field: EditorField, value: unknown): DraftValue => {
    if (field.type === 'bool') return value === true ? 'true' : 'false';
    if (field.type === 'str_list') return Array.isArray(value) ? value.map(String) : [];
    return value === null || value === undefined ? '' : String(value);
};

function editableField(byPath: Map<string, EditorField>, path: string): EditorField | undefined {
    const field = byPath.get(path);
    return field && field.type !== 'float_map' ? field : undefined;
}

/** Loads a proposed patch (the assistant's) into editor values, so Edit starts from the proposal. */
export function patchToDraft(fields: EditorField[], patch: PreferencePatch | null | undefined): Draft {
    const draft = emptyDraft();
    if (!patch) return draft;
    const byPath = new Map(fields.map((field) => [field.path, field]));
    for (const [path, value] of Object.entries(patch.set || {})) {
        const field = editableField(byPath, path);
        if (field) {
            draft.values[path] = asDraftText(field, value);
        } else if (path === 'importance' && value && typeof value === 'object') {
            for (const [key, weight] of Object.entries(value as Record<string, unknown>)) {
                const target = byPath.get(key);
                if (target && typeof weight === 'number' && (weight >= 1) !== target.hard) {
                    draft.hard[key] = weight >= 1;
                }
            }
        }
    }
    for (const [path, value] of Object.entries(patch.remove || {})) {
        const field = editableField(byPath, path);
        if (!field) continue;
        if (field.type === 'str_list' && Array.isArray(value)) {
            // Dropping named entries keeps the rest of the saved list.
            const dropped = new Set(value.map(String));
            const saved = Array.isArray(field.value) ? field.value.map(String) : [];
            draft.values[path] = saved.filter((item) => !dropped.has(item));
        } else {
            draft.values[path] = field.type === 'str_list' ? [] : '';
        }
    }
    return draft;
}

const isMap = (value: unknown) => value !== null && typeof value === 'object' && !Array.isArray(value);
const isPriorityKey = (path: string) => path === 'priorities' || path.startsWith('priorities.');

/** Whether the editor can show every part of a proposed patch: whole-subtree values and priority weights it cannot. */
export function patchFitsEditor(patch: PreferencePatch | null | undefined): boolean {
    if (!patch) return true;
    const setFits = Object.entries(patch.set || {}).every(
        ([path, value]) => path === 'importance' || (!isMap(value) && !isPriorityKey(path)),
    );
    return setFits && Object.keys(patch.remove || {}).every((path) => !isPriorityKey(path));
}

/** Paths the draft edits whose saved value or importance differs between two snapshots of the document. */
export function conflictingPaths(before: EditorField[], after: EditorField[], draft: Draft): string[] {
    const prior = new Map(before.map((field) => [field.path, field]));
    return after.filter((field) => {
        const old = prior.get(field.path);
        const edited = draft.values[field.path] !== undefined || draft.hard[field.path] !== undefined;
        return old && edited && (!sameDraft(toDraftValue(old), toDraftValue(field)) || old.hard !== field.hard);
    }).map((field) => field.path);
}
