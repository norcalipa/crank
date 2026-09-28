// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Rankings layout, toolbar, empty state and preset switching over the seeded
// Django server (issue #478).
import {expect, test} from '@playwright/test';
import {requireDjangoTier} from './support';

requireDjangoTier();

async function resultsShare(page: import('@playwright/test').Page): Promise<number> {
    return page.evaluate(() => {
        const main = document.querySelector('#main-content') as HTMLElement;
        const list = document.querySelector('#organization-list') as HTMLElement;
        const cs = getComputedStyle(main);
        const content = main.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
        return list.getBoundingClientRect().width / content;
    });
}

for (const [width, height] of [[1280, 800], [1024, 768], [375, 800]] as const) {
    test(`results span the main content width at ${width}px and search is typeable`, async ({page}) => {
        await page.setViewportSize({width, height});
        await page.goto('/');
        await expect(page.getByTestId('rankings-toolbar')).toBeVisible();
        expect(await resultsShare(page)).toBeGreaterThanOrEqual(0.9);
        const search = page.getByRole('textbox', {name: 'Search organizations'});
        await search.fill('Alpha');
        await expect(search).toHaveValue('Alpha');
        await expect(page.getByText(/Showing 1-1 of 1 organizations/)).toBeVisible();
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow).toBeLessThanOrEqual(1);
    });
}

test('How ranking works is collapsed by default', async ({page}) => {
    await page.goto('/');
    await expect(page.getByTestId('how-ranking-works')).not.toHaveAttribute('open', '');
    await expect(page.getByTestId('ranking-definitions')).toBeHidden();
});

test('zero results show one suggest action and no pager', async ({page}) => {
    await page.goto('/?search=zzz-no-match');
    await expect(page.getByText('No organizations found')).toBeVisible();
    await expect(page.getByRole('button', {name: 'Clear search and filters'})).toBeVisible();
    await expect(page.getByRole('button', {name: 'Suggest a company'})).toHaveCount(0);
    await expect(page.getByRole('navigation', {name: 'Organization pagination'})).toHaveCount(0);
    await expect(page.getByText(/Page \d+ of/)).toHaveCount(0);
});

test('the seeded single page renders no pager', async ({page}) => {
    await page.goto('/');
    await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
    await expect(page.getByRole('navigation', {name: 'Organization pagination'})).toHaveCount(0);
});

test('changing preset keeps search and drops page', async ({page}) => {
    await page.goto('/?search=E2E&page=1');
    await page.getByTestId('ranking-preset-select').selectOption({label: 'E2E Culture Preset'});
    await page.waitForURL(/\/algo\/\d+\/\?search=E2E$/);
    await expect(page.getByTestId('ranking-preset-select').locator('option:checked')).toHaveText('E2E Culture Preset');
    await expect(page.getByRole('columnheader', {name: 'Company score (E2E Culture Preset)'})).toBeVisible();
});

test('the 100-character company name wraps without horizontal overflow', async ({page}) => {
    await page.setViewportSize({width: 375, height: 800});
    await page.goto('/?search=Longname');
    await expect(page.locator('.organization-card-name').first()).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
});
