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
            window.scrollTo(0, 400);
        });
        await expect.poll(() => page.evaluate(() => window.history.state?.crankPosition?.scrollY ?? 0)).toBeGreaterThan(300);
        await page.goto('/help/');
        await page.goBack();
        await expect(page.locator('#organization-list')).toBeVisible();
        await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
    });
});
