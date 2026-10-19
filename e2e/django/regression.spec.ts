// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Regression locks for the September 12 audit failures (issue #491).
//
// All checks are geometric or behavioral (bounding boxes, DOM state after
// reload) so they demonstrably fail against the pre-fix behavior and pass
// against the integrated fix.
import {expect, Page, test} from '@playwright/test';
import {
    expectNoHorizontalOverflow,
    login,
    rectOf,
    requireDjangoTier,
} from './support';

requireDjangoTier();

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
    // (JobSearchChat.tsx sendTurn catch path). Failed turns accumulate in
    // e2e_user's conversation, so every locator is scoped to this run's probe.
    test('failed turn keeps the persisted user message visible, with Retry, across reload', async ({page}) => {
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
        await expect(sent).toHaveCount(1);
        await expect(sent).toBeVisible();

        // …Retry is offered and re-issues the turn (outage persists → error
        // returns, message still present, no duplicate turn)…
        await sent.getByTestId('retry-response-button').click();
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');
        await expect(sent).toHaveCount(1);
        await expect(sent.getByTestId('failed-turn')).toBeVisible();

        // …and the turn survives a reload from the server-persisted history
        // with Retry still offered, and pressing it after the reload fails
        // the same honest way without a duplicate turn.
        await page.reload();
        await expect(sent).toHaveCount(1);
        const retry = sent.getByTestId('retry-response-button');
        await expect(retry).toBeEnabled();
        await retry.click();
        await expect(page.getByTestId('chat-error')).toHaveAttribute('data-error-type', 'assistant_unavailable');
        await expect(sent).toHaveCount(1);
        await expect(sent.getByTestId('failed-turn')).toBeVisible();
    });

    // The server stops a turn after JOB_SEARCH_TURN_MAX_ATTEMPTS attempts and
    // says so through `retry_available`; the test reads the cap from that
    // field instead of hard-coding it, then recovers with "Edit as new message".
    test('retry limit reached: Retry is exhausted and Edit as new message recovers the content', async ({page}) => {
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();

        const probe = `E2E retry limit probe ${Date.now()}`;
        await page.locator('textarea[aria-label="Message"]').fill(probe);
        await page.getByLabel('Send message').click();
        const error = page.getByTestId('chat-error');
        await expect(error).toBeVisible();
        const sent = page.locator('article[aria-label="Your message"]', {hasText: probe});
        const retry = sent.getByTestId('retry-response-button');
        await expect(retry).toBeEnabled();

        // Press Retry until the server reports the cap: the button turns into
        // the disabled "Retry limit reached". The bound only guards the loop.
        let presses = 0;
        while (await retry.isEnabled()) {
            expect(presses, 'the server never reported retry_available=false').toBeLessThan(20);
            presses += 1;
            await retry.click();
            await expect(retry).toBeVisible();
            await expect(error).toBeVisible();
            await expect(retry).toHaveAttribute('aria-label', /Retry response|Retry limit reached/);
        }
        expect(presses, 'at least one retry was possible before the cap').toBeGreaterThanOrEqual(1);
        await expect(retry).toBeDisabled();
        await expect(retry).toHaveAttribute('aria-label', 'Retry limit reached');
        await expect(sent).toHaveCount(1);
        await expect(sent.getByTestId('failed-turn')).toContainText('after several retries');

        // The server's own message states the cap: the first send plus every
        // press is one attempt.
        await expect(error).toHaveAttribute('data-error-type', 'retry_limit_reached');
        await expect(error).toContainText(`after ${presses + 1} attempts`);

        // The exhausted state is the persisted one: it survives a reload.
        await page.reload();
        await expect(sent.getByTestId('retry-response-button')).toBeDisabled();

        // A failed send's draft survives a reload, so empty the composer
        // first: only "Edit as new message" may put the content back.
        const composer = page.locator('textarea[aria-label="Message"]');
        await composer.fill('');
        await expect(composer).toHaveValue('');
        await sent.getByTestId('edit-as-new-button').click();
        await expect(composer).toHaveValue(probe);
        await expect(composer).toBeFocused();
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
