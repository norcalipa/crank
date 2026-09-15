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

describe('organization listing overflow and focus (issue #409)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');

    it('does not force a vertical scrollbar on the desktop table wrapper', () => {
        const rule = popupCss.match(/\.organization-table-wrap\s*\{[^}]*\}/);
        expect(rule).not.toBeNull();

        const body = rule![0];
        expect(body).toMatch(/overflow-x:\s*auto/);
        expect(body).toMatch(/overflow-y:\s*hidden/);
        // Forbid any fixed / max vertical sizing that could reintroduce the
        // inner scrollbar, but allow `min-height` for layout stability with
        // very few rows (anchored so `min-height:` is not matched).
        expect(body).not.toMatch(/^\s*(?:height|max-height)\s*:/m);
    });

    it('does not suppress focus outlines or carets on organization listing elements', () => {
        // Scope the assertion to the organization-listing rules so a legitimate
        // focus/caret suppression elsewhere in the stylesheet cannot break it.
        const listingRules = (popupCss.match(/[^{}]+\{[^}]*\}/g) ?? []).filter((rule) =>
            /\.organization-(row|card|table|table-wrap)\b/.test(rule),
        );
        expect(listingRules.length).toBeGreaterThan(0);
        expect(listingRules.join('\n')).not.toMatch(/outline:\s*none\s*!important/);
        expect(listingRules.join('\n')).not.toMatch(/caret-color:\s*transparent\s*!important/);
    });

    it('keeps visible keyboard focus indicators for rows, cards, and the table region', () => {
        expect(popupCss).toMatch(/\.organization-row:focus-visible/);
        expect(popupCss).toMatch(/\.organization-card:focus-visible/);
        expect(popupCss).toMatch(/\.organization-table-wrap:focus-visible/);
        expect(popupCss).toMatch(/outline:\s*3px\s+solid\s+var\(--bs-warning\)/);
    });
});

describe('shared z-index layer tokens and blocking dialogs (issue #464)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');

    const tokenValue = (name: string): number | null => {
        const match = popupCss.match(new RegExp(`--${name}:\\s*(\\d+)`));
        return match ? parseInt(match[1], 10) : null;
    };

    it('defines the shared layer tokens as :root custom properties', () => {
        expect(tokenValue('z-nav-rail')).toBe(1100);
        expect(tokenValue('z-nav-toggle')).toBe(1200);
        expect(tokenValue('z-nav-overlay')).toBe(1250);
        expect(tokenValue('z-nav-drawer')).toBe(1300);
        expect(tokenValue('z-blocking-dialog')).toBe(1400);
        expect(tokenValue('z-skip-link')).toBe(2000);
    });

    it('places the blocking dialog above all background chrome but below the skip link', () => {
        const blocking = tokenValue('z-blocking-dialog');
        expect(blocking).not.toBeNull();
        expect(blocking!).toBeGreaterThan(tokenValue('z-nav-drawer')!);
        expect(blocking!).toBeLessThan(tokenValue('z-skip-link')!);
    });

    it('every fixed-position layer consumes the tokens instead of hard-coded numbers', () => {
        // Page-level stacking layers are the fixed- and absolutely-position
        // rules (the skip link is absolute). Local stacking contexts inside a
        // dialog (e.g. the sticky header's z-index: 1) are deliberately
        // excluded.
        const zRules = (popupCss.match(/[^{}]+\{[^}]*z-index:[^}]*\}/g) ?? [])
            .filter((rule) => /position:\s*(fixed|absolute)/.test(rule));
        expect(zRules.length).toBeGreaterThanOrEqual(6);
        for (const rule of zRules) {
            expect(rule).toMatch(/z-index:\s*var\(--z-/);
        }
        // The known background-chrome and dialog rules all use tokens.
        expect(popupCss).toMatch(/\.app-nav-rail\s*\{[^}]*z-index:\s*var\(--z-nav-rail\)/);
        expect(popupCss).toMatch(/\.app-nav-toggle\s*\{[^}]*z-index:\s*var\(--z-nav-toggle\)/);
        expect(popupCss).toMatch(/\.app-nav-overlay\s*\{[^}]*z-index:\s*var\(--z-nav-overlay\)/);
        expect(popupCss).toMatch(/\.app-nav-drawer\s*\{[^}]*z-index:\s*var\(--z-nav-drawer\)/);
        expect(popupCss).toMatch(/\.skip-to-content\s*\{[^}]*z-index:\s*var\(--z-skip-link\)/);
    });

    it('popup overlay and suggest modal use the blocking dialog token', () => {
        expect(popupCss).toMatch(/\.popup-overlay\s*\{[^}]*z-index:\s*var\(--z-blocking-dialog\)/);
        expect(popupCss).toMatch(/\.modal\.blocking-modal\s*\{[^}]*z-index:\s*var\(--z-blocking-dialog\)/);
    });

    it('keeps the dialog header pinned while the card body scrolls internally', () => {
        const details = popupCss.match(/\.popup-details\s*\{[^}]*\}/);
        expect(details).not.toBeNull();
        expect(details![0]).toMatch(/max-height:\s*90vh/);
        expect(details![0]).toMatch(/display:\s*flex/);
        expect(details![0]).toMatch(/flex-direction:\s*column/);
        // Whole-card scrolling must not come back: the body is the scroll
        // container, the header stays sticky above it.
        expect(details![0]).not.toMatch(/overflow-y:\s*auto/);

        const header = popupCss.match(/\.popup-details \.card-header\s*\{[^}]*\}/);
        expect(header).not.toBeNull();
        expect(header![0]).toMatch(/position:\s*sticky/);
        expect(header![0]).toMatch(/flex-shrink:\s*0/);

        const body = popupCss.match(/\.popup-details \.card-body\s*\{[^}]*\}/);
        expect(body).not.toBeNull();
        expect(body![0]).toMatch(/overflow-y:\s*auto/);
        expect(body![0]).toMatch(/min-height:\s*0/);
    });
});
