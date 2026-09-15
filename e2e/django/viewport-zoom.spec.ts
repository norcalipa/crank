// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Viewport and zoom matrix with measurable layout invariants (issue #491).
//
// 320/375/390/768/1024/1280/1440 widths, 200% text zoom, 400% browser zoom,
// long content — each combination asserting measurable invariants
// (getBoundingClientRect / scrollWidth), never screenshot diffing.
import {expect, Page, test} from '@playwright/test';
import {expectNoHorizontalOverflow, login, pendingTicketMerge, rectOf, requireDjangoTier} from './support';

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

test.describe('zoom invariants', () => {
    // CSS zoom emulates the WCAG text-zoom / browser-zoom scenarios in
    // Chromium, which is this tier's matrix browser.
    test.skip(({browserName}) => browserName !== 'chromium', 'CSS zoom emulation is Chromium-only');

    test('200% text zoom keeps rankings usable at 1280px', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 800});
        await login(page);
        await page.goto('/');
        await page.locator('#organization-list').waitFor();
        await page.evaluate(() => {
            document.documentElement.style.zoom = '2';
        });
        await expectNoHorizontalOverflow(page);
        await expect(page.locator('[data-nav-rail]')).toBeVisible();
    });

    test('400% browser zoom keeps chat usable at 1280px (reflow equivalent)', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 800});
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
        await page.evaluate(() => {
            document.documentElement.style.zoom = '4';
        });
        await expectNoHorizontalOverflow(page);
        await expect(page.locator('textarea[aria-label="Message"]')).toBeVisible();
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
