// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Mobile width regression for the app shell (issue #456). Before the fix, the
// desktop `margin-left: 240px` + `max-width: calc(100vw - 240px)` deduction on
// `.app-content`/`.app-messages` survived navigation collapse below 768px,
// capping the main column to a narrow strip on phones. These assertions pin:
//   - no horizontal document overflow at phone widths,
//   - content spans the viewport minus its padding (no 240px deduction),
//   - key controls stay hit-testable,
//   - desktop (>= 768px) keeps the pre-change geometry.
import {test, expect, Page} from '@playwright/test';

const CHAT_FIXTURE = '/e2e/fixtures/job-search-chat.html';
const ORG_FIXTURE = '/e2e/fixtures/organization-list.html';

interface ConversationFixture {
    id: number;
    active: boolean;
    created: string | null;
    modified: string | null;
    messages: Array<Record<string, unknown>>;
    preferences_changed: boolean;
}

const populatedConversation: ConversationFixture = {
    id: 1,
    active: true,
    created: '2026-08-20T00:00:00Z',
    modified: '2026-08-20T00:00:00Z',
    messages: [
        {
            id: 11,
            role: 'user',
            content: 'I want a remote-friendly Series B company in health tech with a very long requirement sentence that must wrap instead of widening the bubble.',
            preferences_changed: false,
            created: '2026-08-20T00:01:00Z',
            results: null,
        },
        {
            id: 12,
            role: 'assistant',
            content: 'Here are some matches based on your preferences.',
            preferences_changed: false,
            created: '2026-08-20T00:01:01Z',
            results: null,
        },
    ],
    preferences_changed: false,
};

async function mockJobSearchApi(page: Page): Promise<void> {
    await page.route('**/api/agent/conversations/**', async (route) => {
        const request = route.request();
        const method = request.method();
        const pathname = new URL(request.url()).pathname;

        if (method === 'GET' && pathname === '/api/agent/conversations/') {
            await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(populatedConversation)});
            return;
        }
        await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
            message: {id: 99, role: 'assistant', content: 'Thanks — I found a few matches for you.', preferences_changed: false, created: '2026-08-20T00:02:00Z', results: null},
            preferences_changed: false,
        })});
    });
    await page.route('**/api/funding-round-choices/**', (route) =>
        route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({S: 'Seed', A: 'Series A', B: 'Series B', C: 'Series C', D: 'Series D', E: 'Series E', F: 'Series F'})}),
    );
    await page.route('**/api/rto-policy-choices/**', (route) =>
        route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({R: 'Remote', H: 'Hybrid', O: 'In-Office'})}),
    );
}

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
    const dims = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
    }));
    expect(dims.scrollWidth, 'document must not scroll horizontally').toBeLessThanOrEqual(dims.clientWidth + 1);
}

/** The main column must span the container: no surviving 240px sidebar deduction. */
async function expectContentSpansViewport(page: Page): Promise<void> {
    const rect = await page.evaluate(() => {
        const el = document.getElementById('main-content');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return {left: r.left, width: r.width, marginLeft: style.marginLeft, maxWidth: style.maxWidth, clientWidth: document.documentElement.clientWidth};
    });
    expect(rect, '#main-content must exist').not.toBeNull();
    expect(rect!.marginLeft, 'collapsed navigation must not keep the sidebar offset').toBe('0px');
    expect(rect!.maxWidth, 'content tracks its container on mobile').toBe('100%');
    expect(rect!.left, 'content starts at the viewport edge (padding applied inside)').toBeLessThanOrEqual(1);
    // Bootstrap applies border-box sizing, so the rect includes the shell padding.
    expect(rect!.width, 'content fills the container with no 240px deduction').toBeGreaterThanOrEqual(rect!.clientWidth - 1);
}

/**
 * Hit-test controls at their on-screen coordinates: `elementFromPoint` must
 * return the element (or a descendant), proving nothing overlays or clips it.
 * Samples several points across the element's visible region so tall or
 * folded elements are judged by the part a user can actually tap.
 */
async function expectHitTestable(page: Page, selector: string, label: string): Promise<void> {
    const box = await page.locator(selector).first().boundingBox();
    expect(box, `${label} must have a bounding box`).not.toBeNull();
    const viewport = page.viewportSize()!;
    expect(box!.x, `${label} must not start left of the viewport`).toBeGreaterThanOrEqual(-1);
    expect(box!.x + box!.width, `${label} must not be clipped by the right edge`).toBeLessThanOrEqual(viewport.width + 1);
    const visibleTop = Math.max(box!.y, 0);
    const visibleBottom = Math.min(box!.y + box!.height, viewport.height);
    expect(visibleBottom, `${label} must be at least partly on screen`).toBeGreaterThan(visibleTop);
    const candidates = [
        [box!.x + box!.width / 2, (visibleTop + visibleBottom) / 2],
        [box!.x + 8, visibleTop + 4],
        [box!.x + box!.width - 8, visibleBottom - 4],
        [box!.x + 8, visibleBottom - 4],
    ];
    const hit = await page.evaluate(([sel, points]) => {
        const target = document.querySelector(sel as string);
        if (!target) return false;
        return (points as Array<[number, number]>).some(([cx, cy]) => {
            const el = document.elementFromPoint(cx, cy);
            return !!el && (el === target || target.contains(el) || el.contains(target));
        });
    }, [selector, candidates]);
    expect(hit, `${label} must be hit-testable at its own coordinates`).toBe(true);
}

/**
 * The live shell renders the hamburger inside a reserved `.app-mobile-topbar`
 * sticky band (audit follow-up on #456): the toggle participates in normal flow
 * inside that band instead of floating `position: fixed` over page content, so
 * it can never overlap rankings headings or chat alerts at phone widths.
 */
async function expectTopbarReservesBand(page: Page, contentSelector: string, label: string): Promise<void> {
    const data = await page.evaluate((sel) => {
        const topbar = document.querySelector<HTMLElement>('.app-mobile-topbar');
        const toggle = document.querySelector<HTMLElement>('[data-nav-toggle]');
        const content = document.querySelector<HTMLElement>(sel);
        if (!topbar || !toggle || !content) return null;
        const topbarRect = topbar.getBoundingClientRect();
        const toggleRect = toggle.getBoundingClientRect();
        const contentRect = content.getBoundingClientRect();
        return {
            topbarPosition: getComputedStyle(topbar).position,
            topbarHeight: topbarRect.height,
            topbarBottom: topbarRect.bottom,
            togglePosition: getComputedStyle(toggle).position,
            toggleInTopbar: toggle.closest('.app-mobile-topbar') === topbar,
            toggleRect: {top: toggleRect.top, bottom: toggleRect.bottom, left: toggleRect.left, right: toggleRect.right},
            contentRect: {top: contentRect.top, bottom: contentRect.bottom, left: contentRect.left, right: contentRect.right},
        };
    }, contentSelector);
    expect(data, `topbar, toggle and ${label} must all exist`).not.toBeNull();
    expect(data!.topbarPosition, 'topbar is a sticky reserved band').toBe('sticky');
    expect(data!.topbarHeight, 'topbar reserves a comfortable band').toBeGreaterThanOrEqual(48);
    expect(data!.togglePosition, 'toggle flows inside the topbar band, not fixed over content').toBe('static');
    expect(data!.toggleInTopbar, 'toggle is rendered inside .app-mobile-topbar').toBe(true);
    expect(data!.contentRect.top, `${label} starts below the topbar band`).toBeGreaterThanOrEqual(data!.topbarBottom - 1);
    const overlapsVertically = data!.toggleRect.bottom > data!.contentRect.top + 1 && data!.toggleRect.top < data!.contentRect.bottom - 1;
    const overlapsHorizontally = data!.toggleRect.right > data!.contentRect.left + 1 && data!.toggleRect.left < data!.contentRect.right - 1;
    expect(overlapsVertically && overlapsHorizontally, `toggle must not overlap ${label}`).toBe(false);
}

const mobileViewports = [320, 375, 390, 430];

for (const width of mobileViewports) {
    test.describe(`mobile ${width}px`, () => {
        test.use({viewport: {width, height: 800}, isMobile: true, hasTouch: true});

        test('rankings reflows without the sidebar deduction and controls stay reachable', async ({page}) => {
            await mockJobSearchApi(page);
            await page.goto(ORG_FIXTURE);

            await expectNoHorizontalOverflow(page);
            await expectContentSpansViewport(page);

            // The hamburger gets its own reserved band: it must not overlay the
            // page heading (audit on #456 measured the fixed toggle overlapping
            // the "Company rankings" heading at every phone width).
            await expectTopbarReservesBand(page, '#organization-list h1', 'rankings heading');

            await expectHitTestable(page, '#organization-search', 'organization search input');
            await expectHitTestable(page, '#acceleratedVesting', 'accelerated vesting checkbox');
            // Mobile shows the card list; the opener is a card article (role=button).
            await expectHitTestable(page, 'article[aria-label^="View details for"]', 'organization card details opener');

            // Long names wrap instead of widening the document. Target the
            // deliberately long fixture record (Zephyr, id 6) — the audit found
            // this assertion was picking the first short card ("Acme Robotics"),
            // so it never exercised long-name overflow. The record carries both
            // spaced words and one unbroken token so `overflow-wrap: anywhere`
            // is genuinely exercised, not just ordinary space wrapping.
            const cardName = page.locator('.organization-card-name', {hasText: 'Zephyr Quintessence'}).first();
            await expect(cardName).toBeVisible();
            await expect(cardName).toContainText('NanotechnologicalBiopharmaceuticalResearchLaboratories');
            const nameOverflow = await cardName.evaluate((el) => {
                const style = getComputedStyle(el);
                return {wrap: style.overflowWrap || style.wordWrap, scrollW: el.scrollWidth, clientW: el.clientWidth};
            });
            expect(nameOverflow.scrollW, 'long organization names must not force horizontal overflow').toBeLessThanOrEqual(nameOverflow.clientW + 1);
        });

        test('chat composer stays reachable and messages wrap', async ({page}) => {
            await mockJobSearchApi(page);
            await page.goto(CHAT_FIXTURE);

            await expect(page.locator('article[aria-label="Your message"]')).toHaveCount(1);
            await expectNoHorizontalOverflow(page);
            await expectContentSpansViewport(page);

            // The reserved topbar band must also clear Django message alerts
            // (the audit measured the fixed toggle overlapping the chat alert).
            await expectTopbarReservesBand(page, '.app-messages .alert', 'chat message alert');

            const composer = page.locator('textarea[aria-label="Message"]');
            await expect(composer).toBeVisible();
            await expectHitTestable(page, 'textarea[aria-label="Message"]', 'chat composer');
            await expectHitTestable(page, 'button[aria-label="Send message"]', 'send button');

            // The wrap rule lives on the inner bubble div (inline
            // word-break), not the outer article.
            const bubble = page.locator('article[aria-label="Your message"] > div').first();
            const breakWord = await bubble.evaluate((el) => {
                const style = getComputedStyle(el);
                return {break: style.wordBreak || style.overflowWrap, scrollW: el.scrollWidth, clientW: el.clientWidth};
            });
            expect(['break-word', 'anywhere'].includes(breakWord.break), 'chat bubbles keep their word-break rule').toBe(true);
            expect(breakWord.scrollW, 'long chat messages must wrap, not widen').toBeLessThanOrEqual(breakWord.clientW + 1);
        });
    });
}

test.describe('zoom resilience (mobile 375px)', () => {
    test.use({viewport: {width: 375, height: 800}, isMobile: true, hasTouch: true});

    test('200% text zoom keeps actions reachable without horizontal clipping', async ({page}) => {
        await mockJobSearchApi(page);
        await page.goto(ORG_FIXTURE);

        await page.addStyleTag({content: 'html { font-size: 200% !important; }'});

        await expectNoHorizontalOverflow(page);
        await expectHitTestable(page, '#organization-search', 'organization search input at 200% text zoom');
        // At 200% text zoom the card list stacks taller and the first card
        // sits below the fold in normal flow; a user scrolls to it, so
        // assert reachability after scrolling (same pattern as the
        // composer below).
        const opener = page.locator('article[aria-label^="View details for"]').first();
        await opener.scrollIntoViewIfNeeded();
        await expectHitTestable(page, 'article[aria-label^="View details for"]', 'details opener at 200% text zoom');

        await page.goto(CHAT_FIXTURE);
        await page.addStyleTag({content: 'html { font-size: 200% !important; }'});
        await expectNoHorizontalOverflow(page);
        // At 200% text zoom the history stacks taller and the composer sits
        // below the fold in normal flow; a user scrolls to it, so assert
        // reachability after scrolling rather than initial visibility.
        const composer = page.locator('textarea[aria-label="Message"]');
        await composer.scrollIntoViewIfNeeded();
        await expectHitTestable(page, 'textarea[aria-label="Message"]', 'composer at 200% text zoom');
        await expectHitTestable(page, 'button[aria-label="Send message"]', 'send button at 200% text zoom');
    });

    test.skip(({browserName}) => browserName !== 'chromium', 'page-scale zoom emulation is Chromium-only');

    // Mirrors the 200% page-zoom pattern from job-search-chat-viewport.spec.ts
    // at 4x: the shell must not gain horizontal scroll or bury actions.
    test('400% page zoom keeps actions reachable', async ({page, context}) => {
        await mockJobSearchApi(page);
        await page.goto(CHAT_FIXTURE);
        await expect(page.locator('article[aria-label="Your message"]')).toHaveCount(1);

        const cdp = await context.newCDPSession(page);
        await cdp.send('Emulation.setPageScaleFactor', {pageScaleFactor: 4});

        await expectNoHorizontalOverflow(page);
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toBeVisible();
        await expect(composer).toBeEnabled();
        await expect(page.locator('button[aria-label="Send message"]')).toBeVisible();
    });
});

test.describe('desktop parity (1280px)', () => {
    test.use({viewport: {width: 1280, height: 800}});

    test('rankings keeps the 240px nav-rail geometry', async ({page}) => {
        await mockJobSearchApi(page);
        await page.goto(ORG_FIXTURE);

        const rect = await page.evaluate(() => {
            const el = document.getElementById('main-content')!;
            const r = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return {left: r.left, width: r.width, marginLeft: style.marginLeft, maxWidth: style.maxWidth, clientWidth: document.documentElement.clientWidth};
        });
        expect(rect.marginLeft).toBe('240px');
        // getComputedStyle resolves the calc() to the used pixel value.
        expect(rect.maxWidth).toBe(`${page.viewportSize()!.width - 240}px`);
        expect(rect.left).toBe(240);
        // Desktop geometry is unchanged from pre-fix behavior: the content
        // column occupies the viewport minus the 240px rail.
        expect(rect.width).toBeLessThanOrEqual(rect.clientWidth - 240 + 1);
        await expectNoHorizontalOverflow(page);
    });

    test('chat keeps the 240px nav-rail geometry', async ({page}) => {
        await mockJobSearchApi(page);
        await page.goto(CHAT_FIXTURE);

        const rect = await page.evaluate(() => {
            const el = document.getElementById('main-content')!;
            const r = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return {left: r.left, width: r.width, marginLeft: style.marginLeft, maxWidth: style.maxWidth, clientWidth: document.documentElement.clientWidth};
        });
        expect(rect.marginLeft).toBe('240px');
        expect(rect.left).toBe(240);
        expect(rect.width).toBeLessThanOrEqual(rect.clientWidth - 240 + 1);
        await expectNoHorizontalOverflow(page);
    });
});
