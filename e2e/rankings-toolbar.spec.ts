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
    await expect(page.getByTestId('ranking-definitions')).toContainText('Requirement coverage');
});

test('single page renders no pager', async ({page}) => {
    await page.goto(ORG_FIXTURE);
    await expect(page.getByText('Showing 1–6 of 6 companies')).toBeVisible();
    await expect(page.getByRole('navigation', {name: 'Organization pagination'})).toHaveCount(0);
    await expect(page.getByText(/Page \d+ of/)).toHaveCount(0);
});

test('on a phone a paged list hides the top pager and keeps the bottom one', async ({page}) => {
    await page.setViewportSize({width: 375, height: 812});
    await page.goto(PAGED_FIXTURE);
    // Rendered, so its hiding is the phone rule's doing and not an absent pager.
    await expect(page.locator('.rankings-toolbar-pager')).toBeAttached();
    await expect(page.locator('.rankings-toolbar-pager')).toBeHidden();
    await expect(page.locator('.rankings-pager-bottom')).toBeVisible();
    await page.setViewportSize({width: 1280, height: 800});
    await expect(page.locator('.rankings-toolbar-pager')).toBeVisible();
});

// The count line stays inside the toolbar at phone widths (issue #617): with
// production-like data (43 companies, every one rated on "10 of 10") "most rated
// on" gives way, and "N of M" is whole or the coverage is gone. A wider font
// (letter-spacing stands in) must not push it out either.
const PRODUCTION_LIKE_FIXTURE = (html: string) => html.replace(/(<script id="organization-data" type="application\/json">)([\s\S]*?)(<\/script>)/, (_m, open, json, close) => {
    const base = JSON.parse(json) as Array<Record<string, unknown>>;
    const orgs = Array.from({length: 43}, (_v, i) => ({
        ...base[i % base.length], id: i + 1, name: `Fixture Company ${String(i + 1).padStart(2, '0')}`, ranking: i + 1,
        rating_dimensions_total: 10, rating_dimensions_covered: 10,
    }));
    return `${open}${JSON.stringify(orgs)}${close}`;
});

for (const width of [320, 360, 375, 575]) {
    for (const letterSpacing of ['normal', '.7px']) {
        for (const pageNumber of [1, 2]) {
            test(`the count line stays inside the toolbar with "10 of 10" at ${width}px, page ${pageNumber}, letter-spacing ${letterSpacing}`, async ({page}) => {
                await page.setViewportSize({width, height: 800});
                await page.route('**/e2e/fixtures/organization-list-paged.html', async (route) => {
                    const response = await route.fetch();
                    await route.fulfill({response, body: PRODUCTION_LIKE_FIXTURE(await response.text())});
                });
                await page.goto(PAGED_FIXTURE);
                await page.addStyleTag({content: `.rankings-toolbar-count-text { letter-spacing: ${letterSpacing}; }`});
                if (pageNumber === 2) {
                    await page.locator('.rankings-pager-bottom').getByRole('link', {name: 'Next page'}).click();
                }
                const range = pageNumber === 1 ? '1–15' : '16–30';
                await expect(page.locator('.organization-results-count')).toContainText(`Showing ${range} of 43 companies`);
                const edges = await page.evaluate(() => {
                    const count = document.querySelector('.organization-results-count')!.getBoundingClientRect();
                    const toolbar = document.querySelector('.rankings-toolbar-count')!.getBoundingClientRect();
                    const coverage = document.querySelector('.organization-common-coverage') as HTMLElement | null;
                    const value = coverage?.querySelector('.organization-common-coverage-value') as HTMLElement | null;
                    const coverageShown = !!coverage && getComputedStyle(coverage).display !== 'none';
                    return {
                        countRight: count.right, toolbarRight: toolbar.right,
                        coverageShown, valueText: coverageShown ? value?.textContent : null,
                        valueRight: coverageShown && value ? value.getBoundingClientRect().right : null,
                        valueFits: coverageShown && value ? value.scrollWidth <= value.clientWidth + 1 : null,
                        docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                    };
                });
                expect(edges.countRight).toBeLessThanOrEqual(edges.toolbarRight + 1);
                expect(edges.docOverflow).toBeLessThanOrEqual(1);
                if (edges.coverageShown) {
                    expect(edges.valueText).toBe('10 of 10');
                    expect(edges.valueRight!).toBeLessThanOrEqual(edges.toolbarRight + 1);
                    expect(edges.valueFits).toBe(true);
                }
            });
        }
    }
}

// jsdom cannot see the media-query CSS that swaps the two spellings, so the
// name a screen reader gets is read from the browser's accessibility tree.
for (const [width, name] of [[375, 'Vests < 1 yr'], [1440, 'First vesting < 1 year']] as const) {
    test(`the vesting checkbox is named "${name}" at ${width}px`, async ({page}) => {
        await page.setViewportSize({width, height: 800});
        await page.goto(ORG_FIXTURE);
        await expect(page.getByRole('checkbox', {name, exact: true})).toBeVisible();
        await expect(page.getByRole('checkbox', {name: /vest/i})).toHaveCount(1);
    });
}

test('the labels status region is in the accessibility tree before a failure and announces it', async ({page}) => {
    // Two requests that fail: the region exists, empty, while they are out, so the
    // error that fills it is a change to a live region and not a new region.
    let fail: () => void = () => {};
    const gate = new Promise<void>((resolve) => { fail = resolve; });
    await page.unroute('**/api/funding-round-choices/');
    await page.unroute('**/api/rto-policy-choices/');
    await page.route('**/api/funding-round-choices/', async (route) => { await gate; await route.fulfill({status: 500, body: 'no'}); });
    await page.route('**/api/rto-policy-choices/', async (route) => { await gate; await route.fulfill({status: 500, body: 'no'}); });
    await page.goto(ORG_FIXTURE);
    const slot = page.locator('.choices-status-slot');
    await expect(page.getByTestId('choices-status-loading')).toBeVisible();
    await expect(slot).toBeEmpty();
    await expect(page.getByRole('status').and(slot)).toHaveCount(1);
    fail();
    await expect(page.getByTestId('choices-status-error')).toBeVisible();
    await expect(page.getByRole('status').and(slot).getByTestId('choices-status-error')).toBeVisible();
    // Retry goes back through the same empty, still-exposed region.
    await page.unroute('**/api/funding-round-choices/');
    await page.unroute('**/api/rto-policy-choices/');
    await page.route('**/api/funding-round-choices/', (route) => route.fulfill({json: {A: 'Series A'}}));
    await page.route('**/api/rto-policy-choices/', (route) => route.fulfill({json: {H: 'Hybrid'}}));
    await page.getByTestId('choices-retry').click();
    await expect(page.getByTestId('choices-status-error')).toHaveCount(0);
    await expect(page.getByRole('status').and(slot)).toHaveCount(1);
});

test('pager works with the keyboard', async ({page}) => {
    await page.goto(PAGED_FIXTURE);
    await expect(page.getByRole('link', {name: 'Page 1'}).first()).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('navigation', {name: /Organization pagination/})).toHaveCount(2);
    // The pager names the page, so "Page N of M" is not said twice.
    await expect(page.getByText(/Page \d+ of/)).toHaveCount(0);
    await page.getByRole('link', {name: 'Next page'}).first().focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('link', {name: 'Page 2'}).first()).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('link', {name: 'Page 2'}).first()).toHaveAttribute('aria-current', 'page');
    await page.getByRole('link', {name: 'Previous page'}).first().focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('link', {name: 'Page 1'}).first()).toHaveAttribute('aria-current', 'page');
});

test('removing a filter chip updates count and URL', async ({page}) => {
    await page.goto(`${PAGED_FIXTURE}?accelerated_vesting=1`);
    await expect(page.getByText('Showing 1–8 of 8 companies')).toBeVisible();
    await page.getByRole('button', {name: 'Remove filter: first vesting in under 1 year'}).click();
    await expect(page.getByText('Showing 1–15 of 16 companies')).toBeVisible();
    expect(page.url()).not.toContain('accelerated_vesting');
});
