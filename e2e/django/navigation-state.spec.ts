// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Navigation-state sharing (issue #479): the conversation, page context,
// drafts and result position survive full Django page transitions, never
// cross accounts, and never appear in URLs or history.state.
import {chromium, expect, Page, test} from '@playwright/test';
import {DJANGO_BASE_URL, E2E_PASSWORD, login, requireDjangoTier} from './support';

const COMPANY = 'E2E Alpha Corp';
const ACCOUNT_DIGEST = /^u:[0-9a-f]{16}$/;
const STRIP = '[data-testid="assistant-context-strip"]';

async function askAboutCompany(page: Page): Promise<void> {
    await page.goto('/');
    await page.locator(`[aria-label="View details for ${COMPANY}"]:visible`).first().click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.getByTestId('company-chat-cta').click();
    // The 375px sheet is itself a dialog, so only the details dialog must be gone.
    await expect(page.locator('#organization-details-title')).toHaveCount(0);
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
        await page.getByRole('button', {name: 'New conversation'}).click();
        await expect(page.locator('article[aria-label="Your message"]')).toHaveCount(0);

        await other.reload();
        await expect(other.getByTestId('job-search-chat')).toBeVisible();
        await expect(other.locator('textarea[aria-label="Message"]')).toBeEnabled();
        await expect(other.locator('article[aria-label="Your message"]')).toHaveCount(0);
        await expect(other.locator('body')).not.toContainText('first conversation marker');
        // The unsent draft belonged to the reset conversation: no stale draft.
        await expect(other.locator('textarea[aria-label="Message"]')).toHaveValue('');
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

    test('the company is in the store before the first /api/agent/ request (AC-2, #484 ordering)', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        const companyId = await companyIdFor(page, COMPANY);
        await page.goto('/');
        const seenAtFirstFetch: Array<number | null> = [];
        await page.route(/\/api\/agent\//, async (route) => {
            seenAtFirstFetch.push(await page.evaluate(() => (window as unknown as {
                __crankWorkspace__?: {snapshot?: {context?: {organizationId?: number} | null}};
            }).__crankWorkspace__?.snapshot?.context?.organizationId ?? null));
            await route.continue();
        });
        await page.locator(`[aria-label="View details for ${COMPANY}"]:visible`).first().click();
        await page.getByTestId('company-chat-cta').click();
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
        await expect.poll(() => seenAtFirstFetch.length).toBeGreaterThan(0);
        expect(seenAtFirstFetch[0]).toBe(Number(companyId));
    });

    for (const width of [1280, 1024]) {
        test(`a restored open panel does not take focus on load or Back at ${width}px`, async ({page}) => {
            await page.setViewportSize({width, height: 900});
            await login(page);
            await page.goto('/');
            await page.getByTestId('assistant-launcher').click();
            const composer = page.locator('textarea[aria-label="Message"]');
            await expect(composer).toBeEnabled();
            // An explicit open still moves focus into the composer.
            await expect(composer).toBeFocused();
            await sendMessage(page, `focus restore marker ${width}`);

            const settledOnBody = async (): Promise<void> => {
                await expect(page.getByTestId('assistant-panel')).toBeVisible();
                await expect(page.locator('article[aria-label="Your message"]', {hasText: `focus restore marker ${width}`}).first()).toBeVisible();
                await expect(composer).toBeEnabled();
                // Resume-driven focus used to land after a deferred tick.
                await page.waitForTimeout(500);
                expect(await page.evaluate(() => document.activeElement?.tagName)).toBe('BODY');
            };
            await page.goto('/help/');
            await settledOnBody();
            await page.goBack();
            await settledOnBody();
        });
    }

    for (const width of [1280, 1024]) {
        test(`Ask with a failing resume leaves focus on the alert action at ${width}px`, async ({page}) => {
            await page.setViewportSize({width, height: 900});
            await login(page);
            await page.goto('/');
            await page.route(/\/api\/agent\/conversations\/$/, async (route) => {
                if (route.request().method() === 'GET') {
                    await route.fulfill({status: 500, contentType: 'application/json', body: '{"detail":"boom"}'});
                    return;
                }
                await route.continue();
            });
            await page.locator(`[aria-label="View details for ${COMPANY}"]:visible`).first().click();
            await page.getByTestId('company-chat-cta').click();
            const action = page.getByRole('button', {name: 'Start a conversation'});
            await expect(action).toBeVisible();
            await expect(action).toBeFocused();
        });
    }

    test('a failed whoami keeps this tab context on /chat/ and /help/ (not treated as sign-out)', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await askAboutCompany(page);
        await page.goto('/chat/');
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
        await page.route('**/api/account/whoami/', (route) => route.fulfill({status: 503, body: 'down'}));
        await page.reload();
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
        expect(await storeAccount(page)).toEqual({status: 'authenticated', key: 'e2e_user'});
        await page.goto('/help/');
        await page.waitForTimeout(500);
        const record = JSON.parse(await page.evaluate(() => window.sessionStorage.getItem('crank:workspace:v1') ?? 'null'));
        expect(record.account.status).toBe('authenticated');
        expect(record.account.key).toMatch(/^d:[0-9a-f]{16}$/);
        expect(JSON.stringify(record)).not.toContain('e2e_user');
        expect(record.context.organizationName).toBe(COMPANY);
        await page.unroute('**/api/account/whoami/');
        await page.reload();
        await expect(page.locator(STRIP)).toContainText(`About ${COMPANY}`);
    });

    test('after another tab signs out, /chat/ shows the signed-out job matches, not an error with retry', async ({page, context}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('ranked-job-matches')).toBeVisible();
        const other = await context.newPage();
        await other.goto('/chat/');
        await logout(other);
        await expect(page.getByTestId('job-match-signed-out')).toBeVisible();
        await expect(page.getByText('We couldn’t load your job matches')).toHaveCount(0);
        await expect(page.getByRole('button', {name: /try again/i})).toHaveCount(0);
    });

    test('a second anonymous tab does not destroy the sign-in handoff draft (#465 AC-8)', async ({page, context}) => {
        await page.setViewportSize({width: 1280, height: 900});
        const anonymous = await context.newPage();
        await anonymous.goto('/');
        await expect(anonymous.locator('#organization-list')).toBeVisible();

        await page.goto('/chat/');
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toBeEnabled();
        const draft = 'handoff draft with another tab open';
        await composer.fill(draft);
        await expect.poll(() => page.evaluate(() => window.localStorage.getItem('crank:jobsearch:draft:pending'))).toBe(draft);
        await page.getByTestId('job-search-sign-in-cta').click();
        await page.locator('input[name="login"]').fill('e2e_user');
        await page.locator('input[name="password"]').fill(E2E_PASSWORD);
        await page.getByRole('button', {name: 'Sign In'}).click();
        await page.waitForURL((url) => url.pathname === '/chat/');

        // The other tab has been told and re-hydrated onto the signed-in account…
        await expect.poll(() => anonymous.evaluate(() => window.sessionStorage.getItem('crank:nav-account-seen'))).toMatch(ACCOUNT_DIGEST);
        // …and the draft still arrives in the composer that signed in.
        await expect(page.locator('textarea[aria-label="Message"]')).toHaveValue(draft);
    });

    test('a session that expires while another tab holds an unsent draft keeps that draft for re-login', async ({page, context}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/chat/');
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toBeEnabled();
        await composer.fill('draft that must survive expiry');
        await expect.poll(() => page.evaluate(() => Object.keys(window.localStorage).filter((k) => k.startsWith('crank:jobsearch:draft:') && !k.endsWith(':pending') && !k.includes('draftts')).length)).toBeGreaterThan(0);

        const other = await context.newPage();
        await other.goto('/');
        await expect.poll(() => other.evaluate(() => window.sessionStorage.getItem('crank:nav-account-seen'))).toMatch(ACCOUNT_DIGEST);
        await context.clearCookies({name: 'sessionid'});
        await other.reload();
        await expect.poll(() => other.evaluate(() => window.sessionStorage.getItem('crank:nav-account-seen'))).toBe('anon');
        // The announcing tab, not this receiver, owns the shared storage.
        await expect.poll(() => page.evaluate(() => window.sessionStorage.getItem('crank:nav-account-seen'))).toBe('anon');
        expect(await page.evaluate(() => Object.keys(window.localStorage).some((k) => /^crank:jobsearch:draft:\d+$/.test(k)))).toBe(true);

        await login(other);
        await page.goto('/chat/');
        await expect(page.locator('textarea[aria-label="Message"]')).toHaveValue('draft that must survive expiry');
    });

    test('a failed whoami that lands after the chat mounts keeps the signed-in draft slot and hides the signed-out introduction', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/chat/');
        await sendMessage(page, 'seed a conversation for the draft key');

        await page.addInitScript(() => {
            const w = window as unknown as {__hydrations: {unobserved: boolean}[]};
            w.__hydrations = [];
            document.addEventListener('crank:auth-hydrated', (e) => {
                w.__hydrations.push({unobserved: (e as CustomEvent).detail.unobserved === true});
            });
        });
        let failedWhoami = false;
        await page.route('**/api/account/whoami/', async (route) => {
            await new Promise((resolve) => setTimeout(resolve, 2000));
            failedWhoami = true;
            await route.fulfill({status: 503, body: 'down'});
        });
        await page.reload();
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toBeEnabled();
        await expect.poll(() => failedWhoami, {timeout: 10_000}).toBe(true);
        // Type only once the page has processed the failed hydration.
        await expect.poll(() => page.evaluate(() => (window as unknown as {
            __hydrations: {unobserved: boolean}[];
        }).__hydrations.some((h) => h.unobserved))).toBe(true);
        await expect(page.getByTestId('signed-out-introduction')).toHaveCount(0);
        await composer.fill('SIGNED-IN PRIVATE TEXT');
        await expect.poll(() => page.evaluate(() => Object.keys(window.localStorage)
            .filter((k) => /^crank:jobsearch:draft:\d+$/.test(k))
            .map((k) => window.localStorage.getItem(k)))).toContain('SIGNED-IN PRIVATE TEXT');
        expect(await page.evaluate(() => window.localStorage.getItem('crank:jobsearch:draft:pending'))).toBeNull();
        await expect(page.getByTestId('signed-out-introduction')).toHaveCount(0);
    });

    // `nonAppPage`: the tab sits on a document without app-nav (so its
    // `crank:nav-account-seen` never moves) while the account changes in
    // another tab. Storage events are swallowed only in documents that have
    // entered the bfcache, modelling an engine that does not replay them.
    for (const variant of [
        {name: 'a page of the app', offline: false, away: '/help/'},
        {name: 'a non-app page', offline: false, away: '/static/dist/manifest.json'},
        {name: 'a non-app page, offline at restore', offline: true, away: '/static/dist/manifest.json'},
        {name: 'a non-app page, offline, with no epoch change (only last-account differs)', offline: true, forgetEpoch: true, away: '/static/dist/manifest.json'},
    ]) {
        test(`a page restored from the back/forward cache is purged after another account signs in: tab on ${variant.name} (real bfcache)`, async () => {
            const browser = await chromium.launch({
                channel: 'chromium',
                ignoreDefaultArgs: ['--disable-back-forward-cache'],
                args: ['--host-resolver-rules=MAP local.crank.fyi 127.0.0.1'],
            });
            try {
                const context = await browser.newContext({baseURL: DJANGO_BASE_URL, viewport: {width: 1280, height: 900}});
                await context.addInitScript(() => {
                    const w = window as unknown as {__pageshows: boolean[]; __cached: boolean; __epochEvents: (string | null)[]};
                    w.__pageshows = [];
                    w.__epochEvents = [];
                    window.addEventListener('storage', (e) => {
                        if (e.key === 'crank:account-epoch') w.__epochEvents.push(e.newValue);
                    });
                    w.__cached = false;
                    window.addEventListener('pagehide', (e) => {
                        if (e.persisted) w.__cached = true;
                    });
                    window.addEventListener('storage', (e) => {
                        if (w.__cached) e.stopImmediatePropagation();
                    }, true);
                    window.addEventListener('pageshow', (e) => w.__pageshows.push(e.persisted));
                });
                const page = await context.newPage();
                await login(page);
                await askAboutCompany(page);
                const secret = 'PRIVATE MESSAGE FROM ACCOUNT A';
                await sendMessage(page, secret);
                const epochBefore = await page.evaluate(() => window.localStorage.getItem('crank:account-epoch'));
                // The hold must be in place before the page enters the bfcache,
                // or it never delays the restored page's whoami.
                if (!variant.offline) {
                    await page.route('**/api/account/whoami/', async (route) => {
                        await new Promise((resolve) => setTimeout(resolve, 3000));
                        await route.continue().catch(() => undefined);
                    });
                }
                await page.goto(variant.away);
                await expect(page).toHaveURL(new RegExp(`${variant.away.replace(/\./g, '\\.')}$`));

                const other = await context.newPage();
                await other.goto('/chat/');
                await logout(other);
                await login(other, 'e2e_user_b', E2E_PASSWORD);
                if (variant.forgetEpoch) {
                    // No tab announced the switch: only the shared last-account
                    // (rewritten for B by the chat mount) can say it happened.
                    await other.goto('/chat/');
                    await expect.poll(() => other.evaluate(() => window.localStorage.getItem('crank:last-account'))).toMatch(/^d:/);
                    await other.evaluate((value) => {
                        if (value === null) window.localStorage.removeItem('crank:account-epoch');
                        else window.localStorage.setItem('crank:account-epoch', value);
                    }, epochBefore);
                }
                const announcedBefore = await other.evaluate(() => (window as unknown as {__epochEvents: unknown[]}).__epochEvents.length);

                if (variant.offline) {
                    await context.setOffline(true);
                }
                await page.goBack({waitUntil: 'commit'});
                await expect.poll(() => page.evaluate(() => (window as unknown as {__pageshows?: boolean[]}).__pageshows ?? []),
                    {message: 'the page must come from the back/forward cache'}).toContain(true);
                // Well before whoami can answer: the epoch comparison purges
                // synchronously and needs no network.
                await expect(page.getByText(secret)).toHaveCount(0, {timeout: 1500});
                await expect(page.locator(STRIP)).toHaveCount(0, {timeout: 1500});
                if (variant.offline) {
                    await expect(page.locator('#nav-account')).not.toContainText('e2e_user');
                } else {
                    await expect(page.locator('#nav-account')).toContainText('e2e_user_b');
                }
                const record = await page.evaluate(() => window.sessionStorage.getItem('crank:workspace:v1') ?? '');
                expect(record).not.toContain(COMPANY);
                expect(record).not.toContain('e2e_user');
                // The restored document never re-announced: the live tab saw no
                // epoch write since before Back (online: after whoami answered).
                if (!variant.offline) {
                    await expect(page.locator('#nav-account')).toContainText('e2e_user_b', {timeout: 8000});
                    await page.waitForTimeout(500);
                } else {
                    await page.waitForTimeout(1500);
                }
                expect(await other.evaluate(() => (window as unknown as {__epochEvents: unknown[]}).__epochEvents.length)).toBe(announcedBefore);
                await context.close();
            } finally {
                await browser.close();
            }
        });
    }
});
