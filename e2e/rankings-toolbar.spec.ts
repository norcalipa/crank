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

// The count line at phone widths (issue #617): two reserved lines. The count is
// the first; the second holds whichever ONE of "most rated on N of M", the
// loading words or the "Updated" mark is showing (a count of 100 or more drops
// the coverage). The second line is always there, so the labels' arrival moves
// nothing, and because the lines stack nothing can overlap. Measured on text
// extents (Range rects), not box edges: a box can be narrower than its text.
const PRODUCTION_LIKE_FIXTURE = (html: string, total: number) => html.replace(/(<script id="organization-data" type="application\/json">)([\s\S]*?)(<\/script>)/, (_m, open, json, close) => {
    const base = JSON.parse(json) as Array<Record<string, unknown>>;
    const orgs = Array.from({length: total}, (_v, i) => ({
        ...base[i % base.length], id: i + 1, name: `Fixture Company ${String(i + 1).padStart(2, '0')}`, ranking: i + 1,
        rating_dimensions_total: 10, rating_dimensions_covered: 10,
    }));
    return `${open}${JSON.stringify(orgs)}${close}`;
});

// Text spacing as WCAG 1.4.12 asks a page to tolerate, and a larger root size.
// Scoped to the count region so the icon font is left alone.
const TEXT_MODES: Record<string, string> = {
    default: '',
    'root text 125%': 'html { font-size: 125%; }',
    'WCAG 1.4.12 spacing': '.rankings-toolbar-count-text, .rankings-toolbar-count-text * { line-height: 1.5 !important; letter-spacing: .12em !important; word-spacing: .16em !important; }',
};

const measureCountLine = () => {
    const rect = (el: Element | null) => {
        if (!el || getComputedStyle(el).display === 'none') return null;
        // The text extents: a Range over the contents is the ink's boxes, even
        // where the element's own box is narrower than its text.
        const range = document.createRange();
        range.selectNodeContents(el);
        const boxes = Array.from(range.getClientRects()).filter((r) => r.width > 0);
        if (!boxes.length) return null;
        return {
            left: Math.min(...boxes.map((r) => r.left)), right: Math.max(...boxes.map((r) => r.right)),
            top: Math.min(...boxes.map((r) => r.top)), bottom: Math.max(...boxes.map((r) => r.bottom)),
        };
    };
    const count = document.querySelector('.organization-results-count') as HTMLElement;
    // The count's own words only (the coverage and the mark are its children).
    const own = document.createRange();
    own.setStartBefore(count.firstChild!);
    own.setEndAfter(Array.from(count.childNodes).find((n) => n.nodeType === 3)!);
    const ownBoxes = Array.from(own.getClientRects()).filter((r) => r.width > 0);
    const countText = {
        left: Math.min(...ownBoxes.map((r) => r.left)), right: Math.max(...ownBoxes.map((r) => r.right)),
        top: Math.min(...ownBoxes.map((r) => r.top)), bottom: Math.max(...ownBoxes.map((r) => r.bottom)),
    };
    const toolbar = document.querySelector('.rankings-toolbar-count')!.getBoundingClientRect();
    const line = document.querySelector('.rankings-toolbar-count-text')!.getBoundingClientRect();
    const coverage = document.querySelector('.organization-common-coverage');
    const value = coverage?.querySelector('.organization-common-coverage-value') ?? null;
    const items: Record<string, ReturnType<typeof rect>> = {
        coverage: rect(coverage), badge: rect(count.querySelector('.badge')), loading: rect(document.querySelector('.choices-status-text')),
    };
    const overlaps = (a: NonNullable<ReturnType<typeof rect>>, b: NonNullable<ReturnType<typeof rect>>) =>
        Math.min(a.right, b.right) - Math.max(a.left, b.left) > .5 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > .5;
    const shown = Object.entries(items).filter(([, r]) => r) as Array<[string, NonNullable<ReturnType<typeof rect>>]>;
    return {
        height: line.height, toolbarLeft: toolbar.left, toolbarRight: toolbar.right,
        countText, shown: shown.map(([name, r]) => ({name, left: r.left, right: r.right})),
        overlapping: shown.filter(([, r]) => overlaps(countText, r)).map(([name]) => name)
            .concat(shown.flatMap(([n1, r1], i) => shown.slice(i + 1).filter(([, r2]) => overlaps(r1, r2)).map(([n2]) => `${n1}/${n2}`))),
        coverageShown: !!coverage && getComputedStyle(coverage).display !== 'none',
        valueText: coverage && getComputedStyle(coverage).display !== 'none' ? value?.textContent ?? null : null,
        valueLines: value && getComputedStyle(value).display !== 'none' ? value.getClientRects().length : null,
        docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
};

for (const width of [320, 360, 375, 390, 414, 575]) {
    for (const [modeName, modeCss] of Object.entries(TEXT_MODES)) {
        for (const total of [43, 120]) {
            for (const pageNumber of total === 43 ? [1, 2] : [1]) {
                test(`the count line is two reserved lines and never overlaps or leaves the toolbar: ${total} companies, page ${pageNumber}, ${width}px, ${modeName}`, async ({page}) => {
                    await page.setViewportSize({width, height: 800});
                    await page.route('**/e2e/fixtures/organization-list-paged.html', async (route) => {
                        const response = await route.fetch();
                        await route.fulfill({response, body: PRODUCTION_LIKE_FIXTURE(await response.text(), total)});
                    });
                    // Hold the labels, so the loading words are measured and then the loaded line.
                    let release: () => void = () => {};
                    const gate = new Promise<void>((resolve) => { release = resolve; });
                    await page.route('**/api/funding-round-choices/', async (route) => { await gate; await route.fulfill({json: {A: 'Series A', B: 'Series B', C: 'Series C', D: 'Series D'}}); });
                    await page.goto(PAGED_FIXTURE);
                    if (modeCss) await page.addStyleTag({content: modeCss});
                    if (pageNumber === 2) {
                        await page.locator('.rankings-pager-bottom').getByRole('link', {name: 'Next page'}).click();
                    }
                    const range = pageNumber === 1 ? '1–15' : '16–30';
                    await expect(page.locator('.organization-results-count')).toContainText(`Showing ${range} of ${total} companies`);
                    await page.locator('.rankings-toolbar-count').scrollIntoViewIfNeeded();
                    await expect(page.getByTestId('choices-status-loading')).toBeVisible();
                    const loading = await page.evaluate(measureCountLine);
                    release();
                    await expect(page.getByTestId('choices-status-loading')).toHaveCount(0);
                    const loaded = await page.evaluate(measureCountLine);

                    for (const state of [loading, loaded]) {
                        expect(state.overlapping).toEqual([]);
                        expect(state.docOverflow).toBeLessThanOrEqual(1);
                        expect(state.countText.right).toBeLessThanOrEqual(state.toolbarRight + 1);
                        expect(state.countText.left).toBeGreaterThanOrEqual(state.toolbarLeft - 1);
                        for (const item of state.shown) {
                            expect(item.right, `${item.name} right edge`).toBeLessThanOrEqual(state.toolbarRight + 1);
                            expect(item.left, `${item.name} left edge`).toBeGreaterThanOrEqual(state.toolbarLeft - 1);
                        }
                    }
                    // Loading words and the coverage never share the line.
                    expect(loading.coverageShown).toBe(false);
                    expect(loading.shown.map((i) => i.name)).toContain('loading');
                    // "N of M" is whole (one line, not clipped) or dropped (a count of 100 or more).
                    if (total >= 100) {
                        expect(loaded.coverageShown).toBe(false);
                    } else {
                        expect(loaded.coverageShown).toBe(true);
                        expect(loaded.valueText).toBe('10 of 10');
                        expect(loaded.valueLines).toBe(1);
                    }
                    // The count line is the same height before, during and after the labels load.
                    expect(Math.abs(loaded.height - loading.height)).toBeLessThanOrEqual(.5);
                });
            }
        }
    }
}

// What the reserved second line costs: three whole cards still sit above the fold.
for (const [width, height] of [[375, 812], [360, 740]] as const) {
    test(`three cards are fully visible above the fold at ${width}x${height}`, async ({page}) => {
        await page.setViewportSize({width, height});
        await page.goto(PAGED_FIXTURE);
        await expect(page.locator('.organization-results-count')).toContainText('Showing');
        const whole = await page.evaluate((viewport) => Array.from(document.querySelectorAll('.organization-row'))
            .filter((row) => row.getBoundingClientRect().bottom <= viewport).length, height);
        expect(whole).toBeGreaterThanOrEqual(3);
    });
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
