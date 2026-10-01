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

/** Human rendering of a preference value. */
export function preferenceValueLabel(value: unknown): string {
    if (value === null || value === undefined) return 'Not set';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (typeof value === 'string') return value === '' ? 'Not set' : value;
    if (typeof value === 'number') {
        return Number.isFinite(value) ? value.toLocaleString('en-US') : String(value);
    }
    if (Array.isArray(value)) {
        return value.length ? value.map(preferenceValueLabel).join(', ') : 'None';
    }
    try {
        return JSON.stringify(value);
    } catch {
        return String(value);
    }
}
