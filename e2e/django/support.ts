// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Shared helpers for the Django-backed Playwright tier (issue #491).
import {expect, Page, test} from '@playwright/test';

/** E2E account created by `manage.py seed_e2e` (throwaway, dev-only). */
export const E2E_USERNAME = 'e2e_user';
export const E2E_PASSWORD = 'e2e-throwaway-password';

/**
 * Skip rule for the fixture tier: the fixture-tier config (playwright.config.ts)
 * scans all of ./e2e and has no Django backend or seeded data. Specs collected
 * there skip with an explicit named reason instead of failing or silently
 * passing against the wrong server.
 */
export const DJANGO_TIER_REASON =
    'Django tier only: requires the seeded Django server from playwright.django.config.ts ' +
    '(the fixture tier serves static fixtures only)';

export function requireDjangoTier(): void {
    test.skip(!process.env.PW_DJANGO_TIER, DJANGO_TIER_REASON);
}

/**
 * Skip rule for surfaces whose owning fix tickets have not merged yet.
 * The skip text must name the ticket so the reason is explicit and greppable;
 * delete the call site when the ticket merges.
 */
export function pendingTicketMerge(ticket: number, what: string): void {
    test.skip(true, `pending #${ticket} merge: ${what}; delete this skip when #${ticket} lands`);
}

/** Sign in through the real allauth login page and land on the rankings. */
export async function login(page: Page, username = E2E_USERNAME, password = E2E_PASSWORD): Promise<void> {
    await page.goto('/accounts/login/');
    await page.locator('input[name="login"]').fill(username);
    await page.locator('input[name="password"]').fill(password);
    await page.getByRole('button', {name: 'Sign In'}).click();
    await page.waitForURL((url) => url.pathname === '/');
    await expect(page.locator('#nav-account')).toContainText(username);
}

/** Assert the document itself never scrolls horizontally. */
export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
    const dims = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
    }));
    expect(
        dims.scrollWidth,
        `document scrollWidth ${dims.scrollWidth} must not exceed clientWidth ${dims.clientWidth}`,
    ).toBeLessThanOrEqual(dims.clientWidth + 1);
}

/** Measure a bounding box in CSS pixels via getBoundingClientRect. */
export async function rectOf(page: Page, selector: string): Promise<DOMRect> {
    return page.locator(selector).first().evaluate((el) => el.getBoundingClientRect().toJSON());
}

/** True when two rects overlap in both axes. */
export function rectsIntersect(a: DOMRect, b: DOMRect): boolean {
    return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}
