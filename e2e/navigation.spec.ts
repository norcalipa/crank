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

    test('mobile topbar band is hidden on desktop', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        // The reserved band must not consume vertical space while the rail is
        // visible (desktop layout unchanged, audit follow-up on #456).
        await expect(page.locator('.app-mobile-topbar')).toBeHidden();
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

    test('hamburger sits in its own reserved topbar band on mobile', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        // Audit follow-up on #456: the shell must render the toggle inside a
        // sticky `.app-mobile-topbar` band so it participates in normal flow
        // instead of floating fixed over page content.
        const topbar = page.locator('.app-mobile-topbar');
        await expect(topbar).toBeVisible();
        const toggle = page.locator('[data-nav-toggle]');
        await expect(toggle).toBeVisible();

        const data = await page.evaluate(() => {
            const topbar = document.querySelector<HTMLElement>('.app-mobile-topbar');
            const toggle = document.querySelector<HTMLElement>('[data-nav-toggle]');
            if (!topbar || !toggle) return null;
            const topbarRect = topbar.getBoundingClientRect();
            const toggleRect = toggle.getBoundingClientRect();
            return {
                topbarPosition: getComputedStyle(topbar).position,
                togglePosition: getComputedStyle(toggle).position,
                toggleInTopbar: toggle.closest('.app-mobile-topbar') === topbar,
                topbarHeight: topbarRect.height,
                toggleTop: toggleRect.top,
                toggleBottom: toggleRect.bottom,
                topbarTop: topbarRect.top,
                topbarBottom: topbarRect.bottom,
            };
        });
        expect(data, 'topbar and toggle must exist').not.toBeNull();
        expect(data!.topbarPosition).toBe('sticky');
        expect(data!.togglePosition, 'toggle is not a fixed overlay').toBe('static');
        expect(data!.toggleInTopbar).toBe(true);
        expect(data!.topbarHeight).toBeGreaterThanOrEqual(48);
        // The toggle stays inside the band's vertical extent.
        expect(data!.toggleTop).toBeGreaterThanOrEqual(data!.topbarTop - 1);
        expect(data!.toggleBottom).toBeLessThanOrEqual(data!.topbarBottom + 1);
    });

    test('topbar band pushes content below it without overlap', async ({page}) => {
        await page.goto(NAV_FIXTURE);
        const data = await page.evaluate(() => {
            const topbar = document.querySelector<HTMLElement>('.app-mobile-topbar');
            const toggle = document.querySelector<HTMLElement>('[data-nav-toggle]');
            const content = document.getElementById('main-content');
            if (!topbar || !toggle || !content) return null;
            const topbarRect = topbar.getBoundingClientRect();
            const toggleRect = toggle.getBoundingClientRect();
            const contentRect = content.getBoundingClientRect();
            return {
                topbarBottom: topbarRect.bottom,
                contentTop: contentRect.top,
                toggleBottom: toggleRect.bottom,
                toggleRight: toggleRect.right,
                contentLeft: contentRect.left,
            };
        });
        expect(data, 'topbar, toggle and main content must exist').not.toBeNull();
        expect(data!.contentTop, 'main content starts below the reserved band').toBeGreaterThanOrEqual(data!.topbarBottom - 1);
        expect(data!.toggleBottom).toBeLessThanOrEqual(data!.contentTop + 1);
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

// Audit follow-up on #456: the mobile topbar contract previously existed only
// in the e2e fixtures while the live template kept rendering the hamburger as a
// fixed overlay. These tests assert the *live* template file itself (served
// raw by the static webServer), so fixture-only markup can no longer mask a
// template regression.
test.describe('navigation shell — live template topbar contract', () => {
    test('live _navigation.html wraps the hamburger in the mobile topbar', async ({request}) => {
        const response = await request.get('/templates/_navigation.html');
        expect(response.ok(), 'live template must be reachable from the static server').toBe(true);
        const template = await response.text();

        const topbarStart = template.indexOf('<header class="app-mobile-topbar">');
        expect(topbarStart, 'live template must render the mobile topbar header').toBeGreaterThanOrEqual(0);
        const topbarEnd = template.indexOf('</header>', topbarStart);
        expect(topbarEnd).toBeGreaterThan(topbarStart);
        const topbarMarkup = template.slice(topbarStart, topbarEnd);
        expect(topbarMarkup, 'topbar header must contain the nav toggle').toContain('data-nav-toggle');
        expect(topbarMarkup, 'topbar header must contain the nav toggle button class').toContain('app-nav-toggle');
        expect(topbarMarkup).toContain('aria-controls="mobile-nav"');

        // The toggle must exist exactly once — inside the topbar, not repeated
        // as a free-floating direct child of .app-shell (the audit finding).
        expect(template.split('data-nav-toggle').length - 1, 'live template renders exactly one toggle').toBe(1);
    });

    test('e2e navigation fixture mirrors the live topbar contract', async ({request}) => {
        const [liveResponse, fixtureResponse] = await Promise.all([
            request.get('/templates/_navigation.html'),
            request.get(NAV_FIXTURE),
        ]);
        expect(liveResponse.ok()).toBe(true);
        expect(fixtureResponse.ok()).toBe(true);
        const live = await liveResponse.text();
        const fixture = await fixtureResponse.text();

        for (const [source, markup] of [['live template', live], ['e2e fixture', fixture]] as Array<[string, string]>) {
            const start = markup.indexOf('<header class="app-mobile-topbar">');
            expect(start, `${source} must contain the mobile topbar header`).toBeGreaterThanOrEqual(0);
            const header = markup.slice(start, markup.indexOf('</header>', start));
            expect(header, `${source} topbar must wrap the nav toggle`).toContain('data-nav-toggle');
        }
    });
});
