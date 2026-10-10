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

export interface SummaryChip {
    path: string;
    label: string;
    display: string;
    hard: boolean;
    supported: boolean;
    items?: string[];
}

// The lead names a second requirement only while it stays this short ("Requires: $150,000, Remote");
// a longer one is counted with the rest, so the row does not cut the value it names.
const SUMMARY_LEAD_CHARS = 26;

// A value that says nothing alone ("Yes", "2") is named by its field; "Remote" or "$150,000" stands for itself.
// Returned as the text that may be cut and the end that may not (the value after a field name).
function summaryValue(chip: SummaryChip, currency?: unknown, choicePaths?: ReadonlySet<string>): [string, string] {
    const value = chipValueLabel(chip.path, chip.display, currency, chip.items, choicePaths?.has(chip.path));
    if (value === 'Yes') return [chip.label, ''];
    // A list is as long as the user made it: with its field's name it stays in the part that may be cut.
    if ((chip.items?.length ?? 0) > 1) return [`${chip.label}: ${value}`, ''];
    // A bare number and a number with its unit ("0.5%") say little without their field.
    return /^(No|-?[\d,.]+\s?(%|[A-Za-z]+)?)$/.test(value) ? [chip.label, `: ${value}`] : [value, ''];
}

export interface SummaryParts {
    // Truncates first: "Requires: $150,000, Remote".
    lead: string;
    // The end of the lead that never truncates: the value after a field name (": 2").
    keep: string;
    sep: string;
    // Never truncates: the counts ("+2 \u00b7 6 preferences").
    tail: string;
}

/** The collapsed row's summary in the parts it is laid out in; joined they read as `prioritiesSummary`. */
export function prioritiesSummaryParts(chips: SummaryChip[], currency?: unknown, choicePaths?: ReadonlySet<string>): SummaryParts {
    const required = chips.filter((chip) => chip.hard)
        .sort((a, b) => Number(!a.supported) - Number(!b.supported));
    const preferences = chips.length - required.length;
    const counted = preferences > 0 ? `${preferences} ${preferences === 1 ? 'preference' : 'preferences'}` : '';
    if (required.length === 0) return {lead: counted, keep: '', sep: '', tail: ''};
    const [first, second] = required.map((chip) => summaryValue(chip, currency, choicePaths));
    let lead = `Requires: ${first[0]}`;
    let keep = first[1];
    let named = 1;
    if (second && `${lead}${keep}, ${second.join('')}`.length <= SUMMARY_LEAD_CHARS) {
        lead = `${lead}${keep}, ${second[0]}`;
        keep = second[1];
        named = 2;
    }
    const more = required.length - named;
    if (more === 0) return counted ? {lead, keep, sep: ' \u00b7 ', tail: counted} : {lead, keep, sep: '', tail: ''};
    // The comma belongs to what it follows, so a cut lead is never followed by a stray ", ".
    const tail = [`+${more}`, ...(counted ? [counted] : [])].join(' \u00b7 ');
    return keep ? {lead, keep: `${keep},`, sep: ' ', tail} : {lead: `${lead},`, keep, sep: ' ', tail};
}

/** One-line summary for the collapsed sidebar row: "Requires: $150,000, Remote, +1 · 5 preferences".
 *  Values read as on the chips, in chip order (criteria the matcher uses first). Empty when nothing is saved. */
export function prioritiesSummary(chips: SummaryChip[], currency?: unknown, choicePaths?: ReadonlySet<string>): string {
    const {lead, keep, sep, tail} = prioritiesSummaryParts(chips, currency, choicePaths);
    return `${lead}${keep}${sep}${tail}`;
}
