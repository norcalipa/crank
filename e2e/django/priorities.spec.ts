// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Editable priority chips with a review / apply / undo flow (issue #480).
// Uses the dedicated seeded account `e2e_prefs_user` because these journeys
// mutate the saved preference document.
import {expect, Page, test} from '@playwright/test';
import {E2E_PASSWORD, expectNoHorizontalOverflow, login, requireDjangoTier} from './support';

const PREFS_USER = 'e2e_prefs_user';
const SALARY = /Minimum base salary/;

async function openEditor(page: Page): Promise<void> {
    await page.goto('/chat/');
    // Desktop shows the main variant; narrow viewports show the sidebar variant.
    const desktop = (page.viewportSize()?.width ?? 1280) >= 768;
    const section = page.getByTestId(desktop ? 'priorities-main' : 'priorities-sidebar');
    await expect(section).toBeVisible();
    await section.getByRole('button', {name: /Edit priorities|Add priorities/}).click();
    await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
}

async function applySalary(page: Page, value: string): Promise<void> {
    await openEditor(page);
    await page.getByRole('spinbutton', {name: SALARY}).fill(value);
    await page.getByRole('button', {name: 'Review changes'}).click();
    await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
    await page.getByRole('button', {name: 'Apply to account'}).click();
    await expect(page.getByRole('list', {name: 'Changed priorities'})).toBeVisible();
}

test.describe('priorities editor (issue #480)', () => {
    test.beforeEach(async ({page}) => {
        requireDjangoTier();
        await page.setViewportSize({width: 1280, height: 900});
        await login(page, PREFS_USER, E2E_PASSWORD);
        // Start every journey from the defaults.
        await page.goto('/chat/');
        const reset = page.getByTestId('priorities-main').getByRole('button', {name: 'Reset priorities'});
        if (await reset.isVisible().catch(() => false)) {
            await reset.click();
            await page.getByTestId('priorities-main').getByRole('button', {name: 'Reset priorities'}).click();
            await expect(page.getByTestId('priorities-main').getByRole('group', {name: 'Confirm reset'})).toHaveCount(0);
        }
    });

    test('edit, review, apply and undo from the editor', async ({page}) => {
        await applySalary(page, '150000');
        await page.getByRole('button', {name: 'Undo'}).click();
        await expect(page.getByText('Change undone.')).toBeVisible();
        await page.getByRole('button', {name: 'Done'}).click();
        await expect(page.getByTestId('priorities-main').getByText(/150,?000/)).toHaveCount(0);
    });

    test('an invalid value keeps the edits and shows an inline error', async ({page}) => {
        await openEditor(page);
        const input = page.getByRole('spinbutton', {name: SALARY});
        await input.fill('-5');
        await page.getByRole('button', {name: 'Review changes'}).click();
        await expect(page.getByRole('alert').first()).toBeVisible();
        await expect(input).toHaveValue('-5');
    });

    test('a stale apply is refused and asks for a fresh review', async ({page, context}) => {
        await openEditor(page);
        await page.getByRole('spinbutton', {name: SALARY}).fill('120000');
        await page.getByRole('button', {name: 'Review changes'}).click();
        await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();

        const other = await context.newPage();
        await applySalary(other, '130000');
        await other.close();

        await page.getByRole('button', {name: 'Apply to account'}).click();
        await expect(page.getByRole('alert').filter({hasText: 'Your priorities changed elsewhere'})).toBeVisible();
        await expect(page.getByRole('button', {name: 'Review latest'})).toBeVisible();
    });

    test('resetting priorities is separate from starting a new conversation', async ({page}) => {
        await applySalary(page, '140000');
        await page.getByRole('button', {name: 'Done'}).click();
        await page.getByTestId('priorities-main').getByRole('button', {name: 'Reset priorities'}).click();
        await expect(page.getByText(/Your conversations are not changed/)).toBeVisible();
        await page.getByRole('button', {name: 'Keep priorities'}).click();
        await expect(page.getByRole('button', {name: 'New conversation'}).first()).toBeVisible();
        await expect(page.getByRole('button', {name: 'Reset chat'})).toHaveCount(0);
    });

    test('the last entry of a list and the only work arrangement can be cleared and saved', async ({page}) => {
        await openEditor(page);
        const places = page.getByRole('textbox', {name: /Excluded locations/});
        await places.fill('San Francisco, CA');
        await places.press('Enter');
        await page.getByRole('checkbox', {name: 'Remote'}).check();
        await page.getByRole('button', {name: 'Review changes'}).click();
        await page.getByRole('button', {name: 'Apply to account'}).click();
        await expect(page.getByRole('list', {name: 'Changed priorities'})).toBeVisible();
        await page.getByRole('button', {name: 'Done'}).click();

        await openEditor(page);
        await page.getByRole('button', {name: 'Remove San Francisco, CA from Excluded locations'}).click();
        await page.getByRole('checkbox', {name: 'Remote'}).uncheck();
        await page.getByRole('button', {name: 'Review changes'}).click();
        await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
        await expect(page.getByText(/must list items/)).toHaveCount(0);
        await page.getByRole('button', {name: 'Apply to account'}).click();
        await expect(page.getByRole('list', {name: 'Changed priorities'})).toBeVisible();
        await page.getByRole('button', {name: 'Done'}).click();
        await expect(page.getByTestId('priorities-main').getByText(/San Francisco/)).toHaveCount(0);
    });

    test('Review latest rebases the review onto the changed priorities', async ({page, context}) => {
        await openEditor(page);
        await page.getByRole('spinbutton', {name: SALARY}).fill('120000');
        await page.getByRole('button', {name: 'Review changes'}).click();
        await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();

        const other = await context.newPage();
        await applySalary(other, '130000');
        await other.close();

        await page.getByRole('button', {name: 'Apply to account'}).click();
        await page.getByRole('button', {name: 'Review latest'}).click();
        const review = page.getByRole('list', {name: 'Proposed changes'});
        await expect(review).toContainText('130,000');
        await expect(review).toContainText('120,000');
        await expect(page.getByRole('button', {name: 'Apply to account'})).toBeVisible();
    });

    test('chips stay within the viewport at 375px', async ({page}) => {
        await page.setViewportSize({width: 375, height: 800});
        await applySalary(page, '150000');
        await expectNoHorizontalOverflow(page);
    });
});

test.describe('priorities signed out (issue #480)', () => {
    test.beforeEach(() => {
        requireDjangoTier();
    });

    test('the chat page offers one sign in instead of the editor', async ({page}) => {
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-sign-in-cta')).toBeVisible();
        await expect(page.getByTestId('priorities-main')).toContainText('Sign in to save your priorities.');
        await expect(page.getByTestId('priorities-sidebar')).toHaveCount(0);
        await expect(page.getByRole('button', {name: /Edit priorities/})).toHaveCount(0);
    });
});

test.describe('priorities during a provider outage @outage', () => {
    test.skip(
        process.env.CRANK_E2E_PROVIDER_FAILURE !== '1',
        'provider-outage pass only: run with CRANK_E2E_PROVIDER_FAILURE=1',
    );

    test('the editor applies and undoes without the assistant provider', async ({page}) => {
        requireDjangoTier();
        await login(page, PREFS_USER, E2E_PASSWORD);
        await applySalary(page, '111000');
        await page.getByRole('button', {name: 'Undo'}).click();
        await expect(page.getByText('Change undone.')).toBeVisible();
    });
});
