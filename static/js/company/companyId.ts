// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.

const MAX_WIRE_ID = 2 ** 31 - 1;

/**
 * A company id that is safe to put in a URL path: a positive integer the
 * server's id column can hold. Ids arrive from `?company=`, the
 * `crank:company-open` event and the assistant's typed target without having
 * to be a list row, so nothing is requested for anything else (issue #486).
 */
export function isCompanyId(value: unknown): value is number {
    return typeof value === 'number' && Number.isInteger(value) && value > 0 && value <= MAX_WIRE_ID;
}

/** Parses a raw query value or event detail; null when it is not a company id. */
export function parseCompanyId(raw: unknown): number | null {
    if (typeof raw === 'number') {
        return isCompanyId(raw) ? raw : null;
    }
    if (typeof raw !== 'string' || !/^[1-9]\d{0,9}$/.test(raw)) {
        return null;
    }
    const id = Number(raw);
    return isCompanyId(id) ? id : null;
}
