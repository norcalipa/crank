// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// A failed ranked read is an error with Retry, never "No matches" (issue #609).
import {test, expect} from '@playwright/test';

const FIXTURE = '/e2e/fixtures/job-match-panel.html?state=ranked_unavailable';

for (const [name, width, height] of [['desktop', 1280, 800], ['mobile', 375, 800]] as const) {
    test(`a failed ranked read shows the error card and Try again at ${name} width`, async ({page}) => {
        await page.setViewportSize({width, height});
        await page.goto(FIXTURE);
        const alert = page.getByTestId('job-match-error');
        await expect(alert).toBeVisible();
        await expect(alert).toHaveAttribute('role', 'alert');
        await expect(alert).toContainText('Couldn’t load your matches');
        await expect(alert).toContainText('Your saved requirements are unchanged.');
        // Try again sits below the alert, not inside it.
        const retry = page.getByRole('button', {name: 'Retry loading matches'});
        await expect(retry).toBeVisible();
        await expect(alert.getByRole('button')).toHaveCount(0);
        await expect(page.getByTestId('job-match-refresh')).toBeEnabled();
        await expect(page.getByText('No matches for your current requirements')).toHaveCount(0);
    });
}

// Visual review round 2: the Try again target, one spinner style, alert copy.
test('Try again is a padded 44px target that fills the width on mobile only', async ({page, browserName}) => {
    await page.setViewportSize({width: 1280, height: 800});
    await page.goto(FIXTURE);
    const retry = page.getByRole('button', {name: 'Retry loading matches'});
    await expect(retry).toBeVisible();
    const style = await retry.evaluate((el) => {
        const cs = getComputedStyle(el);
        const icon = getComputedStyle(el.querySelector('svg') as SVGElement);
        return {display: cs.display, gap: cs.columnGap, pad: cs.paddingTop + ' ' + cs.paddingLeft,
            height: el.getBoundingClientRect().height, iconW: icon.width, iconMargin: icon.marginRight};
    });
    expect(style.display).toBe('inline-flex');
    expect(style.gap).toBe('8px');
    expect(style.pad).toBe('8px 16px');
    expect(style.height).toBeGreaterThanOrEqual(44);
    expect(style.iconW).toBe('16px');
    expect(style.iconMargin).toBe('0px');
    const desktopWidth = (await retry.boundingBox())!.width;
    expect(desktopWidth).toBeLessThan(300);
    await page.setViewportSize({width: 375, height: 800});
    const body = page.getByTestId('job-match-error');
    const mobile = await retry.evaluate((el) => getComputedStyle(el).display);
    expect(mobile).toBe('flex');
    // Same width as the alert above it (the empty state's full-width button spans its card the same way).
    const [rb, ab] = [await retry.boundingBox(), await body.boundingBox()];
    expect(Math.abs(rb!.width - ab!.width)).toBeLessThanOrEqual(1);
    // Firefox does not implement text-wrap: pretty and reports its default.
    await expect(body.locator('.job-match-error-body')).toHaveCSS('text-wrap', browserName === 'firefox' ? 'wrap' : /pretty/);
    await expect(body).not.toContainText('Something went wrong');
});

test('the loading header button uses the inline spinner size and colour, stays disabled, and the header is 61px', async ({page}) => {
    await page.goto('/e2e/fixtures/job-match-panel.html?state=loading');
    const refresh = page.getByTestId('job-match-refresh');
    await expect(refresh).toBeDisabled();
    await expect(refresh).toHaveAttribute('aria-busy', 'true');
    await expect(refresh).toHaveCSS('opacity', '1');
    await expect(refresh).toHaveCSS('cursor', 'default');
    const colour = await refresh.locator('svg').evaluate((el) => getComputedStyle(el).color);
    const inline = await page.locator('.job-match-loading-spinner').evaluate((el) => getComputedStyle(el).color);
    expect(colour).toBe(inline);
    // Computed size (a spinning svg's bounding box is its rotated extent).
    await expect(refresh.locator('svg')).toHaveCSS('width', '16px');
    await expect(refresh.locator('svg')).toHaveCSS('height', '16px');
    const header = await page.locator('.card-header').first().boundingBox();
    expect(Math.round(header!.height)).toBe(61);
});
