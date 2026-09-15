// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Viewport and zoom matrix with measurable layout invariants (issue #491).
//
// 320/375/390/768/1024/1280/1440 widths, 200% text zoom, 400% browser zoom,
// long content — each combination asserting measurable invariants
// (getBoundingClientRect / scrollWidth), never screenshot diffing.
import {expect, Page, test} from '@playwright/test';
import {DJANGO_BASE_URL, expectNoHorizontalOverflow, login, pendingTicketMerge, rectOf, requireDjangoTier} from './support';

requireDjangoTier();

const WIDTHS = [320, 375, 390, 768, 1024, 1280, 1440];

async function assertLayoutInvariants(page: Page, path: string, width: number): Promise<void> {
    await expectNoHorizontalOverflow(page);
    const main = await rectOf(page, '#main-content');
    // Refloor (WCAG 1.4.10): at 320 CSS px the content must stay meaningful.
    if (width <= 390) {
        expect(
            main.width,
            `#main-content on ${path} is ${main.width}px at ${width}px`,
        ).toBeGreaterThanOrEqual(280);
    } else {
        expect(main.width, `#main-content on ${path} collapsed at ${width}px`).toBeGreaterThan(width * 0.5);
    }
}

test.describe('viewport matrix — rankings', () => {
    for (const width of WIDTHS) {
        test(`rankings at ${width}px`, async ({page}) => {
            await page.setViewportSize({width, height: 800});
            await login(page);
            await page.goto('/');
            await expect(page.locator('#organization-list')).toBeVisible();
            await assertLayoutInvariants(page, '/', width);
            // Navigation stays reachable at every width (rail on desktop,
            // hamburger on mobile).
            if (width < 768) {
                await expect(page.locator('[data-nav-toggle]')).toBeVisible();
            } else {
                await expect(page.locator('[data-nav-rail]')).toBeVisible();
            }
        });
    }
});

test.describe('viewport matrix — chat', () => {
    for (const width of WIDTHS) {
        test(`chat at ${width}px`, async ({page}) => {
            await page.setViewportSize({width, height: 800});
            await login(page);
            await page.goto('/chat/');
            await expect(page.getByTestId('job-search-chat')).toBeVisible();
            await assertLayoutInvariants(page, '/chat/', width);
            // The composer stays visible and usable at every width. The card's
            // exact vertical fit is chat-shell territory (#471/#477); what must
            // hold at every width is that the composer is rendered, keeps a
            // usable width, and is reachable (scroll + focus).
            const composer = page.locator('textarea[aria-label="Message"]');
            await expect(composer).toBeVisible();
            const box = await rectOf(page, 'textarea[aria-label="Message"]');
            expect(box.width, `composer width ${box.width}px at ${width}px`).toBeGreaterThanOrEqual(160);
            await composer.scrollIntoViewIfNeeded();
            await composer.focus();
            await expect(composer).toBeFocused();
        });
    }
});

test.describe('distinct zoom scenarios (issue #491 AC-5)', () => {
    // 200% TEXT-ONLY ZOOM (WCAG 1.4.4 resize text): scale the root font-size
    // to 200% — the app is rem/bootstrap-based, so rendered text doubles while
    // the layout viewport stays at its original CSS width. This is a distinct
    // browser scenario from CSS `zoom` (which paints-scales the whole subtree)
    // and from browser zoom (which reflows the layout viewport). Measurable
    // invariants: computed text size actually doubles, the CSS viewport is
    // unchanged (proving this is not browser zoom), and the doubled text does
    // not break the document (no horizontal overflow, content usable).
    test('200% text-only zoom doubles rendered text and keeps rankings usable at 1280px', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 800});
        await login(page);
        await page.goto('/');
        await page.locator('#organization-list').waitFor();
        const before = await page.evaluate(() =>
            parseFloat(getComputedStyle(document.querySelector('#organization-list') as HTMLElement).fontSize));
        await page.addStyleTag({content: 'html { font-size: 200% !important; }'});
        const after = await page.evaluate(() =>
            parseFloat(getComputedStyle(document.querySelector('#organization-list') as HTMLElement).fontSize));
        // Text really doubled at the computed-size level…
        expect(after, `text went from ${before}px to ${after}px`).toBeGreaterThanOrEqual(before * 1.9);
        // …while the CSS layout viewport is unchanged — this is text zoom, not
        // browser zoom (which would reflow the viewport to 320px).
        const viewport = await page.evaluate(() => document.documentElement.clientWidth);
        expect(viewport).toBe(1280);
        // Doubled text must not break the document: no horizontal overflow and
        // the rankings list stays visible with a meaningful width.
        await expectNoHorizontalOverflow(page);
        await expect(page.locator('#organization-list')).toBeVisible();
        const main = await rectOf(page, '#main-content');
        expect(main.width, `#main-content collapsed to ${main.width}px under 200% text zoom`).toBeGreaterThan(640);
        await expect(page.locator('[data-nav-rail]')).toBeVisible();
    });

    // 400% BROWSER ZOOM (WCAG 1.4.10 reflow): a 1280px window at 400% browser
    // zoom lays out in a 320 CSS px viewport and renders at a 4x device pixel
    // ratio. Emulated with a dedicated browser context (viewport 320 CSS px +
    // deviceScaleFactor 4) — unlike CSS `zoom` or page-scale paint tricks, this
    // produces the real media-query/reflow behavior of an actual browser zoom.
    // Measurable invariants: the layout viewport actually reflows to 320 CSS px,
    // the page renders at 4x device scale, content refloors (no horizontal
    // overflow, meaningful width), and the composer stays visible and focusable.
    test('400% browser zoom reflows chat to a 320px layout viewport at 4x device scale', async ({browser}) => {
        const context = await browser.newContext({
            baseURL: DJANGO_BASE_URL,
            viewport: {width: 320, height: 800},
            deviceScaleFactor: 4,
        });
        const page = await context.newPage();
        try {
            await login(page);
            await page.goto('/chat/');
            await expect(page.getByTestId('job-search-chat')).toBeVisible();
            const dims = await page.evaluate(() => ({
                clientWidth: document.documentElement.clientWidth,
                devicePixelRatio: window.devicePixelRatio,
            }));
            // Real reflow at the layout level (not paint scaling): the CSS
            // viewport is 1280/4 = 320px — the browser-zoom layout equivalent.
            expect(dims.clientWidth, '400% browser zoom must reflow to a 320px layout viewport').toBe(320);
            // …and the rendering is at the 400% device scale.
            expect(dims.devicePixelRatio).toBe(4);
            // Reflow invariants: no horizontal scrolling, content meaningful,
            // composer visible and keyboard-reachable.
            await expectNoHorizontalOverflow(page);
            const main = await rectOf(page, '#main-content');
            expect(main.width, `#main-content collapsed to ${main.width}px at 400% browser zoom`).toBeGreaterThanOrEqual(280);
            const composer = page.locator('textarea[aria-label="Message"]');
            await expect(composer).toBeVisible();
            const box = await rectOf(page, 'textarea[aria-label="Message"]');
            expect(box.width, `composer width ${box.width}px at 400% browser zoom`).toBeGreaterThanOrEqual(160);
            await composer.focus();
            await expect(composer).toBeFocused();
            await expect(page.locator('[data-nav-toggle]')).toBeVisible();
        } finally {
            await context.close();
        }
    });
});

test.describe('long content', () => {
    test('maximal-length organization name wraps without overflow at 375px', async ({page}) => {
        await page.setViewportSize({width: 375, height: 800});
        await login(page);
        await page.goto('/');
        await expect(page.locator('#organization-list')).toBeVisible();
        await expectNoHorizontalOverflow(page);
        // The seeded 100-char name renders inside the list and does not
        // break the document layout.
        await expect(page.locator('#organization-list')).toContainText('Longname');
    });

    test('a maximal-length chat message renders and is answered', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 800});
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
        const longMessage = `E2E long content probe: ${'crank'.repeat(120)}`;
        await page.locator('textarea[aria-label="Message"]').fill(longMessage);
        await page.getByLabel('Send message').click();
        await expect(
            page.locator('article[aria-label="Your message"]', {hasText: 'long content probe'}).first(),
        ).toBeVisible();
        await expect(page.locator('article[aria-label="Assistant message"]').last()).toBeVisible();
        await expectNoHorizontalOverflow(page);
    });
});

test.describe('assistant sidebar open/closed states', () => {
    test('assistant panel open/closed layout invariants', async ({page}) => {
        pendingTicketMerge(
            471,
            'the desktop assistant sidebar and its open/closed states ship with #471/#477; ' +
            'assert sidebar-aware layout invariants when they merge and delete this skip',
        );
        await page.setViewportSize({width: 1280, height: 800});
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
        await expectNoHorizontalOverflow(page);
    });
});
