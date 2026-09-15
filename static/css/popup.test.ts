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

    // The complete shared layer-token contract: every page-level stacking
    // layer. Seven layers, all defined as :root custom properties, all
    // asserted by exact value below and all swept for adoption above
    // (review r2).
    const LAYER_TOKENS = [
        'z-mobile-topbar',
        'z-nav-rail',
        'z-nav-toggle',
        'z-nav-overlay',
        'z-nav-drawer',
        'z-blocking-dialog',
        'z-skip-link',
    ] as const;

    // Z-index declarations intentionally NOT on the token contract: local
    // stacking contexts inside a dialog card. Each entry pins both the
    // selector and the exact value — any other raw z-index in the stylesheet
    // fails the sweep.
    const LOCAL_STACKING_CONTEXTS: Array<{selector: RegExp; value: string}> = [
        // The sticky dialog header's z-index: 1 stacks the header above the
        // card body content inside the dialog's own stacking context only.
        {selector: /^\.popup-details \.card-header$/, value: '1'},
    ];

    // Splits a stylesheet into (selector, declarations) rules. Pairing both
    // braces keeps @media preludes out of inner selectors, and stripping
    // comments first keeps commented-out braces from confusing the parse.
    const cssRules = (css: string): Array<{selector: string; declarations: string}> => {
        const stripped = css.replace(/\/\*[\s\S]*?\*\//g, '');
        return Array.from(stripped.matchAll(/([^{}]+)\{([^{}]*)\}/g),
            (match) => ({selector: match[1].trim(), declarations: match[2]}));
    };

    it('defines the shared layer tokens as :root custom properties', () => {
        expect(tokenValue('z-nav-rail')).toBe(1100);
        expect(tokenValue('z-nav-toggle')).toBe(1200);
        expect(tokenValue('z-nav-overlay')).toBe(1250);
        expect(tokenValue('z-nav-drawer')).toBe(1300);
        expect(tokenValue('z-blocking-dialog')).toBe(1400);
        expect(tokenValue('z-skip-link')).toBe(2000);
        expect(tokenValue('z-mobile-topbar')).toBe(1030);
    });

    it('places the blocking dialog above all background chrome but below the skip link', () => {
        const blocking = tokenValue('z-blocking-dialog');
        expect(blocking).not.toBeNull();
        expect(blocking!).toBeGreaterThan(tokenValue('z-nav-drawer')!);
        expect(blocking!).toBeLessThan(tokenValue('z-skip-link')!);
        // The sticky mobile topbar is below the other navigation chrome so
        // the nav toggle and drawer stay reachable above it.
        expect(tokenValue('z-nav-toggle')!).toBeGreaterThan(tokenValue('z-mobile-topbar')!);
    });

    it('every z-index declaration consumes a shared layer token instead of a hard-coded number', () => {
        // Review r2: the previous sweep only inspected rules declaring
        // `position` and `z-index` in the same block and skipped any matched
        // text mentioning `.popup-details`, so raw later overrides (a
        // follow-up `.app-mobile-topbar { z-index: 1500 }` block with no
        // `position` declaration) and other page-level positioned layers
        // (`.app-messages { position: relative; z-index: 1500 }`) escaped it
        // while the suite reported complete token adoption. Sweep EVERY rule
        // that declares z-index — whatever its `position` — and require a
        // shared token or an explicitly whitelisted dialog-local stacking
        // context with its exact value.
        const rules = cssRules(popupCss).filter((rule) => /z-index:/.test(rule.declarations));
        expect(rules.length).toBeGreaterThanOrEqual(9);

        const tokenPattern = `z-index:\\s*var\\(--(?:${LAYER_TOKENS.join('|')})\\)`;
        for (const rule of rules) {
            const whitelisted = LOCAL_STACKING_CONTEXTS.some((context) =>
                context.selector.test(rule.selector)
                && new RegExp(`z-index:\\s*${context.value}\\s*;`).test(rule.declarations));
            if (whitelisted) {
                continue;
            }
            expect(rule.declarations).toMatch(new RegExp(tokenPattern));
        }

        // Every one of the seven layer tokens is actually consumed, so the
        // contract cannot drift: removing a consumer breaks the sweep just
        // like adding a raw value does.
        for (const token of LAYER_TOKENS) {
            expect(popupCss).toMatch(new RegExp(`var\\(--${token}\\)`));
        }

        // The known background-chrome and dialog rules map to their tokens.
        expect(popupCss).toMatch(/\.app-nav-rail\s*\{[^}]*z-index:\s*var\(--z-nav-rail\)/);
        expect(popupCss).toMatch(/\.app-nav-toggle\s*\{[^}]*z-index:\s*var\(--z-nav-toggle\)/);
        expect(popupCss).toMatch(/\.app-nav-overlay\s*\{[^}]*z-index:\s*var\(--z-nav-overlay\)/);
        expect(popupCss).toMatch(/\.app-nav-drawer\s*\{[^}]*z-index:\s*var\(--z-nav-drawer\)/);
        expect(popupCss).toMatch(/\.app-mobile-topbar\s*\{[^}]*z-index:\s*var\(--z-mobile-topbar\)/);
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

describe('blocking-dialog close targets and row focus perimeter (issue #464 r2)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');

    it('gives the popup Close control a 44×44px touch target with a compact glyph', () => {
        const rule = popupCss.match(/\.popup-details \.card-header \.btn-close\s*\{[^}]*\}/);
        expect(rule).not.toBeNull();
        for (const decl of ['inline-size: 44px', 'block-size: 44px', 'min-inline-size: 44px', 'min-block-size: 44px']) {
            expect(rule![0]).toContain(decl);
        }
        // The glyph stays visually compact inside the larger hit box.
        expect(rule![0]).toMatch(/background-size:\s*1rem/);
        expect(rule![0]).toMatch(/padding:\s*0/);
        // The keyboard focus indicator survives the larger control.
        expect(popupCss).toMatch(
            /\.popup-details \.card-header \.btn-close:focus-visible\s*\{[^}]*outline:\s*3px solid var\(--bs-warning\)/,
        );
    });

    it('keeps the suggest-modal Close target on the same 44px contract', () => {
        const rule = popupCss.match(/\.modal\.blocking-modal \.btn-close\s*\{[^}]*\}/);
        expect(rule).not.toBeNull();
        for (const decl of ['inline-size: 44px', 'block-size: 44px', 'min-inline-size: 44px', 'min-block-size: 44px']) {
            expect(rule![0]).toContain(decl);
        }
        expect(rule![0]).toMatch(/background-size:\s*1rem/);
        expect(popupCss).toMatch(
            /\.modal\.blocking-modal \.btn-close:focus-visible\s*\{[^}]*outline:\s*3px solid var\(--bs-warning\)/,
        );
    });

    it('compensates the taller Close target by shrinking the header padding-block', () => {
        // The 44px control must not balloon the pinned header: the container
        // (not the target) absorbs the height.
        const headerRules = popupCss.match(/\.popup-details \.card-header\s*\{[^}]*\}/g) ?? [];
        expect(headerRules.join('\n')).toMatch(/padding-block:\s*\.375rem/);
    });

    it('draws the focused row perimeter on cells so the wrapper cannot clip it', () => {
        // The `tr` outline would clip at the rounded, horizontally scrollable
        // wrapper; cells carry the ring instead.
        expect(popupCss).toMatch(
            /\.organization-row:focus-visible\s*\{\s*outline:\s*none;/,
        );
        expect(popupCss).toMatch(
            /\.organization-row:focus-visible > \*\s*\{[^}]*inset 0 3px 0 var\(--bs-warning\)[^}]*inset 0 -3px 0 var\(--bs-warning\)/,
        );
        expect(popupCss).toMatch(
            /\.organization-row:focus-visible > :first-child\s*\{[^}]*inset 3px 0 0 var\(--bs-warning\)/,
        );
        expect(popupCss).toMatch(
            /\.organization-row:focus-visible > :last-child\s*\{[^}]*inset -3px 0 0 var\(--bs-warning\)/,
        );
    });

    it('keeps the complete outline ring on the mobile organization card', () => {
        const grouped = popupCss.match(
            /\.organization-card:focus-visible[\s\S]*?\{[^}]*outline:\s*3px solid var\(--bs-warning\)/,
        );
        expect(grouped).not.toBeNull();
    });
});
