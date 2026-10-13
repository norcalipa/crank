// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Shared helpers for the Django-backed Playwright tier (issue #491); the fixture
// tier imports this file too.
import {expect, Page, test} from '@playwright/test';
import {CONFIRM_ARM_DELAY_MS} from '../../static/js/chat/confirmArmDelay';

/** E2E account created by `manage.py seed_e2e` (throwaway, dev-only). */
export const E2E_USERNAME = 'e2e_user';
export const E2E_PASSWORD = 'e2e-throwaway-password';

/**
 * Base URL of the seeded Django dev server. Mirrors playwright.django.config.ts
 * (`baseURL`); only the 400%-browser-zoom scenario needs it, because that
 * scenario builds its own browser context at a 320 CSS px layout viewport
 * with a 4x device scale factor, and manually created contexts do not inherit
 * the config's `use.baseURL`.
 */
export const DJANGO_BASE_URL = 'http://local.crank.fyi:4174';

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

/**
 * The conversation menu ignores a press on its confirm button until
 * CONFIRM_ARM_DELAY_MS after the confirmation opened (issue #568). Call this
 * once the confirmation is open and before a deliberate confirming press; the
 * wait starts after the confirmation rendered, so it outlasts the delay.
 */
export async function waitForConfirmArmed(page: Page): Promise<void> {
    await page.waitForTimeout(CONFIRM_ARM_DELAY_MS);
}

/**
 * The page records when each pointerdown and click happened (the browser's own event times), so a case
 * about a press made soon after a confirmation opened can prove that its press really started inside the
 * delay. Call before the first navigation of the test.
 */
interface PressEntry {type: string; t: number; id: string | null}

export async function recordPresses(page: Page): Promise<void> {
    await page.addInitScript(() => {
        const log: PressEntry[] = [];
        (window as unknown as {__pressLog: PressEntry[]}).__pressLog = log;
        for (const type of ['pointerdown', 'click']) {
            document.addEventListener(type, (e) => {
                const target = e.target as Element | null;
                log.push({type, t: e.timeStamp, id: target?.closest?.('[data-testid]')?.getAttribute('data-testid') ?? null});
            }, true);
        }
    });
}

/**
 * When the second press on the confirm button started (its pointerdown, or its click when no pointer was
 * involved) and when it ended, in ms after the click that opened the confirmation, by the page's own event times.
 */
export async function readPressGaps(page: Page): Promise<{start: number; end: number | null}> {
    const log = await page.evaluate(() => (window as unknown as {__pressLog: PressEntry[]}).__pressLog);
    const opening = log.map((e) => e.type === 'click' && e.id === 'conversation-delete').lastIndexOf(true);
    expect(opening, 'the opening click was recorded').toBeGreaterThanOrEqual(0);
    const after = log.slice(opening + 1);
    const down = after.find((e) => e.type === 'pointerdown' && e.id === 'confirm-action');
    const click = after.find((e) => e.type === 'click' && e.id === 'confirm-action');
    const first = down ?? click;
    expect(first, 'the second press was recorded').toBeDefined();
    return {start: first!.t - log[opening].t, end: click ? click.t - log[opening].t : null};
}

/** Before a case asserts that nothing was deleted: the press it made really started inside the delay. */
export async function expectStartedInside(page: Page, what: string): Promise<void> {
    const {start} = await readPressGaps(page);
    test.info().annotations.push({type: `start of ${what}`, description: `${start} ms`});
    expect(
        start,
        `the runner was too slow to prove anything: ${what} started ${start} ms after the opening click, and the delay is ${CONFIRM_ARM_DELAY_MS} ms`,
    ).toBeLessThan(CONFIRM_ARM_DELAY_MS);
}

/**
 * Waits until `ms` have passed since `since` (a Date.now() taken right after the opening press), so the
 * checks made in between count toward the gap instead of adding to it.
 */
export async function waitUntil(page: Page, since: number, ms: number): Promise<void> {
    await page.waitForTimeout(Math.max(0, ms - (Date.now() - since)));
}

/** The conversation delete requests the page sends from now on. */
export function trackDeletes(page: Page): string[] {
    const deletes: string[] = [];
    page.on('request', (request) => {
        const {pathname} = new URL(request.url());
        if (request.method() === 'POST' && /^\/api\/agent\/conversations\/\d+\/delete\/$/.test(pathname)) deletes.push(pathname);
    });
    return deletes;
}

export const inside = (box: {x: number; y: number; width: number; height: number}, x: number, y: number): boolean =>
    x >= box.x && x <= box.x + box.width && y >= box.y && y <= box.y + box.height;

export async function mouseDown(page: Page, x: number, y: number): Promise<void> {
    await page.mouse.move(x, y);
    await page.mouse.down();
}

export async function mousePress(page: Page, x: number, y: number): Promise<void> {
    await mouseDown(page, x, y);
    await page.mouse.up();
}

/** Before a press at (x, y) asserts anything: with the confirmation open, that point is on the confirm button. */
export async function expectPressOnConfirm(page: Page, x: number, y: number): Promise<void> {
    expect(inside((await page.getByTestId('confirm-action').boundingBox())!, x, y), 'the press point is inside the confirm button').toBe(true);
    const hit = await page.evaluate(([px, py]) => document.elementFromPoint(px, py)?.closest('[data-testid]')?.getAttribute('data-testid'), [x, y]);
    expect(hit, 'the confirm button is what the press hits').toBe('confirm-action');
}
