// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Regression locks for the September 12 audit failures (issue #491).
//
// All checks are geometric or behavioral (bounding boxes, DOM state after
// reload) so they demonstrably fail against the pre-fix behavior and pass
// against the integrated fix. Surfaces whose owning tickets have not merged
// are implemented in full and gated behind an explicit, named `test.skip`.
import {expect, Page, test} from '@playwright/test';
import {
    expectNoHorizontalOverflow,
    login,
    pendingTicketMerge,
    rectsIntersect,
    rectOf,
    requireDjangoTier,
} from './support';

requireDjangoTier();

test.describe('modal z-order vs navigation shell', () => {
    // (a) Old behavior: `.popup-overlay` (z-index 1000) paints under
    // `.app-nav-rail` (1100) and the mobile nav toggle/overlay (1200+), so the
    // dialog title/Close visually collide with navigation chrome. The
    // bounding-box assertions below fail exactly when the dialog shares space
    // with the rail/toggle; they are gated until the owning fix merges.
    test.describe.configure({mode: 'serial'});

    test('company dialog never intersects the nav rail or mobile toggle', async ({page}) => {
        pendingTicketMerge(
            500,
            'the dialog paints under the nav rail (desktop) and the nav toggle (mobile); ' +
            'unskip by deleting this call once #500 merges',
        );

        // Desktop (1280px): dialog title and Close must sit clear of the rail.
        await page.setViewportSize({width: 1280, height: 800});
        await login(page);
        await page.locator('tr[aria-label="View details for E2E Alpha Corp"]').click();
        await expect(page.getByRole('dialog')).toBeVisible();
        const rail = await rectOf(page, '[data-nav-rail]');
        const title = await rectOf(page, '#organization-details-title');
        const close = await rectOf(page, '.popup-details button[aria-label="Close"]');
        expect(rectsIntersect(title, rail), 'dialog title must not sit under the nav rail').toBe(false);
        expect(rectsIntersect(close, rail), 'dialog Close must not sit under the nav rail').toBe(false);

        // Mobile (375px): the rail is hidden; the toggle must not overlap the
        // dialog header controls.
        await page.setViewportSize({width: 375, height: 667});
        await expect(page.locator('[data-nav-rail]')).toBeHidden();
        await expect(page.getByRole('dialog')).toBeVisible();
        const toggle = await rectOf(page, '[data-nav-toggle]');
        const mobileTitle = await rectOf(page, '#organization-details-title');
        const mobileClose = await rectOf(page, '.popup-details button[aria-label="Close"]');
        expect(rectsIntersect(mobileTitle, toggle), 'dialog title must not sit under the nav toggle').toBe(false);
        expect(rectsIntersect(mobileClose, toggle), 'dialog Close must not sit under the nav toggle').toBe(false);
    });
});

test.describe('mobile width regression', () => {
    // (b) Old behavior: main content collapsed to ~150px at 320px width.
    // Measured with getBoundingClientRect, never screenshots.
    for (const path of ['/', '/chat/']) {
        test(`main content keeps a meaningful width at 320px on ${path}`, async ({page}) => {
            await page.setViewportSize({width: 320, height: 568});
            if (path === '/chat/') {
                await login(page);
            }
            await page.goto(path);
            await expectNoHorizontalOverflow(page);
            const main = await rectOf(page, '#main-content');
            expect(
                main.width,
                `#main-content on ${path} collapsed to ${main.width}px at 320px viewport`,
            ).toBeGreaterThanOrEqual(280);
        });
    }
});

test.describe('failed assistant turn persistence @outage', () => {
    // Runs only in the provider-outage pass (CRANK_E2E_PROVIDER_FAILURE=1);
    // in the normal pass the demo provider never fails, so these skip with
    // an explicit reason instead of silently passing.
    test.skip(
        process.env.CRANK_E2E_PROVIDER_FAILURE !== '1',
        'provider-outage pass only: run with CRANK_E2E_PROVIDER_FAILURE=1 ' +
        '(see playwright.django.config.ts and docs/e2e-validation.md)',
    );

    // (c) Old behavior: the optimistic user turn is rolled back client-side
    // while the server persists it, so the message vanishes from the live UI
    // (JobSearchChat.tsx sendTurn catch path). The immediate-visibility and
    // retry assertions are the #496 fix surface; the reload-survival
    // assertion exercises the server-side persistence contract that already
    // holds, and pins it so the fix cannot regress it either.
    test('failed turn keeps the persisted user message visible, with Retry, across reload', async ({page}) => {
        test.skip(true, 'pending #496 merge: the optimistic turn is rolled back on failure until #496 lands; ' +
            'unskip by deleting this call once #496 merges');

        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();

        const probe = `E2E outage probe ${Date.now()}`;
        await page.locator('textarea[aria-label="Message"]').fill(probe);
        await page.getByLabel('Send message').click();

        // The stable 503 envelope is shown and typed as assistant_unavailable.
        const error = page.getByTestId('chat-error');
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');

        // The user message stays visible immediately (no optimistic rollback)…
        const sent = page.locator('article[aria-label="Your message"]', {hasText: probe});
        await expect(sent).toBeVisible();

        // …Retry is offered and re-issues the turn (outage persists → error
        // returns, message still present, no duplicate turn)…
        await page.getByTestId('retry-button').click();
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');
        await expect(sent).toBeVisible();

        // …and both survive a reload from the server-persisted history.
        await page.reload();
        await expect(
            page.locator('article[aria-label="Your message"]', {hasText: probe}),
        ).toBeVisible();
    });

    test('server persists the failed turn: it survives a reload', async ({page}) => {
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();

        const probe = `E2E reload probe ${Date.now()}`;
        await page.locator('textarea[aria-label="Message"]').fill(probe);
        await page.getByLabel('Send message').click();

        const error = page.getByTestId('chat-error');
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');

        // Server-side persistence contract: the user turn is durable even
        // though the provider failed, so a reload restores it.
        await page.reload();
        await expect(
            page.locator('article[aria-label="Your message"]', {hasText: probe}),
        ).toBeVisible();
    });
});
