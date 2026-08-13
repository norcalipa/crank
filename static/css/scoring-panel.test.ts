// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import fs from 'fs';
import path from 'path';

describe('scoring panel details element regression (issue #387)', () => {
    const templatePath = path.join(__dirname, '..', '..', 'templates', 'crank', 'index.html');

    it('has the open attribute on the scoring-panel details element', () => {
        const html = fs.readFileSync(templatePath, 'utf8');

        // The <details class="card scoring-panel"> must have the `open` attribute
        // so the element contributes non-zero height on desktop. Without `open`,
        // a closed <details> collapses to 0 height even when CSS forces its
        // child content to display:block (the root cause of issue #387).
        expect(html).toMatch(/<details\s+class="card scoring-panel"\s+open>/);
    });

    it('would have caught the height-0 bug: details without open yields no renderable content box', () => {
        const html = fs.readFileSync(templatePath, 'utf8');

        // Extract the details tag
        const match = html.match(/<details\s+class="card scoring-panel"[^>]*>/);
        expect(match).not.toBeNull();

        const detailsTag = match![0];
        // The open attribute must be present — without it the details element
        // has 0 height on desktop despite display:block on children.
        expect(detailsTag).toContain('open');
    });
});
