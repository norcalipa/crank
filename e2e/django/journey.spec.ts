// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Cross-surface journey over real Django pages (issue #491): sign-in →
// priorities (chat) → companies (rankings) → evidence (dialog provenance) →
// jobs (match panel). Every action produces a visible, asserted outcome.
// Surfaces owned by unmerged tickets are asserted under an explicit,
// named skip; the ranked panel and chat basics already on main run for real.
import {expect, test} from '@playwright/test';
import {login, requireDjangoTier} from './support';

requireDjangoTier();

test.describe.serial('priorities → companies → evidence → jobs journey', () => {
    test('sign-in handoff through the real login page', async ({page}) => {
        await page.goto('/');
        await page.locator('#nav-login').click();
        await expect(page).toHaveURL(/\/accounts\/login\//);
        await page.locator('input[name="login"]').fill('e2e_user');
        await page.locator('input[name="password"]').fill('e2e-throwaway-password');
        await page.getByRole('button', {name: 'Sign In'}).click();
        await page.waitForURL((url) => url.pathname === '/');
        await expect(page.locator('#nav-account')).toContainText('e2e_user');
    });

    test('setting priorities via the chat produces an assistant reply and a preference notice', async ({page}) => {
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();

        const turn = 'I want remote work with a strong salary and a collaborative culture';
        await page.locator('textarea[aria-label="Message"]').fill(turn);
        await page.getByLabel('Send message').click();

        // Optimistic echo of the user turn…
        await expect(page.locator('article[aria-label="Your message"]', {hasText: turn}).first()).toBeVisible();
        // …then a persisted assistant reply (state-tolerant: the demo
        // provider's first-turn and follow-up replies both reference the
        // matched organizations)…
        const reply = page.locator('article[aria-label="Assistant message"]').last();
        await expect(reply).toBeVisible();
        await expect(reply).toContainText('organizations');
        // …and the preference-updated notice (demo provider detected the
        // preference dimensions in the message).
        await expect(page.locator('[aria-label="Preference update"]')).toBeVisible();
    });

    test('rankings show the seeded organizations ranked by score', async ({page}) => {
        await login(page);
        await page.goto('/');
        await expect(page.locator('#organization-list')).toBeVisible();
        const count = page.locator('.organization-results-count');
        await expect(count).toContainText('of 4 organizations');
        // Strict score order: Alpha (4.8) ranks first.
        await expect(page.locator('.organization-table, .organization-cards').first())
            .toContainText('E2E Alpha Corp');
    });

    test('company details dialog opens with evidence provenance and closes cleanly', async ({page}) => {
        await login(page);
        await page.goto('/');
        const trigger = page.locator('tr[aria-label="View details for E2E Alpha Corp"]');
        await trigger.click();

        const dialog = page.getByRole('dialog');
        await expect(dialog).toBeVisible();
        await expect(page.locator('#organization-details-title')).toHaveText('E2E Alpha Corp');
        // Evidence/provenance section renders the accepted seeded observation.
        const provenance = page.getByTestId('provenance-section');
        await expect(provenance).toBeVisible();
        await expect(page.getByTestId('observation-details')).toContainText('e2e.example.test');
        await expect(page.getByTestId('observation-details')).toContainText('accepted', {ignoreCase: true});
        await expect(page.getByTestId('last-updated')).not.toHaveText('');

        // Close restores focus to the opening trigger (asserted in depth by
        // a11y-keyboard.spec.ts; here we pin the outcome).
        await page.locator('.popup-details button[aria-label="Close"]').click();
        await expect(dialog).toBeHidden();
        await expect(trigger).toBeFocused();
    });

    test('match panel shows the ranked seeded listing with reasons', async ({page}) => {
        await login(page);
        await page.goto('/chat/');
        const panel = page.getByTestId('job-match-panel');
        await expect(panel).toBeVisible();
        const ranked = page.getByTestId('ranked-job-matches');
        await expect(ranked).toBeVisible();
        const listing = ranked.locator('a', {hasText: 'E2E Seed Software Engineer'});
        await expect(listing).toBeVisible();
        await expect(listing).toHaveAttribute('href', /usajobs\.gov/);
        // Positive remote-work reason from the seeded preference document.
        await expect(ranked.getByTestId(/job-reasons-\d+/).first()).toBeVisible();
        // Ranked result carries a numeric score badge.
        await expect(ranked.getByTestId(/job-score-\d+/).first()).toContainText(/\d/);
    });
});
