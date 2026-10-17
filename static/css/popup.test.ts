// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import fs from 'fs';
import path from 'path';

describe('rankings toolbar and disclosure styles (issue #478)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');

    it('fixes desktop column geometry and floors touch targets at 44px', () => {
        expect(popupCss).toMatch(/\.organization-table\s*\{[^}]*table-layout:\s*fixed/);
        for (const col of ['rank', 'score', 'funding', 'rto', 'profile']) {
            expect(popupCss).toMatch(new RegExp(`\\.col-${col}\\s*\\{\\s*width:`));
        }
        expect(popupCss).toMatch(/\.rankings-toolbar \.filter-chip,[^{]*\.pagination \.page-link[^{]*\{\s*min-height:\s*44px/);
    });

    it('defines the toolbar, chip and disclosure rules and drops the side-column scoring panel', () => {
        expect(popupCss).toMatch(/\.rankings-toolbar\s*\{[^}]*flex-wrap:\s*wrap/);
        expect(popupCss).toMatch(/\.filter-chip-text\s*\{[^}]*text-overflow:\s*ellipsis/);
        expect(popupCss).toMatch(/\.how-ranking-works\s*\{/);
        expect(popupCss).not.toMatch(/\.scoring-(panel|summary)/);
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
        'z-assistant',
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
        // The chat More menu floats over the transcript inside the assistant
        // card only; it never competes with the page-level layers (issue #483).
        {selector: /^\.chat-more-menu$/, value: '7'},
        // Chat card-local stacking: header, panel overlay and pinned footer
        // order themselves inside the assistant card only (issue #483).
        {selector: /^#job-search-chat \.card-header$/, value: '4'},
        {selector: /^\.chat-panel-stack$/, value: '6'},
        {selector: /^#job-search-chat \.chat-footer > \.chat-jump-row$/, value: '2'},
        {selector: /^\.chat-confirm-dismiss$/, value: '1'},
    ];

    // Splits a stylesheet into (selector, declarations) rules. Pairing both
    // braces keeps @media preludes out of inner selectors, and stripping
    // comments first keeps commented-out braces from confusing the parse.
    const cssRules = (css: string): Array<{selector: string; declarations: string}> => {
        const stripped = css.replace(/\/\*[\s\S]*?\*\//g, '');
        return Array.from(stripped.matchAll(/([^{}]+)\{([^{}]*)\}/g),
            (match) => ({selector: match[1].trim(), declarations: match[2]}));
    };

    // Review r3: the contract is enforced on EACH z-index declaration, not
    // per rule. The r2 sweep passed a rule as soon as one of its declarations
    // consumed a token, so a same-rule override (`.app-mobile-topbar {
    // z-index: var(--z-mobile-topbar); z-index: 9999; }`) escaped the sweep
    // while the raw value won in CSS. The lookbehind keeps a custom property
    // named `*z-index*` from counting as a declaration mention.
    const Z_INDEX_MENTION = /(?<![\w-])z-index\s*:/;
    const zIndexDeclarations = (declarations: string): string[] =>
        declarations.match(/(?<![\w-])z-index\s*:\s*[^;]+;/g) ?? [];

    // Returns every z-index declaration that violates the layer-token
    // contract as a [selector, offending declaration] pair. A declaration
    // is clean only when it consumes a shared layer token, or — inside a
    // whitelisted dialog-local stacking context — equals the pinned value
    // exactly; any additional raw declaration sharing the rule is flagged,
    // because it would override the clean declaration in CSS.
    const zIndexViolations = (css: string): Array<[string, string]> => {
        const tokenDeclaration =
            new RegExp(`^z-index\\s*:\\s*var\\(--(?:${LAYER_TOKENS.join('|')})\\)\\s*;$`);
        const violations: Array<[string, string]> = [];
        for (const rule of cssRules(css)) {
            if (!Z_INDEX_MENTION.test(rule.declarations)) {
                continue;
            }
            const declarations = zIndexDeclarations(rule.declarations);
            if (declarations.length === 0) {
                // The rule declares z-index but parses to nothing (e.g. a
                // missing trailing semicolon): never skip it silently.
                violations.push([rule.selector, '<unparsable z-index declaration>']);
                continue;
            }
            const context = LOCAL_STACKING_CONTEXTS.find((c) => c.selector.test(rule.selector));
            const allowedPattern = context
                ? new RegExp(`^z-index\\s*:\\s*${context.value}\\s*;$`)
                : tokenDeclaration;
            for (const declaration of declarations) {
                if (!allowedPattern.test(declaration)) {
                    violations.push([rule.selector, declaration.trim()]);
                }
            }
        }
        return violations;
    };

    it('defines the shared layer tokens as :root custom properties', () => {
        expect(tokenValue('z-nav-rail')).toBe(1100);
        expect(tokenValue('z-nav-toggle')).toBe(1200);
        expect(tokenValue('z-nav-overlay')).toBe(1250);
        expect(tokenValue('z-nav-drawer')).toBe(1300);
        expect(tokenValue('z-blocking-dialog')).toBe(1400);
        expect(tokenValue('z-skip-link')).toBe(2000);
        expect(tokenValue('z-mobile-topbar')).toBe(1030);
        expect(tokenValue('z-assistant')).toBe(1050);
    });

    it('places the assistant workspace above the mobile topbar but below every blocking dialog', () => {
        // Issue #472: the launcher/drawer/sheet float over page chrome, and
        // the blocking-dialog layer stays on top so a dialog opened over the
        // sheet is never occluded by it.
        expect(tokenValue('z-assistant')!).toBeGreaterThan(tokenValue('z-mobile-topbar')!);
        expect(tokenValue('z-assistant')!).toBeLessThan(tokenValue('z-blocking-dialog')!);
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
        // that declares z-index — whatever its `position`.
        // Review r3: enforcement is per DECLARATION, so a same-rule raw
        // override can no longer hide behind a token declaration in the
        // same block — every declaration must consume a token, or be the
        // whitelisted dialog-local value with nothing else in the rule.
        const zRules = cssRules(popupCss).filter((rule) => Z_INDEX_MENTION.test(rule.declarations));
        expect(zRules.length).toBeGreaterThanOrEqual(9);

        // A failing diff lists each offending [selector, declaration] pair.
        expect(zIndexViolations(popupCss)).toEqual([]);

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

    it('rejects raw z-index values, including overrides hidden in a tokenized rule', () => {
        // Review r3 mutation probes, run through the same per-declaration
        // enforcement that guards popup.css: each synthetic violation must
        // be flagged, so the sweep cannot regress to per-rule matching.
        const mutantCss = [
            // Same-rule override: the token declaration parses, but the later
            // raw value wins in CSS.
            '.app-mobile-topbar { position: sticky; z-index: var(--z-mobile-topbar); z-index: 9999; }',
            // Whitelisted dialog-local context smuggling an extra raw value.
            '.chat-confirm-dismiss { position: absolute; z-index: 1; z-index: 15; }',
            // Page-level positioned layer with no token at all.
            '.app-messages { position: relative; z-index: 1500; }',
        ].join('\n');
        expect(zIndexViolations(mutantCss)).toEqual([
            ['.app-mobile-topbar', 'z-index: 9999;'],
            ['.chat-confirm-dismiss', 'z-index: 15;'],
            ['.app-messages', 'z-index: 1500;'],
        ]);

        // Control: the same enforcement accepts clean declarations — a
        // tokenized rule and the whitelisted dialog-local value — so it does
        // not over-block the legitimate contract.
        const cleanCss = [
            '.app-nav-rail { position: fixed; z-index: var(--z-nav-rail); }',
            '.chat-confirm-dismiss { position: absolute; z-index: 1; }',
        ].join('\n');
        expect(zIndexViolations(cleanCss)).toEqual([]);
    });

    it('the suggest modal uses the blocking dialog token and no company overlay is left', () => {
        expect(popupCss).not.toContain('.popup-overlay');
        expect(popupCss).toMatch(/\.modal\.blocking-modal\s*\{[^}]*z-index:\s*var\(--z-blocking-dialog\)/);
    });

    it('the company is not a dialog: no overlay, no pinned header and no inner scroll container', () => {
        // The details used to be a blocking dialog with a sticky header over
        // an internally scrolling body (issue #464); they are a view in the
        // page now (issue #486), so none of that may come back.
        expect(popupCss).not.toMatch(/\.popup-details\s*\{/);
        expect(popupCss).not.toMatch(/\.popup-details \.card-header/);
        const body = popupCss.match(/\.popup-details \.card-body\s*\{[^}]*\}/g) ?? [];
        expect(body.join('\n')).not.toMatch(/overflow-y:\s*auto/);
        expect(body.join('\n')).not.toMatch(/max-height/);
    });
});

describe('blocking-dialog close targets and row focus perimeter (issue #464 r2)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');

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

describe('company view (issue #486)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');
    const section = popupCss.slice(popupCss.indexOf('/* issue #486 company view */'), popupCss.indexOf('/* end issue #486 company view */'));
    const rule = (selector: string) => {
        const match = section.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`));
        return match ? match[1] : null;
    };

    it('has its own section at the end of the stylesheet', () => {
        const start = popupCss.indexOf('/* issue #486 company view */');
        const end = popupCss.indexOf('/* end issue #486 company view */');
        expect(start).toBeGreaterThan(0);
        expect(end).toBeGreaterThan(start);
        expect(section).toBe(popupCss.slice(start, end));
        expect(section.length).toBeGreaterThan(200);
        expect(section).not.toMatch(/z-index/);
    });

    it('is an in-flow card: nothing fixed, sticky or internally scrolling', () => {
        for (const selector of ['.company-view', '.company-view-header', '.company-view .card-body']) {
            const declarations = rule(selector);
            expect(declarations).not.toBeNull();
            expect(declarations).not.toMatch(/position:\s*(fixed|sticky)/);
            expect(declarations).not.toMatch(/overflow(-y)?:\s*(auto|scroll)/);
            expect(declarations).not.toMatch(/max-height/);
        }
        expect(rule('.company-view')).toMatch(/min-width:\s*0/);
    });

    it('gives Back to results and the content controls a 44px target', () => {
        expect(rule('.company-view-back')).toMatch(/min-height:\s*44px/);
        expect(section).toMatch(/\.company-view \.popup-company-actions \.btn,\s*\.company-view \.card-body :is\(\.btn, \.btn-link\)\s*\{[^}]*min-height:\s*44px/);
    });

    it('wraps a long name instead of overflowing, and stacks the grid before a column gets narrow', () => {
        expect(rule('.company-view-header h2')).toMatch(/overflow-wrap:\s*anywhere/);
        expect(rule('.company-view-header h2')).toMatch(/min-width:\s*0/);
        expect(rule('.company-view .popup-details-grid')).toMatch(/minmax\(min\(100%, 22rem\), 1fr\)/);
    });

    it('puts Back to results on its own line on phones and keeps the heading outline off', () => {
        expect(section).toMatch(/@media \(max-width: 767\.98px\)\s*\{[\s\S]*\.company-view-back\s*\{[^}]*width:\s*100%/);
        expect(rule('.company-view-header h2:focus')).toMatch(/outline:\s*none/);
        expect(rule('.organization-results-region:focus')).toMatch(/outline:\s*none/);
    });
});

describe('company view visual review round 1 (issue #486)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');
    const section = popupCss.slice(popupCss.indexOf('/* issue #486 company view */'), popupCss.indexOf('/* end issue #486 company view */'));
    const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const block = (selector: string, from = section) => {
        const match = from.match(new RegExp(`${escape(selector)}\\s*\\{([^}]*)\\}`));
        return match ? match[1] : null;
    };

    it('makes the view a named query container, so its own width decides the row layout', () => {
        expect(block('.company-view')).toMatch(/container-type:\s*inline-size/);
        expect(block('.company-view')).toMatch(/container-name:\s*company-view/);
    });

    it('lets a label wrap in its column and stacks every label/value row at 28rem or less', () => {
        expect(block('.company-view .text-end')).toMatch(/white-space:\s*normal/);
        const query = section.slice(section.indexOf('@container company-view (max-width: 28rem)'));
        expect(query).toMatch(/^@container company-view \(max-width: 28rem\)/);
        expect(block(".company-view .row > [class*='col-']", query)).toMatch(/width:\s*100%/);
        expect(block('.company-view .row > .offset-5', query)).toMatch(/margin-left:\s*0/);
        expect(block('.company-view .row > .text-end', query)).toMatch(/text-align:\s*left !important/);
        expect(query).toMatch(/\.popup-details-score-row,[\s\S]*your-pending-correction-[\s\S]*flex-direction:\s*column/);
    });

    it('hides the rankings introduction while a company is open but keeps the h1 for assistive technology', () => {
        const heading = block('.homepage-container:has(.company-view) > h1');
        expect(heading).toMatch(/position:\s*absolute/);
        expect(heading).toMatch(/clip:\s*rect\(0, 0, 0, 0\)/);
        expect(heading).not.toMatch(/display:\s*none/);
        const intro = block('.homepage-container:has(.company-view) > h1 + p,\n.homepage-container:has(.company-view) > .how-ranking-works');
        expect(intro).toMatch(/display:\s*none/);
    });

    it('keeps the Assistant bar at the bottom edge of a short phone page, from the bar\'s own height', () => {
        const phone = section.slice(section.indexOf('body:has(.assistant-dock) .app-content:has(.company-view)'));
        const declarations = block('body:has(.assistant-dock) .app-content:has(.company-view)', phone) as string;
        // The bar's button is 44.5px tall, so the bar is 2px (not 1px) over 44px + 1rem.
        const bar = '(44px + 1rem + 2px + var(--safe-area-inset-bottom))';
        expect(declarations).toContain(`min-height: calc(100vh - 49px - ${bar});`);
        expect(declarations).toContain(`min-height: calc(100dvh - 49px - ${bar});`);
        expect(declarations.indexOf('100vh')).toBeLessThan(declarations.indexOf('100dvh'));
        expect(section.slice(0, section.indexOf('body:has(.assistant-dock) .app-content:has(.company-view)')))
            .toMatch(/@media \(max-width: 767\.98px\)\s*\{\s*$/);
        expect(popupCss).toMatch(/body:has\(\.assistant-dock\) \.app-content \{\s*padding-bottom: calc\(44px \+ 1rem \+ 1px \+ var\(--safe-area-inset-bottom\)\)/);
    });

    it('makes not found and load error a compact block of at most 40rem', () => {
        expect(block('.company-view--compact')).toMatch(/max-width:\s*40rem/);
    });

    it('gives the empty score line the quiet emphasis and centres the action labels', () => {
        const none = block(".popup-details-scores .table td[data-testid='no-scores']");
        expect(none).toMatch(/font-weight:\s*400/);
        expect(none).toMatch(/text-align:\s*left !important/);
        const actions = block('.company-view .popup-company-actions .btn');
        expect(actions).toMatch(/justify-content:\s*center/);
        expect(actions).toMatch(/text-align:\s*center/);
    });

    it('keeps the sheet\'s Back label on one line with an ellipsis', () => {
        const label = block('.assistant-back-label');
        expect(label).toMatch(/white-space:\s*nowrap/);
        expect(label).toMatch(/text-overflow:\s*ellipsis/);
        expect(label).toMatch(/overflow:\s*hidden/);
        expect(block('.assistant-back-to-results')).toMatch(/min-width:\s*0/);
    });
});

describe('company view visual review round 2 (issue #486)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');
    const section = popupCss.slice(popupCss.indexOf('/* issue #486 company view */'), popupCss.indexOf('/* end issue #486 company view */'));
    const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const block = (selector: string, from = section) => {
        const match = from.match(new RegExp(`${escape(selector)}\\s*\\{([^}]*)\\}`));
        return match ? match[1] : null;
    };

    it('puts the evidence link under its text whenever the view, not the viewport, is 46rem or narrower', () => {
        const query = section.slice(section.indexOf('@container company-view (max-width: 46rem)'));
        expect(query).toMatch(/^@container company-view \(max-width: 46rem\)/);
        expect(block('.company-view .evidence-row-value', query)).toMatch(/display:\s*flex/);
        expect(block('.company-view .evidence-row-value > .correction-action', query)).toMatch(/white-space:\s*normal/);
        // The viewport rule it overrides is left where it is, above the view's section.
        expect(popupCss.indexOf('@media (min-width: 992px) {\n    .evidence-row-value')).toBeLessThan(popupCss.indexOf('/* issue #486 company view */'));
    });

    it('keeps the stacked pending-suggestion row left-aligned against Bootstrap\'s important align-items-center', () => {
        const query = section.slice(section.indexOf('@container company-view (max-width: 28rem)'));
        expect(block("[data-testid^='your-pending-correction-']", query.slice(query.indexOf('Bootstrap')))).toMatch(/align-items:\s*flex-start !important/);
    });

    it('sizes the heading of a narrow view like a phone heading', () => {
        const query = section.slice(section.indexOf('@container company-view (max-width: 28rem) {\n    .company-view .company-view-header h2'));
        expect(block('.company-view .company-view-header h2', query)).toMatch(/font-size:\s*1\.25rem/);
    });
});

describe('company view visual review round 3 (issue #486)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');
    const section = popupCss.slice(popupCss.indexOf('/* issue #486 company view */'), popupCss.indexOf('/* end issue #486 company view */'));

    it('lets the label column of "Check history" give way so a date or "Never" is never broken', () => {
        const rule = section.match(/\n\.company-view \.evidence-timestamps dl\s*\{([^}]*)\}/);
        expect(rule).not.toBeNull();
        expect(rule![1]).toMatch(/grid-template-columns:\s*minmax\(0,\s*max-content\)\s+max-content\s*;/);
        // The rule it overrides stays above the view's section.
        expect(popupCss.indexOf('.evidence-timestamps dl { display: grid')).toBeLessThan(popupCss.indexOf('/* issue #486 company view */'));
    });
});

describe('company view fit and open jobs (issue #486c)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');
    const section = popupCss.slice(popupCss.indexOf('/* issue #486 company view */'), popupCss.indexOf('/* end issue #486 company view */'));
    const rule = (selector: string): string => {
        const start = section.indexOf(`${selector} {`);
        expect(start).toBeGreaterThanOrEqual(0);
        return section.slice(start, section.indexOf('}', start));
    };

    test('a fit that is being read again is dimmed without changing its height, and the loading placeholder is not', () => {
        const dim = rule('.company-fit [aria-busy="true"]:not([role="status"])');
        expect(dim).toMatch(/opacity:\s*\.75;/);
        expect(dim).not.toMatch(/height|display|visibility/);
    });

    test('the new rules live inside the #486 section and use no raw z-index', () => {
        expect(section).toContain('.company-fit-figures');
        expect(section).toContain('.company-job-row');
        expect(section.slice(section.indexOf('Fit and open jobs'))).not.toMatch(/z-index/);
    });

    test('the three figures stack as label / value rows when the view is narrow', () => {
        expect(rule('.company-fit-figures')).toContain('repeat(3, minmax(0, 1fr))');
        const narrow = section.slice(section.indexOf('@container company-view (max-width: 28rem) {\n    .company-fit-figures'));
        expect(narrow).toContain('grid-template-columns: minmax(0, 1fr)');
    });

    test('rows wrap and long text breaks, so nothing overflows sideways', () => {
        expect(rule('.company-fit-row,\n.company-job-row')).toContain('flex-wrap: wrap');
        for (const selector of ['.company-fit-label', '.company-fit-value', '.company-job-meta']) {
            expect(rule(selector)).toContain('min-width: 0');
            expect(rule(selector)).toContain('overflow-wrap: anywhere');
        }
    });

    test('a stand-alone link in the sections is at least 44px high', () => {
        const link = section.slice(section.indexOf('.company-job-link,'));
        expect(link).toContain('min-height: 44px');
        expect(link).toContain("[data-testid='fit-sign-in']");
    });

    test('the action row wraps so View jobs sits beside Ask or under it', () => {
        expect(rule('.company-view .popup-company-actions')).toContain('flex-wrap: wrap');
    });
});

describe('company view visual review round 1 fixes (issue #486c)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');
    const section = popupCss.slice(popupCss.indexOf('/* issue #486 company view */'), popupCss.indexOf('/* end issue #486 company view */'));
    const body = (selector: string, from = section): string => {
        const start = from.indexOf(`${selector} {`);
        expect(start).toBeGreaterThanOrEqual(0);
        return from.slice(start, from.indexOf('}', start));
    };

    test('a jump heading clears the phone top bar, at the breakpoint where the top bar is shown', () => {
        expect(body('.company-view .company-jump-heading')).toContain('scroll-margin-top: 1rem');
        // The top bar's own rule is the max-width 767.98px query higher up.
        const phones = section.slice(section.indexOf('@media (max-width: 767.98px) {\n    .company-view .company-jump-heading'));
        expect(body('.company-view .company-jump-heading', phones)).toContain('scroll-margin-top: 4.5rem');
        expect(popupCss.indexOf('.app-mobile-topbar {\n        position: sticky')).toBeGreaterThan(0);
    });

    test('a jump heading reached from the keyboard shows a ring that outranks the old outline:none', () => {
        const ring = body('.company-view h3.company-jump-heading[tabindex]:focus-visible');
        expect(ring).toMatch(/outline:\s*2px solid/);
        expect(ring).not.toContain('outline: none');
        expect(popupCss).toContain('.popup-details h3[tabindex="-1"]:focus {\n    outline: none;');
        expect(popupCss.indexOf('.popup-details h3[tabindex="-1"]:focus {')).toBeLessThan(popupCss.indexOf('/* issue #486 company view */'));
    });

    test('the two jump buttons share one row on phones', () => {
        expect(body('.company-view .popup-company-jumps')).toContain('display: flex');
        const phones = section.slice(section.indexOf('@media (max-width: 767.98px) {\n    .company-view .popup-company-jumps'));
        expect(body('.company-view .popup-company-jumps', phones)).toContain('flex: 1 1 100%');
        expect(body('.company-view .popup-company-jumps .btn', phones)).toContain('white-space: nowrap');
    });

    test('a value sits near its label, and the three numbers share a baseline', () => {
        expect(body('.company-fit-label')).toContain('flex: 0 1 14rem');
        expect(body('.company-fit-figures .job-match-figure')).toContain('justify-content: space-between');
    });

    test('a limit on what a fact covers is in a colour that is not the muted one', () => {
        expect(body('.company-view .evidence-scope-limited')).toContain('color: #f5c26b');
        expect(body('.company-fit-out-of-scope')).toContain('color: #f5c26b');
    });

    test('the assistant card\'s in-app action is bordered and semibold; Website stays a link', () => {
        const rule = body('.org-card .org-card-view-company');
        expect(rule).toContain('border: 1px solid currentColor');
        expect(rule).toContain('font-weight: 600');
    });

    test('the disclosure summary and the no-link note are 44px high', () => {
        expect(body('.company-fit-legend .how-ranking-works-summary')).toContain('min-height: 44px');
        expect(body('.company-job-no-link')).toContain('min-height: 44px');
    });
});

describe('rankings responsive layout, name clamping and disclosure (issue #478 visual round 2)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');
    const containerBlock = popupCss.match(/@container organization-results \(max-width: 56rem\) \{[\s\S]*?\n\}\n/)![0];

    it('switches table to cards on the results container width, not the viewport', () => {
        expect(popupCss).toMatch(/\.organization-results\s*\{[^}]*container:\s*organization-results\s*\/\s*inline-size/);
        expect(containerBlock).toMatch(/\.organization-table-wrap\s*\{\s*display:\s*none/);
        expect(containerBlock).toMatch(/\.organization-cards\s*\{[^}]*display:\s*grid/);
        expect(popupCss).not.toMatch(/@media[^{]*\{[^@]*\.organization-cards\s*\{[^}]*display:\s*grid/);
    });

    it('clamps card and table names and lays card metadata out in a compact auto-fill grid', () => {
        expect(containerBlock).toMatch(/\.organization-card-name\s*\{[^}]*-webkit-line-clamp:\s*2[^}]*overflow-wrap:\s*anywhere/);
        expect(containerBlock).toMatch(/\.organization-card \.card-body\s*\{[^}]*repeat\(auto-fill,\s*minmax\(6\.5rem/);
        expect(popupCss).toMatch(/\.organization-table \.organization-name\s*\{[^}]*-webkit-line-clamp:\s*3/);
    });

    it('draws a rotating disclosure chevron at every width and honours reduced motion', () => {
        expect(popupCss).toMatch(/\.how-ranking-works-summary::before\s*\{[^}]*rotate\(-45deg\)/);
        expect(popupCss).toMatch(/\.how-ranking-works\[open\] > \.how-ranking-works-summary::before\s*\{[^}]*rotate\(45deg\)/);
        expect(popupCss).toMatch(/prefers-reduced-motion: reduce\)\s*\{\s*\.how-ranking-works-summary::before\s*\{\s*transition:\s*none/);
    });

    it('bounds the empty-state panel', () => {
        expect(popupCss).toMatch(/\.organization-empty-state\s*\{[^}]*max-width:\s*48rem[^}]*padding:\s*1\.25rem/);
    });
});

describe('chat layering and pinned rows (issue #483 adversarial review round 1)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');
    const rule = (selector: string): string => {
        const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        return popupCss.match(new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`))![1];
    };
    const zIndex = (selector: string): number => Number(rule(selector).match(/z-index:\s*(\d+)/)![1]);

    it('layers the More menu above the inline panel stack, which it shares a stacking context with', () => {
        expect(zIndex('.chat-more-menu')).toBeGreaterThan(zIndex('.chat-panel-stack'));
    });

    it('never gives the panel body scroll-padding, which would scroll it whenever focus lands in a chat bar', () => {
        expect(popupCss).not.toMatch(/\.assistant-panel-body[^{]*\{[^}]*scroll-padding/);
    });

    it('pins the action row of an inline panel and caps footer notices against the card, falling back to 40% of the view', () => {
        expect(rule('.chat-confirm-dismiss')).toMatch(/position:\s*sticky/);
        expect(popupCss).not.toMatch(/\.chat-panel-stack \.chat-confirm-actions\s*\{/);
        expect(rule('.chat-footer-notices')).toMatch(/max-height:\s*var\(--chat-notices-max,\s*40dvh\)/);
    });
});

describe('one transcript scroller in the assistant panel (issue #483)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
    // Every rule as (selector, declarations); a rule inside @media keeps its own selector.
    const rules = Array.from(popupCss.matchAll(/([^{}]+)\{([^{}]*)\}/g))
        .map(([, selector, declarations]) => ({selector: selector.trim().replace(/\s+/g, ' '), declarations}));
    const declared = (selector: string): string => {
        const found = rules.filter((r) => r.selector === selector);
        expect(found.length).toBeGreaterThan(0);
        return found.map((r) => r.declarations).join('\n');
    };

    it('gives the transcript a length flex-basis, so the card\'s minimum is its bars and not the whole conversation', () => {
        const transcript = declared('#job-search-chat .chat-transcript');
        expect(transcript).toMatch(/flex:\s*1 1 0px;/);
        expect(transcript).toMatch(/min-height:\s*0;/);
        expect(transcript).toMatch(/overflow-y:\s*auto;/);
        // `auto` makes the minimum the content; `0%` (what `flex: 1` expands to) is treated as content in an auto-height column.
        const flexes = rules.filter((r) => /\.chat-transcript$|\[role='log'\]$/.test(r.selector) && r.selector.includes('#job-search-chat'))
            .flatMap((r) => r.declarations.match(/flex(?:-basis)?:[^;]*;/g) ?? []);
        expect(flexes).toEqual(['flex: 1 1 0px;']);
    });

    it('lets the chat grow into the panel but never shrink below its own bars', () => {
        expect(declared('.assistant-panel-body #job-search-chat')).toMatch(/flex:\s*1 0 auto;/);
        expect(declared('.assistant-panel-body #job-search-chat')).not.toMatch(/min-height/);
        expect(declared('.assistant-panel-body #job-search-chat > section.card')).toMatch(/flex:\s*1 1 auto;/);
    });

    it('ends the card at the visual viewport with the measured keyboard inset', () => {
        expect(declared('.assistant-panel-body #job-search-chat > section.card'))
            .toMatch(/margin-bottom:\s*var\(--chat-keyboard-inset, 0px\);/);
    });

    it('makes the open priorities block the one that gives way, down to 11rem and never above 60%', () => {
        const row = declared('.priorities-sidebar');
        expect(row).toMatch(/flex:\s*0 0 auto;/);
        expect(row).toMatch(/max-height:\s*60%;/);
        expect(row.match(/max-height:/g)).toHaveLength(1);
        const open = declared('.priorities-sidebar:has(.priorities-scroll)');
        expect(open).toMatch(/flex-shrink:\s*1;/);
        expect(open).toMatch(/min-height:\s*min\(11rem, 100%\);/);
        expect(declared('.assistant-panel-body:has(> .priorities-sidebar .priorities-scroll) #job-search-chat .chat-transcript'))
            .toMatch(/min-height:\s*4\.5rem;/);
    });

    it('pins nothing in the chat: no rule that names the chat, the panel body or the priorities block is sticky, bar four known ones', () => {
        const sticky = rules.filter((r) => /#job-search-chat|\.chat-|\.assistant-panel-body|\.priorities-sidebar/.test(r.selector)
            && /position:\s*sticky/.test(r.declarations));
        // Only the confirmation's dismiss control and the priorities editor's own bars pin; no bar of the chat does.
        expect(sticky.map((r) => r.selector)).toEqual([
            '.chat-confirm-dismiss',
            '.priorities-sidebar .priorities-applied > .chat-actions, .priorities-sidebar .priorities-confirm > .chat-actions',
            '.priorities-review > .chat-actions',
            '.priorities-sidebar .priorities-review-compact > .pref-change-error',
        ]);
    });

    it('has no trace of the panel-scroll mode or the priorities pin', () => {
        ['data-scroll-owner', 'data-pinned', '--chat-band-top', '--chat-band-bottom', '--priorities-open-h',
            '--priorities-body-pad', '--priorities-room', '--priorities-chat-floor'].forEach((name) => {
            expect(popupCss).not.toContain(name);
        });
    });

    it('at 480px tall and less hides the disclaimer and the row\'s summary visually, keeping both for assistive technology', () => {
        const short = popupCss.split('@media (max-height: 480px)').slice(1).map((part) => part.slice(0, part.indexOf('\n}')));
        expect(short).toHaveLength(2);
        const clipped = /position:\s*absolute;[^}]*width:\s*1px;[^}]*height:\s*1px;[^}]*clip:\s*rect\(0, 0, 0, 0\);/;
        const all = short.join('\n');
        expect(all).toMatch(new RegExp(`#job-search-chat form \\.chat-disclaimer\\s*\\{[^}]*${clipped.source}`));
        expect(all).toMatch(new RegExp(`\\.priorities-summary-toggle\\[aria-expanded='false'\\] \\.priorities-summary\\s*\\{[^}]*${clipped.source}`));
        // The one-line row keeps its Edit button off the divider under it.
        expect(all).toMatch(/\.assistant-panel-body > \.priorities-sidebar\s*\{\s*padding-block:\s*0 \.25rem;/);
        // More opens beside its button, so a second press on the button closes it, and scrolls inside itself, so Delete is reachable with the body at rest.
        expect(all).toMatch(/\.chat-more-menu\s*\{\s*top:\s*0;\s*right:\s*calc\(100% \+ \.25rem\);[^}]*max-width:\s*calc\(100cqw - 7\.5rem\);[^}]*max-height:\s*calc\(100dvh - 11rem\);[^}]*overflow-y:\s*auto;[^}]*overscroll-behavior:\s*contain;/);
        // The 300px drawer has no room beside the button; its menu opens below it, clear of the button.
        expect(all).toMatch(/\.assistant-panel--drawer \.chat-more-menu\s*\{\s*top:\s*calc\(100% \+ \.25rem\);\s*right:\s*0;[^}]*max-height:\s*calc\(100dvh - 14rem\);/);
        // The unread pill sits on the footer's top edge, so its centre stays clear of the header above a short transcript.
        expect(all).toMatch(/#job-search-chat \.chat-footer > \.chat-jump-row\s*\{\s*bottom:\s*100%;\s*\}/);
        expect(declared('#job-search-chat .chat-footer > .chat-jump-row'))
            .toMatch(/bottom:\s*calc\(100% \+ 1\.5rem - min\(1\.5rem, var\(--chat-keyboard-inset, 0px\)\)\);/);
        expect(all).toMatch(/#job-search-chat \.empty-history-lead\s*\{[^}]*font-size:\s*\.875rem;[^}]*line-height:\s*1\.3;/);
        expect(all).not.toMatch(/display:\s*none|visibility:\s*hidden/);
        // Above 480px the disclaimer is laid out normally.
        expect(declared('#job-search-chat form .chat-disclaimer')).toMatch(/margin:\s*\.5rem 0 0;/);
    });

    it('pins the editor footer over the fields that scroll under it, inside the block\'s own stacking context', () => {
        expect(declared('.priorities-sidebar .priorities-scroll')).toMatch(/isolation:\s*isolate;/);
        expect(declared('.priorities-sidebar .priorities-footer')).toMatch(/z-index:\s*var\(--z-assistant\);/);
    });

    it('keeps the docked transcript box clear of the composer band, except on short panels', () => {
        expect(popupCss).toMatch(/@media \(min-width: 768px\) and \(min-height: 481px\) \{\s*\.assistant-panel #job-search-chat \.chat-transcript\s*\{\s*margin-bottom:\s*\.5rem;/);
    });
});

describe('notices in the transcript and collapsing footer notices (issue #483)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
    const rules = Array.from(popupCss.matchAll(/([^{}]+)\{([^{}]*)\}/g))
        .map(([, selector, declarations]) => ({selector: selector.trim().replace(/\s+/g, ' '), declarations}));
    const declared = (selector: string): string => {
        const found = rules.filter((r) => r.selector === selector);
        expect(found.length).toBeGreaterThan(0);
        return found.map((r) => r.declarations).join('\n');
    };

    it('stacks the diff rows of a notice in the transcript, so a value never sits in a sliver beside its name', () => {
        const item = declared('#job-search-chat .chat-transcript-notice .pref-change-item');
        expect(item).toMatch(/display:\s*flex;/);
        expect(item).toMatch(/flex-direction:\s*column;/);
    });

    it('wraps the notice\'s actions and keeps each label whole', () => {
        expect(declared('#job-search-chat .chat-transcript-notice .chat-actions')).toMatch(/flex-wrap:\s*wrap;/);
        const button = declared('#job-search-chat .chat-transcript-notice .chat-btn');
        expect(button).toMatch(/flex:\s*0 0 auto;/);
        expect(button).toMatch(/white-space:\s*nowrap;/);
    });

    it('clamps a collapsed error to two lines, on the one element that holds its whole text', () => {
        const text = declared('#job-search-chat .chat-footer-notices.is-collapsed .chat-error-text');
        expect(text).toMatch(/-webkit-line-clamp:\s*2;/);
        expect(text).toMatch(/overflow:\s*hidden;/);
        expect(text).toMatch(/line-height:\s*1\.25;/);
        expect(text).not.toMatch(/nowrap/);
        // Nothing hides the error itself; a folded status notice keeps only its title in the row.
        expect(popupCss).not.toMatch(/\.chat-error-(?:text|alert|row)[^{]*\{[^}]*display:\s*none/);
        const folded = '#job-search-chat .chat-footer-notices.is-collapsed';
        expect(declared(`${folded} .assistant-status-icon, ${folded} .assistant-status-body, ${folded} .assistant-status-notice-actions`))
            .toMatch(/display:\s*none !important;/);
        expect(declared(`${folded} [data-testid='assistant-status-notice']`)).not.toMatch(/display:\s*none/);
    });

    it('sits a notice closer to the reply that produced it than to the next message', () => {
        expect(declared('#job-search-chat .chat-transcript-notice')).toMatch(/margin:\s*0 0 1rem;/);
    });

    it('takes the docked log\'s 16px turn gap back above a notice', () => {
        expect(declared('body.assistant-docked #job-search-chat [role=\'log\'] > .chat-transcript-notice')).toMatch(/margin:\s*-1rem 0 0;/);
    });

    it('keeps the toggle at the bottom right of the row whether collapsed or expanded', () => {
        expect(declared('#job-search-chat .chat-footer-notices.is-collapsed > .chat-notices-toggle')).toMatch(/align-self:\s*flex-end;/);
    });

    it('scrolls a control reached with Tab clear of the jump pill', () => {
        expect(declared('#job-search-chat .chat-transcript')).toMatch(/scroll-padding-bottom:\s*4\.5rem;/);
    });

    it('gives the plain banner a 44px close button like the other notices, and a wrapped value keeps its arrow', () => {
        expect(declared('#job-search-chat .chat-transcript-notice .alert.chat-pref-alert')).toMatch(/min-height:\s*44px;/);
        expect(declared('#job-search-chat .chat-transcript-notice .pref-change-arrow')).toMatch(/display:\s*none;/);
        expect(declared('#job-search-chat .chat-transcript-notice .pref-change-new::before')).toMatch(/content:/);
        // A list of chips carries the arrow on its first chip, so the arrow wraps with it.
        expect(declared('#job-search-chat .chat-transcript-notice .pref-change-new > .pref-change-token:first-child::before')).toMatch(/position:\s*absolute;/);
        expect(declared('#job-search-chat .chat-transcript-notice .pref-change-new:has(> .pref-change-token)::before')).toMatch(/display:\s*none;/);
    });

    it('keeps the toggle a 44px target, the last row of the area, right-aligned while the error is expanded', () => {
        expect(declared('#job-search-chat .chat-notices-toggle')).toMatch(/min-block-size:\s*44px;/);
        expect(declared('#job-search-chat .chat-footer-notices:not(.is-collapsed) > .chat-notices-toggle'))
            .toMatch(/align-self:\s*flex-end;/);
        // The details scroll above it; the toggle itself is never inside that scroll.
        expect(declared('.chat-footer-notices .chat-notices-body')).toMatch(/overflow-y:\s*auto;/);
        expect(declared('.chat-footer-notices')).toMatch(/flex-direction:\s*column;/);
    });

    it('leaves the folded status notice unpositioned so it is never painted over Retry (no z-index: the layer tokens stay the contract)', () => {
        const folded = declared('#job-search-chat .chat-footer-notices.is-collapsed [data-testid=\'assistant-status-notice\']');
        expect(folded).toMatch(/(?:^|[\s;])position:\s*static;/);
        expect(folded).not.toMatch(/z-index/);
    });

    it('keeps the expanded toggle inside the box Details had, so a second press cannot reach Retry', () => {
        const toggle = declared('#job-search-chat .chat-footer-notices:not(.is-collapsed) > .chat-notices-toggle');
        expect(toggle).toMatch(/inline-size:\s*min-content;/);
        expect(toggle).toMatch(/white-space:\s*normal;/);
    });

    it('lets the error take a third line in the 300-340px drawer only, and sets the disclaimer in 11px type on the narrowest phones', () => {
        expect(popupCss).toMatch(/@media \(min-width: 768px\) \{\s*@container assistant-panel \(max-width: 21\.5rem\) \{\s*#job-search-chat \.chat-footer-notices\.is-collapsed:not\(\.has-status\) \.chat-error-text \{\s*-webkit-line-clamp:\s*3;/);
        expect(popupCss).toMatch(/@media \(max-width: 340px\) \{\s*#job-search-chat form \.chat-disclaimer \{\s*font-size:\s*\.6875rem;/);
    });

    it('keeps the expanded notices under the existing cap with an inner scroll', () => {
        const notices = declared('.chat-footer-notices');
        expect(notices).toMatch(/max-height:\s*var\(--chat-notices-max, 40dvh\);/);
        expect(declared('.chat-footer-notices .chat-notices-body')).toMatch(/overflow-y:\s*auto;/);
    });
});

describe('job-match figures, notices and contained transcript (issue #473 visual round 2)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');
    const containerBlocks = (width: string) => Array.from(
        popupCss.matchAll(new RegExp(`@container job-match \\(max-width: ${width}\\) \\{([\\s\\S]*?)\\n\\}`, 'g')),
    ).map((match) => match[1]).join('\n');

    it('sizes the panel as a container, since it is narrower at 768px than on a 375px phone', () => {
        expect(popupCss).toMatch(/#job-match-panel\s*\{[^}]*container:\s*job-match \/ inline-size/);
    });

    it('puts the figure labels on one shared row and the values on the next', () => {
        expect(popupCss).toMatch(/#job-match-panel \.job-match-figures\s*\{[^}]*grid-template-rows:\s*auto auto/);
        expect(popupCss).toMatch(/#job-match-panel \.job-match-figure\s*\{[^}]*grid-row:\s*span 2;[^}]*grid-template-rows:\s*subgrid/);
        expect(popupCss).toMatch(/#job-match-panel \.job-match-figure-label\s*\{[^}]*overflow-wrap:\s*anywhere/);
        // No fixed-height label row is left to overprint a three-line label.
        expect(popupCss).not.toMatch(/\.job-match-figure\s*\{[^}]*grid-template-rows:\s*2rem/);
    });

    it('lists the figures as label / value rows when the panel cannot hold three columns', () => {
        const narrow = containerBlocks('24rem');
        expect(narrow).toMatch(/\.job-match-figures\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);[^}]*grid-template-rows:\s*none/);
        expect(narrow).toMatch(/\.job-match-figure\s*\{[^}]*grid-row:\s*auto;[^}]*align-items:\s*baseline/);
    });

    it('gives a wrapped chip qualifier its own line without a leading separator, and stacks the notice', () => {
        const narrow = containerBlocks('26rem');
        expect(narrow).toMatch(/\.job-match-chip-qualifier\s*\{\s*flex-basis:\s*100%/);
        expect(narrow).toMatch(/\.job-match-chip-sep\s*\{\s*display:\s*none/);
        expect(narrow).toMatch(/\.job-match-stale-banner\s*\{[^}]*grid-template-columns:\s*auto minmax\(0, 1fr\);/);
        expect(narrow).toMatch(/\.job-match-stale-refresh\s*\{[^}]*grid-column:\s*2/);
        expect(popupCss).toMatch(/\.job-match-chip \.evidence-badge-profile \.evidence-badge-marker\s*\{\s*display:\s*none/);
    });

    it('makes the transcript the containing block of its visually-hidden text', () => {
        expect(popupCss).toMatch(/#job-search-chat \.chat-transcript\s*\{[^}]*position:\s*relative/);
    });

    it('keeps the focused panel heading clear of the sticky phone top bar, with a visible ring', () => {
        expect(popupCss).toMatch(/#job-match-panel \.job-match-panel-title\s*\{\s*scroll-margin-top:\s*4\.5rem/);
        expect(popupCss).toMatch(/#job-match-panel \.job-match-panel-title:focus-visible\s*\{[^}]*outline:\s*3px solid/);
    });

    it('labels the reply-time snapshot on assistant cards in the card’s muted ink', () => {
        expect(popupCss).toMatch(/\.org-card \.org-card-evidence-label\s*\{[^}]*color:\s*#cbd5e1/);
    });

    it('sits a one-line figure label on its value in the column layout only (visual round 3)', () => {
        expect(popupCss).toMatch(/#job-match-panel \.job-match-figure-label\s*\{[^}]*align-self:\s*end/);
        expect(containerBlocks('24rem')).toMatch(/\.job-match-figure-label\s*\{\s*align-self:\s*auto/);
    });

    it('makes qualified chips full-width rows on narrow panels and bolds the changed qualifier', () => {
        expect(containerBlocks('26rem')).toMatch(/\.job-match-chip:has\(> \.job-match-chip-qualifier\)\s*\{\s*display:\s*flex/);
        expect(popupCss).toMatch(/\.job-match-chip--changed \.job-match-chip-qualifier\s*\{\s*font-weight:\s*600/);
    });

    it('shows a busy re-check control as unavailable without removing it from the tab order', () => {
        expect(popupCss).toMatch(/#job-match-panel \[aria-disabled="true"\]\s*\{[^}]*opacity:\s*\.65;[^}]*cursor:\s*default/);
    });

    it('gives the legend the panel\u2019s sky focus ring and a denser type size on narrow panels', () => {
        expect(popupCss).toMatch(/#job-match-panel \.how-ranking-works-summary:focus-visible\s*\{\s*outline-color:\s*#38bdf8/);
        const narrow = containerBlocks('26rem');
        expect(narrow).toMatch(/\.job-match-legend \.how-ranking-works-content\s*\{\s*font-size:\s*\.875rem;\s*line-height:\s*1\.45/);
        expect(narrow).toMatch(/\.job-match-legend \.ranking-definitions dt\s*\{\s*margin-top:\s*\.5rem/);
    });

    it('lets a reply with cards use the whole row in the narrowest assistant panel, in one line', () => {
        const rule = popupCss.split('\n').filter((line) => line.includes('.chat-bubble-assistant:has(.org-card)'));
        expect(rule).toHaveLength(1);
        expect(rule[0]).toMatch(/^@container assistant-panel \(max-width: 20rem\) \{ #job-search-chat \.chat-bubble-assistant:has\(\.org-card\) \{ max-width: 100% !important; \} \}$/);
    });
});

describe('collapsed priorities row and review step (issue #480 adversarial review round 3)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');

    it('puts the counts on their own line on phones up to 374px and in the 768-999px drawer', () => {
        expect(popupCss).toMatch(
            /@media \(max-width: 374\.98px\), \(min-width: 768px\) and \(max-width: 999\.98px\) \{\s*\.priorities-summary \{ flex-direction: column; \}/,
        );
    });

    it('dims This search only while its request runs', () => {
        expect(popupCss).toMatch(/\.priorities-review-search\[aria-disabled='true'\] \{ opacity: 0\.65; \}/);
    });
});

describe('job-match panel result refresh styles (issue #485)', () => {
    const popupCss = fs.readFileSync(path.join(__dirname, 'popup.css'), 'utf8');

    it('keeps the new rules in their own section ahead of the result-card rules, never at the end of the file', () => {
        const start = popupCss.indexOf('/* issue #485 result refresh */');
        const next = popupCss.indexOf('/* Job-search result cards (issue #396) */');
        expect(start).toBeGreaterThan(0);
        expect(next).toBeGreaterThan(start);
        expect(popupCss.indexOf('/* issue #485 result refresh */', start + 1)).toBe(-1);
        expect(popupCss.slice(start, next)).toMatch(/#job-match-panel a\.btn \{\s*display: inline-flex;\s*align-items: center;\s*justify-content: center;\s*\}/);
    });

    it('indents the folded line under the message, past the icon column', () => {
        const start = popupCss.indexOf('/* issue #485 result refresh */');
        const next = popupCss.indexOf('/* Job-search result cards (issue #396) */');
        expect(popupCss.slice(start, next)).toMatch(/#job-match-panel \.job-match-notice-folded \{\s*margin-left: 1\.5rem;\s*\}/);
    });

    it('keeps the icon of a box with no button on its first line, and leaves the stale banner with a button centred', () => {
        const start = popupCss.indexOf('/* issue #485 result refresh */');
        const next = popupCss.indexOf('/* Job-search result cards (issue #396) */');
        expect(popupCss.slice(start, next)).toMatch(
            /#job-match-panel \.job-match-stale-banner--plain \.job-match-stale-icon \{\s*align-self: start;\s*margin-top: 0\.125rem;\s*\}/,
        );
        // The wide-screen rule that centres the items is still there, for the box with a button.
        expect(popupCss).toMatch(/@media \(min-width: 576px\) \{\s*#job-match-panel \.job-match-stale-banner \{\s*grid-template-columns: auto minmax\(0, 1fr\) auto;\s*align-items: center;/);
    });

    it('scopes the heading scroll margin to the widths that show the sticky top bar', () => {
        expect(popupCss).toMatch(/@media \(max-width: 767\.98px\) \{\s*#job-match-panel \.job-match-panel-title \{ scroll-margin-top: 4\.5rem; \}\s*\}/);
        expect(popupCss).not.toMatch(/^#job-match-panel \.job-match-panel-title \{ scroll-margin-top/m);
    });
});
