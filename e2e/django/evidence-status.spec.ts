// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Coverage, freshness and evidence status in the rankings and the details
// dialog over the seeded Django server (issue #473a).
import {expect, Page, test} from '@playwright/test';
import {expectNoHorizontalOverflow, requireDjangoTier} from './support';

requireDjangoTier();

async function openDialog(page: Page, name: string) {
    await page.goto('/');
    await page.getByRole('button', {name: `View details for ${name}`}).locator('visible=true').first().click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    return dialog;
}

function results(page: Page) {
    return page.locator('#organization-list');
}

for (const [width, height] of [[1280, 800], [375, 800]] as const) {
    test.describe(`at ${width}px`, () => {
        test.beforeEach(async ({page}) => {
            await page.setViewportSize({width, height});
        });

        test('rankings show coverage, facts, last verified and pending review without overflow', async ({page}) => {
            await page.goto('/');
            const list = results(page);
            const beta = list.getByRole('button', {name: 'View details for E2E Beta Labs'}).locator('visible=true').first();
            await expect(beta).toContainText('2 of 2');
            await expect(beta).toContainText('Stale');
            await expect(beta).toContainText('0 verified · 2 stale · 5 unknown');
            await expect(beta).toContainText('Pending review');
            const gamma = list.getByRole('button', {name: 'View details for E2E Gamma Works'}).locator('visible=true').first();
            await expect(gamma).toContainText('1 of 2');
            await expect(gamma).toContainText('Never verified');
            await expect(gamma).toContainText('Unknown');
            await expect(gamma).not.toContainText('Pending review');
            await expectNoHorizontalOverflow(page);
        });

        test('the definitions explain coverage, freshness and the evidence legend', async ({page}) => {
            await page.goto('/');
            await page.getByTestId('how-ranking-works').getByText('How ranking works').click();
            const defs = page.getByTestId('ranking-definitions');
            await expect(defs).toContainText('Rating coverage');
            await expect(defs).toContainText('Fact coverage');
            await expect(defs).toContainText('Freshness');
            await expect(page.getByTestId('evidence-legend')).toContainText('Conflicting observation');
        });

        test('the dialog separates coverage, stale facts and pending review', async ({page}) => {
            const dialog = await openDialog(page, 'E2E Beta Labs');
            await expect(dialog.getByTestId('coverage-summary')).toBeVisible();
            await expect(dialog.getByTestId('rating-coverage')).toContainText('2 of 2');
            await expect(dialog.getByTestId('last-updated')).toBeVisible();
            await expect(dialog.getByText('Editing the record does not re-verify facts')).toBeVisible();
            await expect(dialog.getByTestId('field-stale-rto_policy')).toBeVisible();
            await expect(dialog.getByTestId('field-review-rto_policy')).toContainText('Conflicting observation');
            const pending = dialog.getByTestId('pending-review-rto_policy');
            await expect(pending).toContainText('Observed, not verified');
            await expect(pending).toContainText('Fully in office');
            await expect(dialog.getByTestId('field-unverified-public_status')).toContainText('No accepted evidence');
            await expect(dialog.getByTestId('field-source-link-rto_policy')).toHaveAttribute(
                'rel', 'noopener noreferrer nofollow');
            await expectNoHorizontalOverflow(page);
        });

        test('an off-domain source is shown as text, never as a link', async ({page}) => {
            const dialog = await openDialog(page, 'E2E Alpha Corp');
            await expect(dialog.getByTestId('field-evidence-locations')).toContainText('e2e.example.test');
            await expect(dialog.getByTestId('field-source-link-locations')).toHaveCount(0);
            await expect(dialog.getByTestId('field-source-link-rto_policy')).toBeVisible();
        });

        test('an organization with no evidence explains it', async ({page}) => {
            const dialog = await openDialog(page, 'E2E Gamma Works');
            await expect(dialog.getByTestId('evidence-empty')).toBeVisible();
            await expect(dialog.getByTestId('evidence-empty-action')).toBeVisible();
        });

        test('a failed provenance request shows Retry and recovers', async ({page}) => {
            let failures = 1;
            await page.route('**/api/organizations/*/provenance/', async (route) => {
                if (failures-- > 0) {
                    await route.abort();
                } else {
                    await route.continue();
                }
            });
            const dialog = await openDialog(page, 'E2E Beta Labs');
            await expect(dialog.getByRole('alert')).toContainText('Evidence unavailable — try again');
            await dialog.getByRole('button', {name: 'Retry'}).click();
            await expect(dialog.getByTestId('coverage-summary')).toBeVisible();
            await expect(dialog.getByRole('alert')).toHaveCount(0);
        });
    });
}

for (const [width, height] of [[1280, 900], [375, 800], [320, 800]] as const) {
    test(`the details dialog never scrolls apart and fits the viewport at ${width}px`, async ({page}) => {
        await page.setViewportSize({width, height});
        const dialog = await openDialog(page, 'E2E Beta Labs');
        await expect(dialog.getByTestId('coverage-summary')).toBeVisible();
        const overlay = page.getByTestId('popup-overlay');
        const metrics = () => overlay.evaluate((el) => {
            const box = el.querySelector('[role="dialog"]')!.getBoundingClientRect();
            return {
                scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight,
                top: box.top, bottom: box.bottom, innerHeight: window.innerHeight,
            };
        });
        const before = await metrics();
        expect(before.scrollHeight).toBe(before.clientHeight);
        expect(before.bottom).toBeLessThanOrEqual(before.innerHeight);

        await page.mouse.move(4, Math.round(height / 2));
        await page.mouse.wheel(0, 900);
        await page.mouse.move(Math.round(width / 2), 55);
        await page.mouse.wheel(0, 900);
        await dialog.getByRole('button', {name: 'Close'}).focus();
        for (let i = 0; i < 3; i++) {
            await page.keyboard.press('PageDown');
        }
        const after = await metrics();
        expect(after.scrollTop).toBe(0);
        expect(after.top).toBe(before.top);
        expect(after.bottom).toBeLessThanOrEqual(after.innerHeight);
    });
}

test('a focused Retry stays inside the card body when scores arrive late at 320px', async ({page}) => {
    await page.setViewportSize({width: 320, height: 800});
    await page.route('**/api/organizations/*/provenance/', route => route.abort());
    await page.route('**/api/organizations/*/scores/', async route => {
        await new Promise(resolve => setTimeout(resolve, 800));
        await route.continue();
    });
    const dialog = await openDialog(page, 'E2E Beta Labs');
    const retry = dialog.getByRole('button', {name: 'Retry'});
    await retry.focus();
    const loadingBox = (await retry.boundingBox())!;
    await expect(dialog.getByText('Loading scores')).toHaveCount(0);
    expect(Math.abs((await retry.boundingBox())!.y - loadingBox.y)).toBeLessThanOrEqual(1);
    const [box, body] = await Promise.all([retry.boundingBox(), dialog.locator('.card-body').boundingBox()]);
    expect(box!.y + box!.height + 5).toBeLessThanOrEqual(body!.y + body!.height);
});

test('Retry keeps keyboard focus in the dialog through a failed retry', async ({page}) => {
    await page.route('**/api/organizations/*/provenance/', route => route.abort());
    const dialog = await openDialog(page, 'E2E Beta Labs');
    const retry = dialog.getByRole('button', {name: 'Retry'});
    await retry.focus();
    await page.keyboard.press('Enter');
    await expect(retry).toBeFocused();
    expect(await page.evaluate(() => document.activeElement?.tagName)).toBe('BUTTON');
});
