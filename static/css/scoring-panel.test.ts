// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import fs from 'fs';
import path from 'path';

describe('How ranking works disclosure (issue #478, supersedes #387 scoring panel)', () => {
    const html = fs.readFileSync(path.join(__dirname, '..', '..', 'templates', 'crank', 'index.html'), 'utf8');

    it('renders collapsed by default (no open attribute) so results get the full width', () => {
        const match = html.match(/<details\s+class="how-ranking-works[^"]*"[^>]*>/);
        expect(match).not.toBeNull();
        expect(match![0]).not.toMatch(/\sopen\b/);
        expect(match![0]).toContain('data-testid="how-ranking-works"');
    });

    it('no longer ships the side-column scoring panel or inline preset form', () => {
        expect(html).not.toContain('scoring-panel');
        expect(html).not.toContain('submitForm');
        expect(html).not.toContain('id="algorithm_id"');
    });
});
