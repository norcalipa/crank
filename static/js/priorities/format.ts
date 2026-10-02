// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Display helpers shared by the priorities UI and the chat notices (issue #480).

export interface PreferenceChange {
    path: string;
    old: unknown;
    new: unknown;
}

/** "compensation.minimum_salary" → "Compensation › minimum salary". */
export function preferencePathLabel(path: string): string {
    return path
        .split('.')
        .map((segment, i) => {
            const words = segment.replace(/_/g, ' ');
            return i === 0 ? words.charAt(0).toUpperCase() + words.slice(1) : words;
        })
        .join(' › ');
}

const MONEY_PATH = /salary|compensation$/;
const PERCENT_PATH = /_percent$/;

export function humanizeToken(token: string): string {
    if (/[A-Z]/.test(token)) return token;
    return token.replace(/_/g, ' ').replace(/\b[a-z]/g, (c) => c.toUpperCase());
}

/** "$" for USD or an unset currency, otherwise the saved code ("EUR "). */
export function currencyPrefix(currency?: unknown): string {
    return typeof currency !== 'string' || currency === '' || currency === 'USD' ? '$' : `${currency} `;
}

function formatNumber(path: string, n: number, currency?: unknown): string {
    const grouped = n.toLocaleString('en-US');
    if (MONEY_PATH.test(path)) return `${currencyPrefix(currency)}${grouped}`;
    return PERCENT_PATH.test(path) ? `${grouped}%` : grouped;
}

/** Chip rendering of the server's display string: numbers get grouping, a symbol and a unit; enum words read as labels.
 *  `items` (list fields) keeps each saved entry whole, so an entry containing a comma is never read as two. */
export function chipValueLabel(
    path: string, display: string, currency?: unknown, items?: string[], choice = false,
): string {
    if (items) {
        const labels = choice ? items.map(humanizeToken) : items;
        return labels.join(labels.some((item) => item.includes(',')) ? '; ' : ', ');
    }
    if (/^-?\d+(\.\d+)?$/.test(display)) return formatNumber(path, Number(display), currency);
    return display.split(', ').map(humanizeToken).join(', ');
}

/** Human rendering of a preference value; pass the path for the same money/percent/label formatting the chips use. */
export function preferenceValueLabel(value: unknown, path?: string, currency?: unknown): string {
    if (value === null || value === undefined) return 'Not set';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (typeof value === 'string') {
        if (value === '') return 'Not set';
        // Enum tokens read as labels; free text (it has spaces) is shown as typed.
        return path === undefined || /\s/.test(value) ? value : humanizeToken(value);
    }
    if (typeof value === 'number') {
        if (!Number.isFinite(value)) return String(value);
        return path === undefined ? value.toLocaleString('en-US') : formatNumber(path, value, currency);
    }
    if (Array.isArray(value)) {
        return value.length ? value.map((v) => preferenceValueLabel(v, path, currency)).join(', ') : 'None';
    }
    if (typeof value === 'object') {
        const entries = Object.entries(value as Record<string, unknown>);
        if (entries.length === 0) return 'None';
        return entries.map(([key, v]) => (
            `${preferencePathLabel(key)}: ${path === 'importance' ? importanceLabel(v) : v !== null && typeof v === 'object' ? String(v) : preferenceValueLabel(v)}`
        )).join(', ');
    }
    return String(value);
}

/** A criterion's weight as the user sets it: 1.0 is a requirement, anything lower a preference. */
export function importanceLabel(value: unknown): string {
    if (typeof value !== 'number') return 'Preference';
    return value >= 1 ? 'Requirement' : 'Preference';
}

export interface DisplayChange {
    key: string;
    label: string;
    old: unknown;
    new: unknown;
    path: string;
    importance?: boolean;
}

/** Expands a whole-map diff (importance, priority weights) into one row per changed key, so no JSON reaches the screen. */
export function expandChanges(changes: PreferenceChange[], labels?: Record<string, string>): DisplayChange[] {
    const rows: DisplayChange[] = [];
    for (const change of changes) {
        const isMap = (v: unknown) => v !== null && typeof v === 'object' && !Array.isArray(v);
        if (!isMap(change.old) && !isMap(change.new)) {
            rows.push({
                key: change.path, path: change.path, old: change.old, new: change.new,
                label: labels?.[change.path] || preferencePathLabel(change.path),
            });
            continue;
        }
        const before = (isMap(change.old) ? change.old : {}) as Record<string, unknown>;
        const after = (isMap(change.new) ? change.new : {}) as Record<string, unknown>;
        const group = labels?.[change.path] || preferencePathLabel(change.path);
        const keys = Array.from(new Set([...Object.keys(before), ...Object.keys(after)]))
            .filter((key) => JSON.stringify(before[key]) !== JSON.stringify(after[key]));
        for (const key of keys) {
            const isImportance = change.path === 'importance';
            const name = labels?.[key] || preferencePathLabel(key);
            rows.push({
                key: `${change.path}.${key}`, path: change.path, old: before[key], new: after[key],
                label: isImportance ? `${name} importance` : `${group} \u203a ${name}`, importance: isImportance,
            });
        }
    }
    return rows;
}
