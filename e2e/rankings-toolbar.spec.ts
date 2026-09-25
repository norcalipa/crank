// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Rankings toolbar, explanation disclosure, and pager fixture coverage (issue #478).
import {test, expect} from '@playwright/test';

const ORG_FIXTURE = '/e2e/fixtures/organization-list.html';
const PAGED_FIXTURE = '/e2e/fixtures/organization-list-paged.html';

test.beforeEach(async ({page}) => {
    await page.route('**/api/funding-round-choices/', (route) => route.fulfill({json: {A: 'Series A', B: 'Series B', C: 'Series C', D: 'Series D'}}));
    await page.route('**/api/rto-policy-choices/', (route) => route.fulfill({json: {H: 'Hybrid', R: 'Remote', O: 'Onsite'}}));
});

for (const width of [320, 768, 1280]) {
    test(`toolbar wraps without horizontal overflow at ${width}px`, async ({page}) => {
        await page.setViewportSize({width, height: 800});
        await page.goto(`${ORG_FIXTURE}?search=${'x'.repeat(80)}&accelerated_vesting=1`);
        const toolbar = page.getByTestId('rankings-toolbar');
        await expect(toolbar).toBeVisible();
        await expect(toolbar.getByTestId('ranking-preset-select')).toBeVisible();
        await expect(toolbar.getByTestId('filter-chip-search')).toBeVisible();
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow).toBeLessThanOrEqual(1);
    });
}

test('How ranking works is collapsed by default and keyboard operable', async ({page}) => {
    await page.goto(ORG_FIXTURE);
    const disclosure = page.getByTestId('how-ranking-works');
    await expect(disclosure).not.toHaveAttribute('open', '');
    await expect(page.getByTestId('ranking-definitions')).toBeHidden();
    await disclosure.locator('summary').focus();
    await page.keyboard.press('Enter');
    await expect(page.getByTestId('ranking-definitions')).toBeVisible();
    await expect(page.getByTestId('ranking-definitions')).toContainText('Personal fit');
});

test('single page renders no pager', async ({page}) => {
    await page.goto(ORG_FIXTURE);
    await expect(page.getByText('Showing 1-6 of 6 organizations')).toBeVisible();
    await expect(page.getByRole('navigation', {name: 'Organization pagination'})).toHaveCount(0);
    await expect(page.getByText(/Page \d+ of/)).toHaveCount(0);
});

test('pager works with the keyboard', async ({page}) => {
    await page.goto(PAGED_FIXTURE);
    await expect(page.getByText('Page 1 of 2')).toBeVisible();
    await page.getByRole('link', {name: 'Next page'}).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByText('Page 2 of 2')).toBeVisible();
    await expect(page.getByRole('link', {name: 'Page 2'})).toHaveAttribute('aria-current', 'page');
    await page.getByRole('link', {name: 'Previous page'}).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByText('Page 1 of 2')).toBeVisible();
});

test('removing a filter chip updates count and URL', async ({page}) => {
    await page.goto(`${PAGED_FIXTURE}?accelerated_vesting=1`);
    await expect(page.getByText('Showing 1-8 of 8 organizations')).toBeVisible();
    await page.getByRole('button', {name: 'Remove filter: first vesting in under 1 year'}).click();
    await expect(page.getByText('Showing 1-15 of 16 organizations')).toBeVisible();
    expect(page.url()).not.toContain('accelerated_vesting');
});
