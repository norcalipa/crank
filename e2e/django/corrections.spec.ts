// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Contextual company correction form (issue #477), Django tier.
import {expect, test} from '@playwright/test';
import {expectNoHorizontalOverflow, login, requireDjangoTier} from './support';

requireDjangoTier();

const COMPANY = 'E2E Alpha Corp';

async function openDetails(page: import('@playwright/test').Page) {
    await page.goto('/');
    await page.locator(`[aria-label="View details for ${COMPANY}"] >> visible=true`).first().click();
    const details = page.getByRole('dialog').filter({has: page.locator('#organization-details-title')});
    await expect(details).toBeVisible();
    return details;
}

const VIEWPORTS = [
    {width: 1280, height: 800, field: 'rto_policy', current: /Remote-first[\s\S]*last verified/, saved: /Remote-first/},
    {width: 375, height: 700, field: 'public_status', current: /No verified value on record/, saved: /No verified value on record/},
];

for (const {field, current, saved, ...viewport} of VIEWPORTS) {
    test.describe(`correction form at ${viewport.width}px`, () => {
        test.use({viewport});

        test('a per-field button opens the form with the verified current value, and a draft survives a server validation error', async ({page}) => {
            await login(page);
            const details = await openDetails(page);

            const opener = details.getByTestId(`suggest-correction-field-${field}`);
            await opener.scrollIntoViewIfNeeded();
            await opener.click();

            const form = page.getByRole('dialog', {name: new RegExp(`Suggest a correction — ${COMPANY}`)});
            await expect(form).toBeVisible();
            await expect(details).toBeHidden();
            await expect(form.getByTestId('correction-field')).toHaveValue(field);
            await expect(form.getByTestId('correction-current-value')).toHaveText(current);

            await form.getByRole('textbox', {name: 'Suggested value'}).fill('Hybrid, 3 days');
            await form.getByRole('textbox', {name: 'Evidence link (https)'}).fill('http://example.com/policy');
            await form.getByRole('button', {name: 'Submit suggestion'}).click();

            await expect(form.getByRole('alert')).toBeVisible();
            await expect(form.getByRole('textbox', {name: 'Evidence link (https)'})).toHaveAttribute('aria-invalid', 'true');
            await expect(form.getByRole('textbox', {name: 'Suggested value'})).toHaveValue('Hybrid, 3 days');
            await expectNoHorizontalOverflow(page);

            await form.getByRole('textbox', {name: 'Evidence link (https)'}).fill('https://example.com/policy');
            await form.getByRole('button', {name: 'Submit suggestion'}).click();

            const status = form.getByRole('status');
            await expect(status).toContainText('Pending review');
            await expect(status).toContainText('Hybrid, 3 days');
            await expect(status).toContainText('Still in effect until review');
            await expect(status.getByTestId('correction-saved-current')).toHaveText(saved);

            await form.getByRole('button', {name: `Back to ${COMPANY}`}).click();
            const reopened = page.getByRole('dialog').filter({has: page.locator('#organization-details-title')});
            await expect(reopened).toBeVisible();
            const pending = reopened.getByTestId('your-pending-corrections');
            await expect(pending).toContainText('Pending review');
            await expect(pending).toContainText('Hybrid, 3 days');
        });

        test('Escape closes the form and returns focus to the company entry', async ({page}) => {
            await login(page);
            const details = await openDetails(page);
            await details.getByTestId('suggest-correction-link').click();
            const form = page.getByRole('dialog', {name: /Suggest a correction/});
            await expect(form).toBeVisible();
            await page.keyboard.press('Escape');
            await expect(form).toBeHidden();
            await expect.poll(() => page.evaluate(
                () => document.activeElement?.closest('[data-organization-id]')?.getAttribute('data-organization-id') ?? null,
            )).not.toBeNull();
        });
    });
}

test('a double click on submit creates one pending correction', async ({page}) => {
    await login(page);
    const details = await openDetails(page);
    await details.getByTestId('suggest-correction-field-funding_round').click();
    const form = page.getByRole('dialog', {name: /Suggest a correction/});
    await form.getByRole('textbox', {name: 'Suggested value'}).fill('Series C');
    await form.getByRole('textbox', {name: 'Evidence link (https)'}).fill('https://example.com/funding');
    await form.getByRole('button', {name: 'Submit suggestion'}).dblclick();
    await expect(form.getByRole('status')).toContainText('Pending review');

    const listed = await page.evaluate(async () => {
        const response = await fetch('/api/company-corrections/');
        return (await response.json()).corrections.filter((c: {field_key: string}) => c.field_key === 'funding_round');
    });
    expect(listed).toHaveLength(1);
});

test('the job-results entry opens the form and returns to results', async ({page}) => {
    await login(page);
    await page.goto('/chat/');
    const button = page.getByRole('button', {name: /Suggest a correction for/}).first();
    test.skip(await button.count() === 0, 'no seeded ranked organizations rendered for this user');
    await button.click();
    const form = page.getByRole('dialog', {name: /Suggest a correction/});
    await expect(form).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(form).toBeHidden();
});
