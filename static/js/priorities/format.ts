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

function humanizeToken(token: string): string {
    if (/[A-Z]/.test(token)) return token;
    return token.replace(/_/g, ' ').replace(/\b[a-z]/g, (c) => c.toUpperCase());
}

function formatNumber(path: string, n: number): string {
    const grouped = n.toLocaleString('en-US');
    if (MONEY_PATH.test(path)) return `$${grouped}`;
    return PERCENT_PATH.test(path) ? `${grouped}%` : grouped;
}

/** Chip rendering of the server's display string: numbers get grouping, a symbol and a unit; enum words read as labels. */
export function chipValueLabel(path: string, display: string): string {
    if (/^-?\d+(\.\d+)?$/.test(display)) return formatNumber(path, Number(display));
    return display.split(', ').map(humanizeToken).join(', ');
}

/** Human rendering of a preference value; pass the path for the same money/percent/label formatting the chips use. */
export function preferenceValueLabel(value: unknown, path?: string): string {
    if (value === null || value === undefined) return 'Not set';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (typeof value === 'string') {
        if (value === '') return 'Not set';
        return path === undefined ? value : humanizeToken(value);
    }
    if (typeof value === 'number') {
        if (!Number.isFinite(value)) return String(value);
        return path === undefined ? value.toLocaleString('en-US') : formatNumber(path, value);
    }
    if (Array.isArray(value)) {
        return value.length ? value.map((v) => preferenceValueLabel(v, path)).join(', ') : 'None';
    }
    try {
        return JSON.stringify(value);
    } catch {
        return String(value);
    }
}
