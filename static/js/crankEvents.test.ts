// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Invariant (issue #484 threat model): every `crank:` CustomEvent that the
// app dispatches has a listener, and assistant actions dispatch none. An event
// nobody hears is dead code; one that a click can raise on any page is an
// unreviewed control path.
import * as fs from 'fs';
import * as path from 'path';

const ROOT = __dirname;

function sourceFiles(dir: string): string[] {
    return fs.readdirSync(dir, {withFileTypes: true}).flatMap((entry) => {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            return entry.name === 'e2e' || entry.name === 'node_modules' ? [] : sourceFiles(full);
        }
        return /\.(ts|tsx|js)$/.test(entry.name) && !/\.test\./.test(entry.name) ? [full] : [];
    });
}

const files = sourceFiles(ROOT);
const text = new Map(files.map((file) => [file, fs.readFileSync(file, 'utf8')]));

const constants = new Map<string, string>();
for (const source of text.values()) {
    for (const match of source.matchAll(/const\s+([A-Za-z0-9_]+)\s*=\s*['"](crank:[^'"]+)['"]/g)) {
        constants.set(match[1], match[2]);
    }
}

function resolveEvents(pattern: RegExp): {names: Set<string>; unresolved: string[]} {
    const names = new Set<string>();
    const unresolved: string[] = [];
    for (const [file, source] of text) {
        for (const match of source.matchAll(pattern)) {
            const arg = match[1];
            const literal = /^['"](crank:[^'"]+)['"]$/.exec(arg);
            const value = literal ? literal[1] : constants.get(arg);
            if (value) {
                names.add(value);
            } else if (literal === null && !/^['"]/.test(arg) && !constants.has(arg) && /EVENT/.test(arg)) {
                unresolved.push(`${path.relative(ROOT, file)}: ${arg}`);
            }
        }
    }
    return {names, unresolved};
}

describe('crank: CustomEvents', () => {
    const dispatched = resolveEvents(/new CustomEvent\(\s*([^,)\s]+)/g);
    const listened = resolveEvents(/addEventListener\(\s*([^,)\s]+)/g);

    test('the scan sees the known events', () => {
        expect(Array.from(dispatched.names)).toEqual(expect.arrayContaining([
            'crank:company-open', 'crank:suggest-company', 'crank:assistant-open',
            'crank:private-state-purged', 'crank:auth-hydrated',
        ]));
        expect(dispatched.unresolved).toEqual([]);
        expect(listened.unresolved).toEqual([]);
    });

    test('every dispatched crank: event has a listener', () => {
        const orphans = Array.from(dispatched.names).filter((name) => !listened.names.has(name));
        expect(orphans).toEqual([]);
    });

    test('assistant actions raise no window event', () => {
        const source = text.get(path.join(ROOT, 'chat', 'AssistantActions.tsx')) as string;
        expect(source).toBeDefined();
        expect(source).not.toMatch(/CustomEvent|dispatchEvent/);
    });
});
