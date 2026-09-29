// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Navigation-state sharing (issue #479): the conversation, page context,
// drafts and result position survive full Django page transitions, never
// cross accounts, and never appear in URLs or history.state.
import {expect, Page, test} from '@playwright/test';
import {E2E_PASSWORD, login, requireDjangoTier} from './support';

const COMPANY = 'E2E Alpha Corp';
const STRIP = '[data-testid="assistant-context-strip"]';

async function askAboutCompany(page: Page): Promise<void> {
    await page.goto('/');
    await page.locator(`[aria-label="View details for ${COMPANY}"]:visible`).first().click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.getByTestId('company-chat-cta').click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
}

async function logout(page: Page): Promise<void> {
    await page.locator('.app-nav-rail form.app-nav-logout-form button[type="submit"]').first().click();
    await page.waitForURL((url) => url.pathname !== '/chat/');
}

test.describe('navigation state (issue #479)', () => {
    test.beforeEach(() => {
        requireDjangoTier();
    });

    test('Ask opens the assistant in place with company context and the URL stays clean', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await askAboutCompany(page);
        expect(new URL(page.url()).pathname).toBe('/');
        const keys = Array.from(new URL(page.url()).searchParams.keys());
        for (const key of keys) {
            expect(['search', 'accelerated_vesting', 'page', 'company']).toContain(key);
        }
        const historyState = await page.evaluate(() => JSON.stringify(window.history.state ?? {}));
        expect(historyState).not.toMatch(/draft|conversation/i);
    });

    test('context, draft and conversation survive Rankings → Job Search → Back → forward', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await askAboutCompany(page);

        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toBeEnabled();
        await composer.fill('remember this draft');
        await expect.poll(() => page.evaluate(() => Object.keys(window.localStorage).filter((k) => k.startsWith('crank:jobsearch:draft:')).length)).toBeGreaterThan(0);

        await page.reload();
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
        await expect(composer).toHaveValue('remember this draft');

        await page.goBack();
        await expect(page.locator('#organization-list')).toBeVisible();
        // Back restores the same context on the rankings page (after the
        // account hydrates), so the strip is there before going forward.
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
        await page.goForward();
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
        await expect(composer).toHaveValue('remember this draft');

        const record = await page.evaluate(() => window.sessionStorage.getItem('crank:workspace:v1') ?? '');
        expect(record).not.toContain('remember this draft');
        expect(record).not.toMatch(/conversation/i);
    });

    test('Clear context removes the strip and the company from the URL', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await askAboutCompany(page);
        await page.getByTestId('assistant-clear-context').click();
        await expect(page.locator(STRIP)).toHaveCount(0);
        expect(new URL(page.url()).searchParams.has('company')).toBe(false);
        await expect(page.locator('#organization-list')).toBeVisible();
    });

    test('Clear context on a pinned /chat/?company= URL survives a reload', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/');
        await page.locator(`[aria-label="View details for ${COMPANY}"]:visible`).first().click();
        const href = await page.getByTestId('company-chat-cta').getAttribute('href');
        expect(href).toMatch(/^\/chat\/\?company=\d+$/);
        await page.goto(href as string);
        await expect(page.locator(STRIP)).toContainText(COMPANY);
        await page.getByTestId('assistant-clear-context').click();
        await expect(page.locator(STRIP)).toHaveCount(0);
        expect(new URL(page.url()).searchParams.has('company')).toBe(false);
        await page.reload();
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
        await expect(page.locator(STRIP)).toHaveCount(0);
    });

    // Issue #479 journey. The comparison leg stays open until #490 ships a
    // comparison surface; every leg that exists today is covered here.
    test('Rankings → company dialog → jobs → Back → Forward restores each step', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/');
        await page.locator(`[aria-label="View details for ${COMPANY}"]:visible`).first().click();
        await expect(page.getByRole('dialog')).toBeVisible();
        await expect.poll(() => new URL(page.url()).searchParams.has('company')).toBe(true);
        const rankingsUrl = page.url();

        await page.goto('/chat/');
        await expect(page.getByTestId('job-match-panel')).toBeVisible();
        await expect(page.getByTestId('ranked-job-matches')).toBeVisible();

        await page.goBack();
        expect(page.url()).toBe(rankingsUrl);
        await expect(page.getByRole('dialog')).toBeVisible();
        await expect(page.locator('#organization-details-title')).toHaveText(COMPANY);

        await page.goForward();
        await expect(page.getByTestId('job-match-panel')).toBeVisible();
    });

    test('an account change in another tab purges this tab private state', async ({page, context}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await askAboutCompany(page);
        await page.goto('/chat/');
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toBeEnabled();
        await composer.fill('tab one private draft');
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);

        const other = await context.newPage();
        await other.goto('/chat/');
        await logout(other);

        await expect(page.locator(STRIP)).toHaveCount(0);
        await expect(page.locator('body')).not.toContainText('tab one private draft');
        const record = await page.evaluate(() => window.sessionStorage.getItem('crank:workspace:v1') ?? '');
        expect(record).not.toContain('organizationId');
    });

    test('at 375px the sheet stays open after Ask and restores minimized after navigation', async ({page}) => {
        await page.setViewportSize({width: 375, height: 800});
        await login(page);
        await askAboutCompany(page);
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        await page.goto('/help/');
        await expect(page.getByTestId('assistant-panel')).toHaveCount(0);
        // Sheet mode restores minimized, never as a blocking sheet.
        await expect(page.getByTestId('assistant-restore')).toBeVisible();
        await page.getByTestId('assistant-restore').click();
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
    });

    test('an account switch never exposes the previous account context or draft', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await askAboutCompany(page);
        await page.goto('/chat/');
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toBeEnabled();
        await composer.fill('user A private draft');
        await logout(page);
        // Sign-out purged the record; only an empty anonymous stamp may remain.
        const afterLogout = await page.evaluate(() => window.sessionStorage.getItem('crank:workspace:v1') ?? '');
        expect(afterLogout).not.toContain('organizationId');
        expect(afterLogout).not.toContain('e2e_user');

        await login(page, 'e2e_user_b', E2E_PASSWORD);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
        await expect(page.locator(STRIP)).toHaveCount(0);
        await expect(page.locator('textarea[aria-label="Message"]')).toHaveValue('');
        await expect(page.locator('body')).not.toContainText('user A private draft');
    });

    test('two tabs keep independent company contexts', async ({page, context}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await askAboutCompany(page);
        const other = await context.newPage();
        await other.setViewportSize({width: 1280, height: 900});
        await other.goto('/');
        await expect(other.locator(STRIP)).toHaveCount(0);
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
    });

    test('Back restores the rankings scroll position', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 500});
        await login(page);
        await page.goto('/');
        await expect(page.locator('#organization-list')).toBeVisible();
        await page.evaluate(() => {
            document.body.style.minHeight = '3000px';
        });
        // Scroll a result far down into view and remember which one it is.
        const rows = page.locator('tr.organization-row');
        const total = await rows.count();
        const target = rows.nth(Math.max(0, total - 1));
        await target.scrollIntoViewIfNeeded();
        await expect.poll(() => page.evaluate(() => window.history.state?.crankPosition?.anchor ?? null)).not.toBeNull();
        const targetId = await page.evaluate(() => window.history.state.crankPosition.anchor as string);
        await page.goto('/help/');
        await page.goBack();
        await expect(page.locator('#organization-list')).toBeVisible();
        // The same organization — not merely some scroll offset — is on screen.
        await expect(page.locator(`tr.organization-row[data-organization-id="${targetId}"]`)).toBeInViewport();
    });
});
