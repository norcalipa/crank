// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Validated page context and explicit assistant actions (issue #484). The
// seeded demo provider answers "only remote" with a propose_filters action and
// "open it" with open_company, and only when the page context was sent.
import {expect, Page, test} from '@playwright/test';
import {E2E_PASSWORD, expectNoHorizontalOverflow, login, requireDjangoTier} from './support';

const PREFS_USER = 'e2e_prefs_user';

async function openAssistant(page: Page): Promise<void> {
    const launcher = page.getByTestId('assistant-launcher');
    if (await launcher.count() > 0) {
        await launcher.click();
    }
    await expect(page.getByTestId('assistant-panel')).toBeVisible();
    await expect(page.getByTestId('assistant-composer')).toBeEnabled();
}

async function ask(page: Page, text: string): Promise<void> {
    await page.getByTestId('assistant-composer').fill(text);
    await page.getByRole('button', {name: 'Send message'}).click();
}

const applyRemote = (page: Page) => page.getByRole('button', {name: 'Apply remote filter'});

test.describe('assistant actions (issue #484)', () => {
    test.beforeEach(async ({page}) => {
        requireDjangoTier();
        await page.setViewportSize({width: 1280, height: 900});
    });

    test('a filter suggestion is a button; applying it filters, survives reload and Back restores', async ({page}) => {
        await login(page);
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        await openAssistant(page);
        await ask(page, 'Show only remote companies');

        const button = applyRemote(page);
        await expect(button).toBeVisible();
        // Nothing changes until the button is pressed.
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        expect(new URL(page.url()).searchParams.has('rto')).toBe(false);

        await button.focus();
        await button.press('Enter');
        await expect(page.getByText(/Showing 1-1 of 1 organizations/)).toBeVisible();
        await expect(page.getByTestId('filter-chip-rto')).toContainText('Office policy: Remote');
        expect(new URL(page.url()).searchParams.get('rto')).toBe('R');
        // Keyboard focus stays on the button, which reports the outcome.
        const applied = page.getByRole('button', {name: 'Remote filter applied'});
        await expect(applied).toBeFocused();
        await expect(applied).toBeDisabled();

        await page.reload();
        await expect(page.getByText(/Showing 1-1 of 1 organizations/)).toBeVisible();
        await expect(page.getByTestId('filter-chip-rto')).toBeVisible();

        await page.goBack();
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        await expect(page.getByTestId('filter-chip-rto')).toHaveCount(0);
    });

    test('removing the RTO chip clears the filter and the URL', async ({page}) => {
        await login(page);
        await page.goto('/?rto=R');
        await expect(page.getByText(/Showing 1-1 of 1 organizations/)).toBeVisible();
        await page.getByRole('button', {name: 'Remove filter: office policy Remote'}).click();
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        expect(new URL(page.url()).searchParams.has('rto')).toBe(false);
    });

    test('an unknown rto value in the URL is ignored', async ({page}) => {
        await login(page);
        await page.goto('/?rto=ZZZ');
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        await expect(page.getByTestId('filter-chip-rto')).toHaveCount(0);
    });

    test('a reply that lands after the user moved on is disabled with a reason', async ({page}) => {
        await login(page);
        await openAssistant(page);
        // Hold the reply until the page has changed.
        let release: () => void = () => undefined;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        let held: () => void = () => undefined;
        const reachedServer = new Promise<void>((resolve) => { held = resolve; });
        await page.route('**/api/agent/conversations/*/', async (route) => {
            if (route.request().method() === 'POST') {
                held();
                await gate;
            }
            await route.continue();
        });
        await ask(page, 'Show only remote companies');
        // The turn is sent under the context at this moment; change it after.
        await reachedServer;
        await page.getByTestId('accelerated-vesting-checkbox').check();
        await expect.poll(() => new URL(page.url()).searchParams.get('accelerated_vesting')).toBe('1');
        release();
        const button = applyRemote(page);
        await expect(button).toBeDisabled();
        await expect(page.getByTestId('assistant-actions-stale')).toHaveText('This suggestion was for an earlier view.');
        await button.click({force: true});
        expect(new URL(page.url()).searchParams.has('rto')).toBe(false);
    });

    test('Back after applying leaves an older reply disabled', async ({page}) => {
        await login(page);
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await applyRemote(page).click();
        await expect(page.getByTestId('filter-chip-rto')).toBeVisible();
        await page.goBack();
        await expect(page.getByTestId('filter-chip-rto')).toHaveCount(0);
        await expect(page.getByRole('button', {name: 'Remote filter applied'})).toBeDisabled();
    });

    test('an open_company suggestion reopens the company dialog from the assistant', async ({page}) => {
        await login(page);
        await page.getByRole('button', {name: /View details for E2E Alpha/}).first().click();
        await page.getByRole('link', {name: /Ask the assistant about E2E Alpha/}).click();
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        await expect(page.getByRole('dialog')).toHaveCount(0);
        await ask(page, 'Open this company');
        const open = page.getByRole('button', {name: /^Open E2E Alpha/});
        await expect(open).toBeVisible();
        await open.click();
        await expect(page.getByRole('dialog')).toBeVisible();
    });

    test('signing in as another account leaves no actions behind', async ({page}) => {
        await login(page);
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await expect(applyRemote(page)).toBeVisible();
        await page.goto('/accounts/logout/');
        const confirm = page.getByRole('button', {name: /Sign Out/});
        if (await confirm.count() > 0) {
            await confirm.click();
        }
        await login(page, PREFS_USER, E2E_PASSWORD);
        await openAssistant(page);
        await expect(page.getByRole('button', {name: /remote filter/})).toHaveCount(0);
    });

    test('save as a lasting requirement says so when it is already saved; nothing is written', async ({page}) => {
        await login(page, PREFS_USER, E2E_PASSWORD);
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await applyRemote(page).click();
        await page.getByRole('button', {name: 'Save as a requirement'}).click();
        // The seeded account already prefers remote work, so there is nothing
        // to review: the note says so and no write happens.
        await expect(page.getByTestId('assistant-action-already')).toContainText('Already one of your requirements');
        await expect(page.getByTestId('assistant-action-review')).toHaveCount(0);
    });

    test('actions and the RTO chip fit at 375px', async ({page}) => {
        await page.setViewportSize({width: 375, height: 800});
        await login(page);
        await page.goto('/?rto=R');
        await expect(page.getByTestId('filter-chip-rto')).toBeVisible();
        await expectNoHorizontalOverflow(page);
        await page.goto('/');
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await expect(applyRemote(page)).toBeVisible();
        await expectNoHorizontalOverflow(page);
        const box = await applyRemote(page).boundingBox();
        expect(box?.height ?? 0).toBeGreaterThanOrEqual(36);
    });

    test('the drawer at 1024px shows the action and applies it', async ({page}) => {
        await page.setViewportSize({width: 1024, height: 800});
        await login(page);
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await expect(applyRemote(page)).toBeVisible();
        await expectNoHorizontalOverflow(page);
    });
});
