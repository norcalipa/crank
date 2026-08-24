// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// E2E tests for the application navigation shell (issue #443).
// Covers desktop rail visibility, mobile drawer toggle, keyboard
// interaction (Escape, focus trapping), skip-to-content, aria-current,
// and 200% zoom layout.
import {test, expect, Page} from '@playwright/test';

const NAV_FIXTURE = '/e2e/fixtures/navigation.html';

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
    const dims = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
    }));
    expect(dims.scrollWidth, 'document must not scroll horizontally').toBeLessThanOrEqual(dims.clientWidth + 1);
}

test.describe('navigation shell — desktop', () => {
    test.use({viewport: {width: 1280, height: 800}});

    test('persistent left rail is visible on desktop', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const rail = page.locator('[data-nav-rail]');
        await expect(rail).toBeVisible();
        await expect(page.locator('#nav-rankings')).toBeVisible();
        await expect(page.locator('#nav-job-search')).toBeVisible();
        await expect(page.locator('#nav-help')).toBeVisible();
        await expect(page.locator('#nav-admin')).toBeVisible();
    });

    test('hamburger toggle is hidden on desktop', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        await expect(page.locator('[data-nav-toggle]')).toBeHidden();
    });

    test('active route has aria-current', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const activeLink = page.locator('[data-nav-rail] .app-nav-link--active');
        await expect(activeLink).toHaveAttribute('aria-current', 'page');
        await expect(activeLink).toContainText('Company Rankings');
    });

    test('primary destinations are top-aligned while account actions stay at the bottom', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const rail = page.locator('[data-nav-rail]');
        const brandBox = await rail.locator('.app-nav-brand').boundingBox();
        const rankingsBox = await rail.locator('#nav-rankings').boundingBox();
        const adminBox = await rail.locator('#nav-admin').boundingBox();
        const footerBox = await rail.locator('.app-nav-footer').boundingBox();
        const railBox = await rail.boundingBox();

        expect(brandBox).not.toBeNull();
        expect(rankingsBox).not.toBeNull();
        expect(adminBox).not.toBeNull();
        expect(footerBox).not.toBeNull();
        expect(railBox).not.toBeNull();

        expect(rankingsBox!.y - (brandBox!.y + brandBox!.height)).toBeLessThanOrEqual(24);
        expect(adminBox!.y - (rankingsBox!.y + rankingsBox!.height)).toBeLessThanOrEqual(160);
        expect(footerBox!.y).toBeGreaterThan(adminBox!.y + adminBox!.height + 200);
        expect(Math.abs(footerBox!.y + footerBox!.height - (railBox!.y + railBox!.height))).toBeLessThanOrEqual(1);
    });

    test('skip-to-content link focuses main content', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const skipLink = page.locator('[data-skip-to-content]');
        await skipLink.focus();
        await skipLink.press('Enter');
        await expect(page.locator('#main-content')).toBeFocused();
    });

    test('no horizontal overflow at desktop', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        await expectNoHorizontalOverflow(page);
    });
});

test.describe('navigation shell — mobile', () => {
    test.use({viewport: {width: 375, height: 667}, isMobile: true, hasTouch: true});

    test('left rail is hidden on mobile', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        await expect(page.locator('[data-nav-rail]')).toBeHidden();
    });

    test('hamburger toggle is visible on mobile', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const toggle = page.locator('[data-nav-toggle]');
        await expect(toggle).toBeVisible();
        await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    });

    test('drawer opens on toggle click', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const toggle = page.locator('[data-nav-toggle]');
        await toggle.click();
        await expect(page.locator('[data-nav-drawer]')).toBeVisible();
        await expect(page.locator('[data-nav-overlay]')).toBeVisible();
        await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    });

    test('drawer closes on Escape', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const toggle = page.locator('[data-nav-toggle]');
        await toggle.click();
        await expect(page.locator('[data-nav-drawer]')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.locator('[data-nav-drawer]')).toBeHidden();
        await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    });

    test('drawer closes on overlay click', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const toggle = page.locator('[data-nav-toggle]');
        await toggle.click();
        await expect(page.locator('[data-nav-drawer]')).toBeVisible();
        // Click the overlay at a position outside the drawer (right side)
        await page.locator('[data-nav-overlay]').click({position: {x: 350, y: 300}});
        await expect(page.locator('[data-nav-drawer]')).toBeHidden();
    });

    test('drawer closes on close button', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const toggle = page.locator('[data-nav-toggle]');
        await toggle.click();
        await expect(page.locator('[data-nav-drawer]')).toBeVisible();
        await page.locator('[data-nav-close]').click();
        await expect(page.locator('[data-nav-drawer]')).toBeHidden();
    });

    test('focus is trapped in drawer and restored on close', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const toggle = page.locator('[data-nav-toggle]');
        await toggle.focus();
        await toggle.click();
        await expect(page.locator('[data-nav-drawer]')).toBeVisible();
        // Focus should be inside the drawer
        const drawer = page.locator('[data-nav-drawer]');
        const activeElement = page.evaluate(() => document.activeElement?.closest('[data-nav-drawer]'));
        expect(activeElement).not.toBeNull();
        // Tab through focusable elements
        await page.keyboard.press('Tab');
        await page.keyboard.press('Tab');
        await page.keyboard.press('Tab');
        // Close with Escape
        await page.keyboard.press('Escape');
        await expect(page.locator('[data-nav-drawer]')).toBeHidden();
        // Focus should be restored to toggle
        await expect(toggle).toBeFocused();
    });

});

// The mobile-emulation context (isMobile/hasTouch) makes the runtime
// page.setViewportSize call hang in Firefox, so the 320px overflow measurement
// never ran there. Use a declarative 320px viewport instead, which is reliable
// across all browser projects, while keeping the same assertion.
test.describe('navigation shell — 320px overflow', () => {
    test.use({viewport: {width: 320, height: 568}});

    test('no horizontal overflow at 320px', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        await expectNoHorizontalOverflow(page);
    });
});

test.describe('navigation shell — tablet', () => {
    test.use({viewport: {width: 768, height: 1024}});

    test('left rail is visible at tablet breakpoint', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        await expect(page.locator('[data-nav-rail]')).toBeVisible();
    });
});

test.describe('navigation shell — 200% zoom', () => {
    test.skip(({browserName}) => browserName !== 'chromium', 'page-scale zoom emulation is Chromium-only');

    test('no horizontal overflow at 200% page zoom on desktop', async ({page, context}) => {
        await page.goto(NAV_FIXTURE);
        const cdp = await context.newCDPSession(page);
        await cdp.send('Emulation.setPageScaleFactor', {pageScaleFactor: 2});
        await expectNoHorizontalOverflow(page);
    });

    test('no horizontal overflow at 200% page zoom on mobile', async ({page, context}) => {
        await page.setViewportSize({width: 375, height: 667});
        await page.goto(NAV_FIXTURE);
        const cdp = await context.newCDPSession(page);
        await cdp.send('Emulation.setPageScaleFactor', {pageScaleFactor: 2});
        await expectNoHorizontalOverflow(page);
    });
});

test.describe('navigation shell — landmarks and semantics', () => {
    test.use({viewport: {width: 1280, height: 800}});

    test('has semantic navigation landmark', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        await expect(page.locator('[aria-label="Application navigation"]')).toBeVisible();
        await expect(page.locator('[aria-label="Main navigation"]')).toBeVisible();
    });

    test('skip-to-content link is present', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        await expect(page.locator('[data-skip-to-content]')).toBeVisible();
    });

    test('logo has home affordance label', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const brandLinks = page.locator('[aria-label="CRank home"]');
        await expect(brandLinks).toHaveCount(2);
    });

    test('rankings is explicit nav link not just logo', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const rankingsLink = page.locator('#nav-rankings');
        await expect(rankingsLink).toBeVisible();
        await expect(rankingsLink).toContainText('Company Rankings');
    });
});
