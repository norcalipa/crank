// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import fs from 'fs';
import path from 'path';

describe('desktop scoring panel styles', () => {
    it('keeps closed details content visible at the desktop breakpoint', () => {
        const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');

        expect(popupCss).toMatch(
            /@media \(min-width: 768px\)[\s\S]*?\.scoring-panel > \.scoring-panel-content \{\s*display: block;\s*\}/,
        );
    });
});
