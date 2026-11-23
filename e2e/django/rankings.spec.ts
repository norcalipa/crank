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
        const search = page.getByRole('textbox', {name: 'Search companies'});
        await search.fill('Alpha');
        await expect(search).toHaveValue('Alpha');
        await expect(page.getByText(/Showing 1–1 of 1 companies/)).toBeVisible();
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
    await expect(page.getByText('No companies found')).toBeVisible();
    await expect(page.getByRole('button', {name: 'Clear search and filters'})).toBeVisible();
    await expect(page.getByRole('button', {name: 'Suggest a company'})).toHaveCount(0);
    await expect(page.getByRole('navigation', {name: 'Organization pagination'})).toHaveCount(0);
    await expect(page.getByText(/Page \d+ of/)).toHaveCount(0);
});

test('the seeded single page renders no pager', async ({page}) => {
    await page.goto('/');
    await expect(page.getByText('Showing 1–4 of 4 companies')).toBeVisible();
    await expect(page.getByRole('navigation', {name: 'Organization pagination'})).toHaveCount(0);
});

test('changing preset keeps search and drops page', async ({page}) => {
    await page.goto('/?search=E2E&page=1');
    await page.getByTestId('ranking-preset-select').selectOption({label: 'E2E Culture Preset'});
    await page.waitForURL(/\/algo\/\d+\/\?search=E2E$/);
    await expect(page.getByTestId('ranking-preset-select').locator('option:checked')).toHaveText('E2E Culture Preset');
    const firstRow = page.locator('.organization-row').first();
    await expect(page.locator(`#${await firstRow.getAttribute('aria-describedby')}`)).toContainText('Company score (E2E Culture Preset)');
});

test('the 100-character company name is cut inside its row without horizontal overflow', async ({page}) => {
    await page.setViewportSize({width: 375, height: 800});
    await page.goto('/?search=Longname');
    await expect(page.locator('.organization-name').first()).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
});

// Density budgets (issue #617, docs/design-system.md): a company row is at most
// 96px tall on desktop and 180px on a phone, including the 100-character name.
// Round 2: the desktop target is 58-64px with every row the same height, and a
// phone card is about 118px.
for (const [width, height, budget] of [[1440, 900, 96], [1280, 800, 96], [375, 812, 180]] as const) {
    test(`every company row fits the ${budget}px budget at ${width}px with no horizontal overflow`, async ({page}) => {
        await page.setViewportSize({width, height});
        await page.goto('/');
        const rows = page.locator('ol.organization-list > li > .organization-row');
        await expect(rows).toHaveCount(4);
        const heights = await rows.evaluateAll((els) => els.map((el) => el.getBoundingClientRect().height));
        for (const h of heights) {
            expect(h).toBeLessThanOrEqual(budget);
        }
        if (width >= 1280) {
            for (const h of heights) {
                expect(h).toBeGreaterThanOrEqual(58);
                expect(h).toBeLessThanOrEqual(66);
            }
            expect(Math.max(...heights) - Math.min(...heights)).toBeLessThanOrEqual(1);
        }
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow).toBeLessThanOrEqual(1);
    });
}

// Round 2: the Assistant entry never covers a row action, at rest or scrolled
// to the bottom, and every row names its freshness under the chips.
for (const [width, height] of [[1440, 900], [1280, 800], [375, 812]] as const) {
    test(`the Assistant entry does not overlap any row action at ${width}x${height}, at the top and at the bottom`, async ({page}) => {
        await page.setViewportSize({width, height});
        await page.goto('/');
        await expect(page.getByTestId('assistant-launcher')).toBeVisible();
        const overlap = () => page.evaluate(() => {
            const entry = document.querySelector('[data-testid="assistant-launcher"]')!.getBoundingClientRect();
            const barBottom = document.querySelector('.app-mobile-topbar')?.getBoundingClientRect().bottom ?? 0;
            let worst = 0;
            document.querySelectorAll('.organization-cta').forEach((cta) => {
                const b = cta.getBoundingClientRect();
                // A row scrolled behind the sticky phone top bar is not a resting overlap.
                if (b.top < barBottom && width < 768) return;
                const w = Math.max(0, Math.min(entry.right, b.right) - Math.max(entry.left, b.left));
                const h = Math.max(0, Math.min(entry.bottom, b.bottom) - Math.max(entry.top, b.top));
                worst = Math.max(worst, w * h);
            });
            return worst;
        });
        expect(await overlap()).toBe(0);
        await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
        expect(await overlap()).toBe(0);
        if (width < 768) {
            await expect(page.locator('.app-mobile-topbar [data-testid="assistant-launcher"]')).toHaveCount(1);
            await expect(page.locator('.assistant-dock--floating')).toHaveCount(0);
        } else {
            await expect(page.locator('.rankings-title-row [data-testid="assistant-launcher"]')).toHaveCount(1);
        }
    });
}

test('each row shows when it was checked and the list has a header row on the wide layout', async ({page}) => {
    await page.setViewportSize({width: 1440, height: 900});
    await page.goto('/');
    const rows = page.locator('ol.organization-list > li > .organization-row');
    await expect(rows).toHaveCount(4);
    for (const text of await rows.locator('.organization-checked').allTextContents()) {
        expect(text).toMatch(/^(Checked [A-Z][a-z]{2} \d{1,2}, \d{4}|Never checked)$/);
    }
    const header = page.locator('.organization-list-header');
    await expect(header).toBeVisible();
    await expect(header).toContainText('Company');
    await expect(header).toContainText('Evidence');
    await expect(header).toContainText('Company score (');
    await page.setViewportSize({width: 375, height: 812});
    await expect(header).toBeHidden();
});

test('on a phone the first card clears y=300 and three cards fit the first screen', async ({page}) => {
    await page.setViewportSize({width: 375, height: 812});
    await page.goto('/');
    const rows = page.locator('ol.organization-list > li > .organization-row');
    await expect(rows).toHaveCount(4);
    await expect(page.locator('.rankings-toolbar-pager')).toBeHidden();
    await expect(page.locator('.organization-page-count')).toBeHidden();
    const top = await rows.first().evaluate((el) => el.getBoundingClientRect().top);
    expect(top).toBeLessThanOrEqual(300);
    const fitting = await rows.evaluateAll((els) => els.filter((el) => el.getBoundingClientRect().bottom <= window.innerHeight).length);
    expect(fitting).toBeGreaterThanOrEqual(3);
});

test('signed out, the rail offers exactly one entry, Sign in, which opens the login page', async ({page}) => {
    await page.goto('/');
    const footer = page.locator('.app-nav-rail .app-nav-footer a, .app-nav-rail .app-nav-footer button');
    await expect(footer).toHaveCount(1);
    await expect(footer.first()).toHaveText('Sign in');
    await expect(footer.first()).toHaveAttribute('href', '/accounts/login/');
    await expect(page.locator('.app-nav-link--google')).toHaveCount(0);
});
