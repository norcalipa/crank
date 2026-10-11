// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// The company as a view in the main workspace (issue #486b), over real Django
// pages: one history entry, Back restores the results, Ask and Suggest
// correction keep the view, stale and malformed ids, private-state purge, and
// layout at the four sizes. The correction POST is stubbed: corrections.spec.ts
// spends the seeded user's hourly allowance.
import {expect, test, Page} from '@playwright/test';
import {expectNoHorizontalOverflow, login, requireDjangoTier} from './support';

const COMPANY = 'E2E Alpha Corp';
const ROW = `tr[aria-label="View details for ${COMPANY}"], article[aria-label="View details for ${COMPANY}"]`;

const view = (page: Page) => page.getByTestId('company-view');
const companyParam = (page: Page) => page.evaluate(() => new URLSearchParams(window.location.search).get('company'));
const historyLength = (page: Page) => page.evaluate(() => window.history.length);
const opener = (page: Page) => page.locator(ROW).locator('visible=true').first();

test.describe('company view (issue #486b)', () => {
    test.beforeEach(() => {
        requireDjangoTier();
    });

    test('opening adds one history entry; Back restores the results and focuses the row; Forward reopens', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/?search=E2E');
        await expect(opener(page)).toBeVisible();
        const lengthBefore = await historyLength(page);

        await opener(page).click();
        await expect(view(page)).toBeVisible();
        await expect(page.locator('#organization-details-title')).toBeFocused();
        await expect.poll(() => companyParam(page)).not.toBeNull();
        expect(await historyLength(page)).toBe(lengthBefore + 1);
        await expect(page.getByTestId('organization-results-region')).toBeHidden();

        await page.goBack();
        await expect(view(page)).toHaveCount(0);
        await expect.poll(() => companyParam(page)).toBeNull();
        expect(new URL(page.url()).searchParams.get('search')).toBe('E2E');
        await expect(page.locator('#organization-search')).toHaveValue('E2E');
        await expect(opener(page)).toBeFocused();

        await page.goForward();
        await expect(view(page)).toBeVisible();
        await expect.poll(() => companyParam(page)).not.toBeNull();
    });

    test('Ask keeps the view, opens the assistant and the next turn carries the company context', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/');
        await opener(page).click();
        await expect(view(page)).toBeVisible();
        const bodies: string[] = [];
        page.on('request', (request) => {
            if (request.method() === 'POST' && /\/api\/agent\//.test(request.url())) {
                bodies.push(request.postData() ?? '');
            }
        });

        await page.getByTestId('company-chat-cta').click();
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        await expect(view(page)).toBeVisible();
        await page.locator('textarea[aria-label="Message"]').fill('What stands out here?');
        await page.getByLabel('Send message').click();
        await expect.poll(() => bodies.some((body) => body.includes('"surface":"company"') && body.includes('organization_id'))).toBe(true);
        await expect(view(page)).toBeVisible();
    });

    test('Suggest correction opens the dialog over the view; Cancel keeps the view and focus returns to the trigger', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await page.route('**/api/company-corrections/', (route) => (route.request().method() === 'POST'
            ? route.fulfill({status: 500, contentType: 'application/json', body: '{"error":"stubbed"}'})
            : route.continue()));
        await login(page);
        await page.goto('/');
        await opener(page).click();
        const trigger = view(page).getByTestId('suggest-correction-link');
        await expect(trigger).toBeVisible();
        await trigger.focus();
        await trigger.press('Enter');

        const form = page.getByRole('dialog', {name: /Suggest a correction/});
        await expect(form).toBeVisible();
        await expect(page.locator('[aria-modal="true"]')).toHaveCount(1);
        await form.getByRole('button', {name: 'Cancel'}).click();
        await expect(form).toHaveCount(0);
        await expect(view(page)).toBeVisible();
        await expect(trigger).toBeFocused();
    });

    test('a company that is gone shows the not-found state; a malformed id is ignored with no request', async ({page}) => {
        await login(page);
        const detailRequests: string[] = [];
        page.on('request', (request) => {
            if (/\/api\/organizations\//.test(request.url())) detailRequests.push(request.url());
        });
        await page.goto('/?company=abc');
        await expect(page.getByTestId('organization-results-region')).toBeVisible();
        await expect(view(page)).toHaveCount(0);
        expect(detailRequests).toEqual([]);

        await page.goto('/?company=999999');
        await expect(page.getByTestId('company-not-found')).toHaveText('This company is no longer listed.');
        expect(detailRequests.filter((url) => /provenance|scores/.test(url))).toEqual([]);
        await page.getByRole('button', {name: 'Back to results'}).click();
        await expect(view(page)).toHaveCount(0);
        await expect.poll(() => companyParam(page)).toBeNull();
    });

    test('a private-state purge closes the view and leaves no pending suggestion rows', async ({page}) => {
        await login(page);
        await page.goto('/');
        await opener(page).click();
        await expect(view(page)).toBeVisible();

        await page.evaluate(() => document.dispatchEvent(new CustomEvent('crank:private-state-purged')));

        await expect(view(page)).toHaveCount(0);
        await expect(page.getByTestId('your-pending-corrections')).toHaveCount(0);
    });

    for (const size of [
        {width: 1280, height: 900, name: 'docked'},
        {width: 1024, height: 800, name: 'drawer'},
        {width: 375, height: 667, name: 'phone'},
        {width: 320, height: 640, name: 'narrow phone'},
    ]) {
        test(`layout at ${size.width}x${size.height} (${size.name}): beside the assistant, no overflow, Back in the first screen`, async ({page}) => {
            await page.setViewportSize({width: size.width, height: size.height});
            await login(page);
            await page.goto('/');
            await opener(page).click();
            await expect(view(page)).toBeVisible();

            await expectNoHorizontalOverflow(page);
            const back = view(page).getByRole('button', {name: 'Back to results'});
            const box = (await back.boundingBox())!;
            expect(box.y + box.height).toBeLessThanOrEqual(size.height);
            if (size.width < 768) expect(box.height).toBeGreaterThanOrEqual(44);
            await expect(page.locator('main')).not.toHaveAttribute('inert', /.*/);
            await expect(page.getByRole('dialog')).toHaveCount(0);
            if (size.width >= 1280) {
                const main = (await page.locator('main#main-content').boundingBox())!;
                expect(main.width).toBeGreaterThanOrEqual(640);
            }
        });
    }
});
