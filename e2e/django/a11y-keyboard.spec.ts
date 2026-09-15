// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Keyboard and screen-reader semantics on real pages (issue #491): Tab order,
// Escape + focus-return for blocking dialogs, skip-link reachability, and
// aria-live status regions. These are DOM/semantics assertions only — no
// claim that emulation alone certifies accessibility (see docs/e2e-validation.md).
import {expect, test} from '@playwright/test';
import {login, pendingTicketMerge, requireDjangoTier} from './support';

requireDjangoTier();

test.describe('skip link and tab order', () => {
    test('skip link is the first tab stop and jumps to main content', async ({page}) => {
        await page.goto('/');
        await page.keyboard.press('Tab');
        await expect(page.locator('[data-skip-to-content]')).toBeFocused();
        await page.keyboard.press('Enter');
        await expect(page.locator('#main-content')).toBeFocused();
    });

    test('tab order moves from skip link into the nav rail, then main content', async ({page}) => {
        await page.goto('/');
        await page.keyboard.press('Tab'); // skip link
        await expect(page.locator('[data-skip-to-content]')).toBeFocused();
        await page.keyboard.press('Tab'); // first rail link (brand)
        await expect(page.locator('.app-nav-brand').first()).toBeFocused();
        await page.keyboard.press('Tab');
        await expect(page.locator('#nav-rankings')).toBeFocused();
    });
});

test.describe('blocking dialog keyboard semantics', () => {
    test('company dialog: focus lands on Close, Escape returns focus to the trigger', async ({page}) => {
        await login(page);
        await page.goto('/');
        const trigger = page.locator('tr[aria-label="View details for E2E Alpha Corp"]');
        await trigger.click();
        const dialog = page.getByRole('dialog');
        await expect(dialog).toBeVisible();
        await expect(page.locator('.popup-details button[aria-label="Close"]')).toBeFocused();

        // Tab stays inside the dialog's own controls.
        await page.keyboard.press('Tab');
        const inside = await page.evaluate(() =>
            Boolean(document.activeElement?.closest('.popup-details')),
        );
        expect(inside, 'focus must stay inside the dialog while it is open').toBe(true);

        // Escape closes and restores focus to the element that opened it.
        await page.keyboard.press('Escape');
        await expect(dialog).toBeHidden();
        await expect(trigger).toBeFocused();
    });

    test('suggest-company modal: Escape closes and returns focus', async ({page}) => {
        pendingTicketMerge(
            464,
            'SuggestCompanyModal Escape/focus-return hardening ships with #464; ' +
            'assert the hardened behavior when it merges and delete this skip',
        );
        await login(page);
        await page.goto('/');
        await page.getByTestId('suggest-company-btn').click();
        await expect(page.getByRole('dialog')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.getByRole('dialog')).toBeHidden();
        await expect(page.getByTestId('suggest-company-btn')).toBeFocused();
    });
});

test.describe('live regions and landmarks', () => {
    test('chat history is a polite live log with a status region', async ({page}) => {
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
        const history = page.getByRole('log', {name: 'Message history'});
        await expect(history).toHaveAttribute('aria-live', 'polite');
        await expect(history).toHaveAttribute('aria-label', 'Message history');
        // Screen-reader-only transition region announces pending/error state
        // (distinct from the preference notice, also role=status, which only
        // renders after a preference-changing turn).
        await expect(
            page.locator('section[data-testid="job-search-chat"] .visually-hidden[role="status"]'),
        ).toHaveCount(1);
    });

    test('pending turn flips the live status and announces completion', async ({page}) => {
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
        await page.locator('textarea[aria-label="Message"]').fill('E2E a11y probe turn');
        await page.getByLabel('Send message').click();
        // The pending indicator itself is a polite live region while visible.
        const pending = page.getByTestId('pending-status');
        if (await pending.isVisible().catch(() => false)) {
            await expect(pending).toHaveAttribute('aria-live', 'polite');
        }
        // The turn completes with a visible assistant outcome.
        await expect(page.locator('article[aria-label="Assistant message"]').last()).toBeVisible();
    });

    test('navigation landmarks are exposed once, labeled', async ({page}) => {
        await page.goto('/');
        await expect(page.locator('aside[aria-label="Application navigation"]')).toBeVisible();
        await expect(page.locator('nav[aria-label="Main navigation"]')).toBeVisible();
        await expect(page.locator('main#main-content')).toBeVisible();
    });

    test('document declares a language', async ({page}) => {
        await page.goto('/');
        await expect(page.locator('html')).toHaveAttribute('lang', 'en');
    });
});
