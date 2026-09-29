// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as fs from 'fs';
import * as path from 'path';

// modalIsolation.ts is a module-level refcount that must stay a single
// instance inside the `main` entry graph (issue #472/#479). jobmatch.tsx is a
// separate webpack entry, so importing it (even transitively) would fork the
// refcount and break the one-blocking-surface rule.
const JS_ROOT = path.resolve(__dirname, '..');
const IMPORT_RE = /(?:import|export)\s[^'"]*?from\s+['"](\.[^'"]+)['"]|import\(\s*['"](\.[^'"]+)['"]\s*\)|import\s+['"](\.[^'"]+)['"]/g;

function resolveModule(from: string, spec: string): string | null {
    const base = path.resolve(path.dirname(from), spec);
    for (const candidate of [base, `${base}.ts`, `${base}.tsx`, `${base}.js`, path.join(base, 'index.ts'), path.join(base, 'index.tsx')]) {
        if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
            return candidate;
        }
    }
    return null;
}

function reachable(entry: string): Set<string> {
    const seen = new Set<string>();
    const queue = [entry];
    while (queue.length) {
        const file = queue.pop() as string;
        if (seen.has(file)) continue;
        seen.add(file);
        const source = fs.readFileSync(file, 'utf8');
        for (const match of source.matchAll(IMPORT_RE)) {
            const resolved = resolveModule(file, match[1] || match[2] || match[3]);
            if (resolved) queue.push(resolved);
        }
    }
    return seen;
}

describe('bundle isolation', () => {
    test('jobmatch.tsx never reaches modalIsolation', () => {
        const graph = reachable(path.join(JS_ROOT, 'jobmatch.tsx'));
        expect(graph.size).toBeGreaterThan(1);
        const names = Array.from(graph).map((file) => path.basename(file));
        expect(names).not.toContain('modalIsolation.ts');
    });

    test('the walker does see modalIsolation from the main-bundle popup', () => {
        const graph = reachable(path.join(JS_ROOT, 'OrganizationDetailsPopup.tsx'));
        expect(Array.from(graph).map((file) => path.basename(file))).toContain('modalIsolation.ts');
    });
});
