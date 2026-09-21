// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Issue #465 auth handoffs over real Django pages, real sessions and real
// storage. These are the three journeys the acceptance criteria promise and
// that no browser test previously exercised, which is why the missing
// company CTA and the missing /chat/ logout purge both passed CI:
//
//   AC-7  signed-out company dialog → login → the same dialog, reopened
//   AC-8  signed-out draft → login → the draft restored into the composer
//   AC-9  logout from an authenticated (uncached) page purges private state
import {expect, test} from '@playwright/test';
import {E2E_PASSWORD, E2E_USERNAME, login, requireDjangoTier} from './support';

requireDjangoTier();

/** Complete the real allauth login form and wait for `next` to be honoured. */
async function submitLogin(page: import('@playwright/test').Page): Promise<void> {
    await page.locator('input[name="login"]').fill(E2E_USERNAME);
    await page.locator('input[name="password"]').fill(E2E_PASSWORD);
    await page.getByRole('button', {name: 'Sign In'}).click();
}

/** Every private client-side artefact this feature writes. */
function privateStorageKeys(page: import('@playwright/test').Page): Promise<string[]> {
    return page.evaluate(() =>
        Object.keys(window.localStorage).filter((key) => key.startsWith('crank:jobsearch:')));
}

test.describe('company handoff through sign-in (issue #465 AC-7)', () => {
    test('the dialog CTA carries a signed-out visitor through login and back to the same company', async ({page}) => {
        await page.goto('/');
        const trigger = page.locator('tr[aria-label="View details for E2E Alpha Corp"]');
        await trigger.click();
        await expect(page.getByRole('dialog')).toBeVisible();

        // The initiating control the dialog previously lacked entirely.
        const signInCta = page.getByTestId('company-sign-in-cta');
        await expect(signInCta).toBeVisible();
        await expect(signInCta).toContainText('E2E Alpha Corp');
        await signInCta.click();

        await expect(page).toHaveURL(/\/accounts\/login\/\?next=/);
        await submitLogin(page);

        // Back on the rankings with the same company's dialog reopened.
        await page.waitForURL((url) => url.pathname === '/' && url.searchParams.has('company'));
        await expect(page.getByRole('dialog')).toBeVisible();
        await expect(page.locator('#organization-details-title')).toHaveText('E2E Alpha Corp');

        // The post-sign-in landing state offers the company conversation.
        const chatCta = page.getByTestId('company-chat-cta');
        await expect(chatCta).toBeVisible();
        await chatCta.click();
        await page.waitForURL((url) => url.pathname === '/chat/' && url.searchParams.has('company'));
        await expect(page.getByTestId('job-search-chat')).toBeVisible();
    });
});

test.describe('draft preservation through sign-in (issue #465 AC-8)', () => {
    test('a draft composed while signed out is restored into the composer after login', async ({page}) => {
        await page.goto('/chat/');
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toBeEnabled();

        const draft = 'remote roles with strong equity';
        await composer.fill(draft);
        await expect
            .poll(() => page.evaluate(() => window.localStorage.getItem('crank:jobsearch:draft:pending')))
            .toBe(draft);

        await page.getByTestId('job-search-sign-in-cta').click();
        await expect(page).toHaveURL(/\/accounts\/login\//);
        await submitLogin(page);

        await page.waitForURL((url) => url.pathname === '/chat/');
        await expect(page.locator('textarea[aria-label="Message"]')).toHaveValue(draft);
        // The pending slot is consumed, not left behind for the next account.
        await expect
            .poll(() => page.evaluate(() => window.localStorage.getItem('crank:jobsearch:draft:pending')))
            .toBeNull();
    });
});

test.describe('logout purge on an authenticated page (issue #465 AC-9)', () => {
    test('logging out from /chat/ discards every private artefact before leaving', async ({page}) => {
        // The regression: /chat/ is server-authenticated and uncached, so its
        // logout form posts natively and used to bypass the purge handler,
        // which was bound only to the cached shell's JS-submitted form.
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();

        await page.locator('textarea[aria-label="Message"]').fill('a private draft nobody else may see');
        await expect.poll(() => privateStorageKeys(page)).not.toEqual([]);
        await expect
            .poll(() => page.evaluate(() => window.localStorage.getItem('crank:last-account')))
            .toBe(E2E_USERNAME);

        await page.locator('.app-nav-rail form.app-nav-logout-form button[type="submit"]').click();
        await page.waitForURL((url) => url.pathname !== '/chat/');

        expect(await privateStorageKeys(page)).toEqual([]);
        expect(await page.evaluate(() => window.localStorage.getItem('crank:last-account'))).toBeNull();
        expect(await page.evaluate(() => window.sessionStorage.getItem('crank:auth-intent'))).toBeNull();
        // …and the session really is gone: the anonymous controls are back.
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-sign-in-cta')).toBeVisible();
    });
});
