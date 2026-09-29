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

const OTHER_COMPANY = 'E2E Beta Labs';

// The workspace store's account as this tab currently believes it. The nav of
// a server-rendered page cannot flip, so the store is the tab's source of truth.
function storeAccount(page: Page): Promise<{status: string; key: string} | null> {
    return page.evaluate(() => (window as unknown as {
        __crankWorkspace__?: {snapshot?: {account?: {status: string; key: string}}};
    }).__crankWorkspace__?.snapshot?.account ?? null);
}

async function companyIdFor(page: Page, name: string): Promise<string> {
    await page.goto('/');
    await page.locator(`[aria-label="View details for ${name}"]:visible`).first().click();
    const href = await page.getByTestId('company-chat-cta').getAttribute('href');
    const match = /^\/chat\/\?company=(\d+)$/.exec(href ?? '');
    expect(match).not.toBeNull();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    return (match as RegExpExecArray)[1];
}

async function sendMessage(page: Page, text: string): Promise<void> {
    await page.locator('textarea[aria-label="Message"]').fill(text);
    await page.getByLabel('Send message').click();
    await expect(page.locator('article[aria-label="Your message"]', {hasText: text}).first()).toBeVisible();
    await expect(page.locator('article[aria-label="Assistant message"]').last()).toBeVisible();
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

    test('/chat/?company= replaces a persisted different company (name and id together)', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        const betaId = await companyIdFor(page, OTHER_COMPANY);
        await askAboutCompany(page);
        await page.goto(`/chat/?company=${betaId}`);
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
        // The server renders the URL company's name with its id: the strip
        // names that company (AC-2 copy), never the previously persisted one,
        // and never exposes the database id.
        await expect(page.locator(STRIP)).toContainText(`About ${OTHER_COMPANY}`);
        await expect(page.locator(STRIP)).not.toContainText(COMPANY);
        await expect(page.locator(STRIP)).not.toContainText(`#${betaId}`);
        const record = await page.evaluate(() => window.sessionStorage.getItem('crank:workspace:v1') ?? '');
        expect(JSON.parse(record).context).toEqual({organizationId: Number(betaId), organizationName: OTHER_COMPANY});
        await page.reload();
        await expect(page.locator(STRIP)).toContainText(`About ${OTHER_COMPANY}`);
        await expect(page.locator(STRIP)).not.toContainText(COMPANY);
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

    test('a delayed logout in another tab leaves this tab signed out, then follows the next sign-in', async ({page, context}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-match-panel')).toBeVisible();
        await expect.poll(() => storeAccount(page)).toEqual({status: 'authenticated', key: 'e2e_user'});

        const other = await context.newPage();
        await other.goto('/chat/');
        // Hold the logout POST: the signal must wait for the server-side change.
        await other.route('**/accounts/logout/', async (route) => {
            await new Promise((resolve) => setTimeout(resolve, 1500));
            await route.continue();
        });
        await logout(other);

        // This tab must end up on the signed-out account, not re-hydrated as the old user.
        await expect.poll(() => storeAccount(page)).toEqual({status: 'anonymous', key: ''});
        await expect(page.getByTestId('ranked-job-matches')).toHaveCount(0);
        const record = await page.evaluate(() => window.sessionStorage.getItem('crank:workspace:v1') ?? '');
        expect(record).not.toContain('e2e_user');

        await login(other, 'e2e_user_b', E2E_PASSWORD);
        await expect.poll(() => storeAccount(page)).toEqual({status: 'authenticated', key: 'e2e_user_b'});
    });

    test('reset in tab A: tab B reloads onto the new conversation with no stale draft', async ({page, context}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/chat/');
        await expect(page.locator('textarea[aria-label="Message"]')).toBeEnabled();
        await sendMessage(page, 'first conversation marker');

        const other = await context.newPage();
        await other.setViewportSize({width: 1280, height: 900});
        await other.goto('/chat/');
        await expect(other.locator('article[aria-label="Your message"]', {hasText: 'first conversation marker'}).first()).toBeVisible();
        await other.locator('textarea[aria-label="Message"]').fill('tab B unsent draft');

        page.once('dialog', (dialog) => dialog.accept());
        await page.getByRole('button', {name: 'Reset chat'}).click();
        await expect(page.locator('article[aria-label="Your message"]')).toHaveCount(0);

        await other.reload();
        await expect(other.getByTestId('job-search-chat')).toBeVisible();
        await expect(other.locator('textarea[aria-label="Message"]')).toBeEnabled();
        await expect(other.locator('article[aria-label="Your message"]')).toHaveCount(0);
        await expect(other.locator('body')).not.toContainText('first conversation marker');
    });

    test('delete in tab A: tab B reloads without the deleted conversation', async ({page, context}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/chat/');
        await expect(page.locator('textarea[aria-label="Message"]')).toBeEnabled();
        await sendMessage(page, 'doomed conversation marker');

        const other = await context.newPage();
        await other.goto('/chat/');
        await expect(other.locator('article[aria-label="Your message"]', {hasText: 'doomed conversation marker'}).first()).toBeVisible();

        page.once('dialog', (dialog) => dialog.accept());
        await page.getByRole('button', {name: 'Delete conversation'}).click();
        await expect(page.locator('article[aria-label="Your message"]')).toHaveCount(0);

        await other.reload();
        await expect(other.getByTestId('job-search-chat')).toBeVisible();
        await expect(other.locator('body')).not.toContainText('doomed conversation marker');
    });

    test('a reply landing after the context was cleared carries the stale-context note', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await askAboutCompany(page);
        let release: () => void = () => undefined;
        const gate = new Promise<void>((resolve) => {
            release = resolve;
        });
        await page.route(/\/api\/agent\/conversations\/\d+\/$/, async (route) => {
            if (route.request().method() === 'POST') {
                await gate;
            }
            await route.continue();
        });
        // The first send creates the conversation (not delayed), the second is the held turn.
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toBeEnabled();
        await composer.fill('tell me about this company');
        await page.getByLabel('Send message').click();
        await expect(page.locator('article[aria-label="Your message"]', {hasText: 'tell me about this company'}).first()).toBeVisible();

        await page.getByTestId('assistant-clear-context').click();
        await expect(page.locator(STRIP)).toHaveCount(0);
        release();

        await expect(page.locator('article[aria-label="Assistant message"]').last()).toBeVisible();
        await expect(page.getByTestId('stale-context-note')).toContainText(COMPANY);
        await expect(page.locator(STRIP)).toHaveCount(0);
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
        await sendMessage(page, 'user A sent message');
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
        await expect(page.locator('body')).not.toContainText('user A sent message');
        // B's resume returns B's own conversation.
        await expect(page.locator('textarea[aria-label="Message"]')).toBeEnabled();
        await sendMessage(page, 'user B sent message');
        await page.reload();
        await expect(page.locator('article[aria-label="Your message"]', {hasText: 'user B sent message'}).first()).toBeVisible();
        await expect(page.locator('body')).not.toContainText('user A sent message');
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
