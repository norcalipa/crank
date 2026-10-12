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
        await page.route('**/api/company-corrections/?organization=*', (route) => route.fulfill({
            status: 200, contentType: 'application/json',
            body: JSON.stringify({corrections: [{id: 1, field_key: 'rto_policy', field_label: 'RTO policy',
                proposed_value: 'Remote first', status: 'pending', status_label: 'Pending review'}]}),
        }));
        await login(page);
        await page.goto('/');
        await opener(page).click();
        await expect(view(page)).toBeVisible();
        await expect(page.getByTestId('your-pending-corrections')).toContainText('Remote first');

        await page.evaluate(() => document.dispatchEvent(new CustomEvent('crank:private-state-purged')));

        await expect(view(page)).toHaveCount(0);
        await expect(page.getByTestId('your-pending-corrections')).toHaveCount(0);
        await expect(page.getByText('Remote first')).toHaveCount(0);
    });

    // Hands out the pending list from memory and stores nothing: the POST is stubbed
    // (corrections.spec.ts spends the seeded user's hourly allowance).
    async function stubCorrections(page: Page, initial: Array<Record<string, unknown>> = []) {
        const stored = [...initial];
        await page.route('**/api/company-corrections/**', async (route) => {
            const request = route.request();
            try {
                if (request.method() === 'POST') {
                    const sent = JSON.parse(request.postData() ?? '{}');
                    stored.push({id: 900 + stored.length, field_key: sent.field_key ?? 'rto_policy', field_label: 'RTO policy',
                        proposed_value: sent.proposed_value ?? 'x', status: 'pending', status_label: 'Pending review'});
                    await route.fulfill({status: 201, contentType: 'application/json', body: JSON.stringify({
                        field_label: 'RTO policy', proposed_value: sent.proposed_value, current_value: '', status_label: 'Pending review'})});
                } else {
                    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({corrections: stored})});
                }
            } catch {
                // the request was aborted by a navigation
            }
        });
    }

    const idOf = async (page: Page) => {
        const href = await page.getByTestId('company-chat-cta').getAttribute('href');
        return /company=(\d+)/.exec(href ?? '')![1];
    };

    test('the same view opens from /?company= and /algo/<n>/?company=, inside main#main-content, with the heading focused', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/');
        await opener(page).click();
        const id = await idOf(page);
        await page.getByRole('button', {name: 'Back to results'}).click();
        await page.getByTestId('ranking-preset-select').selectOption({label: 'E2E Culture Preset'});
        await page.waitForURL(/\/algo\/\d+\//);
        const algoPath = new URL(page.url()).pathname;

        for (const url of [`/?company=${id}`, `${algoPath}?company=${id}`]) {
            await page.goto(url);
            await expect(page.locator('main#main-content').getByTestId('company-view')).toBeVisible();
            await expect(page.locator('#organization-details-title')).toHaveText(COMPANY);
            await expect(page.locator('#organization-details-title')).toBeFocused();
            await expect(page.getByTestId('company-view')).toHaveAttribute('data-organization-id', id);
        }
    });

    for (const width of [1280, 1024]) {
        test(`not a dialog at ${width}: no overlay, nothing inert, the document scrolls, the assistant beside it stays usable`, async ({page}) => {
            await page.setViewportSize({width, height: width === 1280 ? 900 : 800});
            await login(page);
            await page.goto('/');
            await opener(page).click();
            await page.getByTestId('company-chat-cta').click();
            await expect(page.getByTestId('assistant-panel')).toBeVisible();
            await expect(view(page)).toBeVisible();

            await expect(page.locator('.popup-overlay')).toHaveCount(0);
            await expect(page.locator('[role="dialog"], [aria-modal="true"]')).toHaveCount(0);
            await expect(page.locator('[inert]')).toHaveCount(0);
            // The document is the scroller: nothing locked it, so it moves.
            await page.evaluate(() => window.scrollTo(0, 60));
            await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
            await expectNoHorizontalOverflow(page);
            const composerReachable = await page.evaluate(() => {
                const composer = document.querySelector('textarea[aria-label="Message"]') as HTMLElement;
                const box = composer.getBoundingClientRect();
                const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
                return hit === composer;
            });
            expect(composerReachable).toBe(true);
            await view(page).getByRole('button', {name: 'Back to results'}).focus();
            await expect(view(page).getByRole('button', {name: 'Back to results'})).toBeFocused();
        });
    }

    test('Escape does nothing in the view; with the drawer open it closes only the drawer', async ({page}) => {
        await page.setViewportSize({width: 1024, height: 800});
        await login(page);
        await page.goto('/');
        await opener(page).click();
        await expect(page.locator('#organization-details-title')).toBeFocused();
        await page.keyboard.press('Escape');
        await expect(view(page)).toBeVisible();

        await page.getByTestId('company-chat-cta').click();
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.getByTestId('assistant-panel')).toHaveCount(0);
        await expect(view(page)).toBeVisible();
        expect(new URL(page.url()).searchParams.get('company')).not.toBeNull();
    });

    test('Tab from the heading reaches Back to results first, then the action row', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/');
        await opener(page).click();
        await expect(page.getByTestId('company-chat-cta')).toBeVisible();
        await expect(page.locator('#organization-details-title')).toBeFocused();
        await page.keyboard.press('Tab');
        await expect(view(page).getByRole('button', {name: 'Back to results'})).toBeFocused();
        await page.keyboard.press('Tab');
        const next = await page.evaluate(() => (document.activeElement as HTMLElement).getAttribute('data-testid'));
        expect(next).toBe('company-chat-cta');
    });

    for (const trigger of ['suggest-correction-link', 'suggest-correction-field-rto_policy']) {
        test(`Suggest correction from ${trigger}: Cancel, Escape and submit keep the view and return focus to the trigger`, async ({page}) => {
            await page.setViewportSize({width: 1280, height: 900});
            await stubCorrections(page);
            await login(page);
            await page.goto('/');
            await opener(page).click();
            const control = view(page).getByTestId(trigger);
            const form = page.getByRole('dialog', {name: /Suggest a correction/});

            for (const close of ['Cancel', 'Escape']) {
                await control.scrollIntoViewIfNeeded();
                await control.focus();
                await control.press('Enter');
                await expect(form).toBeVisible();
                await expect(page.locator('[aria-modal="true"]')).toHaveCount(1);
                if (close === 'Cancel') await form.getByRole('button', {name: 'Cancel'}).click();
                else await page.keyboard.press('Escape');
                await expect(form).toHaveCount(0);
                await expect(page.locator('[aria-modal="true"]')).toHaveCount(0);
                await expect(view(page)).toBeVisible();
                await expect(control).toBeFocused();
            }

            await control.press('Enter');
            const field = form.getByRole('combobox', {name: 'Field'});
            if ((await field.inputValue()) === '') await field.selectOption({label: 'RTO Policy'});
            await form.getByRole('textbox', {name: 'Suggested value'}).fill('Remote-first');
            await form.getByRole('textbox', {name: 'Evidence link'}).fill('https://example.com/policy');
            await form.getByRole('button', {name: 'Submit suggestion'}).click();
            await expect(form.getByTestId('correction-status')).toContainText('Pending review');
            await form.getByRole('button', {name: `Back to ${COMPANY}`}).click();
            await expect(form).toHaveCount(0);
            await expect(page.locator('[aria-modal="true"]')).toHaveCount(0);
            await expect(view(page)).toBeVisible();
            await expect(control).toBeFocused();
            await expect(view(page).getByTestId('your-pending-corrections')).toContainText('Remote-first');
        });
    }

    test('Back, Back to results and Forward keep the search text, the filters, the draft and the conversation', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/?search=E2E&rto=R');
        const composer = page.locator('textarea[aria-label="Message"]');
        await page.locator('[data-testid="assistant-launcher"]').click();
        await composer.fill('a draft that must survive');
        const conversation = () => page.evaluate(async () => (await (await fetch('/api/agent/conversations/')).text()));
        const conversationBefore = await conversation();
        const seen = async () => ({
            search: new URL(page.url()).searchParams.get('search'),
            rto: new URL(page.url()).searchParams.get('rto'),
            company: new URL(page.url()).searchParams.get('company'),
            box: await page.locator('#organization-search').inputValue(),
            chip: await page.getByTestId('filter-chip-rto').count(),
        });
        const before = await seen();
        await expect(page.getByTestId('filter-chip-rto')).toBeVisible();
        before.chip = await page.getByTestId('filter-chip-rto').count();
        expect(before).toMatchObject({search: 'E2E', rto: 'R', company: null, box: 'E2E', chip: 1});
        // Whichever row the filter leaves first: the assertions are about that row.
        const row = page.locator('tr[aria-label^="View details for"], article[aria-label^="View details for"]').locator('visible=true').first();
        await row.scrollIntoViewIfNeeded();
        const top = (await row.boundingBox())!.y;

        for (const leave of ['browser', 'button'] as const) {
            await row.click();
            await expect(view(page)).toBeVisible();
            await expect(composer).toHaveValue('a draft that must survive');
            if (leave === 'browser') await page.goBack();
            else await page.getByRole('button', {name: 'Back to results'}).click();
            await expect(view(page)).toHaveCount(0);
            await expect.poll(async () => (await seen()).company).toBeNull();
            expect(await seen()).toEqual(before);
            await expect(row).toBeFocused();
            await expect.poll(async () => Math.abs((await row.boundingBox())!.y - top)).toBeLessThanOrEqual(2);
            await expect(composer).toHaveValue('a draft that must survive');
            expect(await conversation()).toBe(conversationBefore);
        }
        await page.goForward();
        await expect(view(page)).toBeVisible();
        await page.goBack();
        await expect(view(page)).toHaveCount(0);
    });

    test('opening another company from inside the view replaces the entry; from a deep link Back to results stays on the site', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/?search=E2E');
        await opener(page).click();
        const first = await idOf(page);
        const lengthOpen = await historyLength(page);
        await page.evaluate((id) => window.dispatchEvent(new CustomEvent('crank:company-open', {detail: {organizationId: Number(id) + 1}})), first);
        await expect.poll(() => companyParam(page)).toBe(String(Number(first) + 1));
        expect(await historyLength(page)).toBe(lengthOpen);
        await page.goBack();
        await expect(view(page)).toHaveCount(0);
        await expect(page.getByTestId('organization-results-region')).toBeVisible();

        await page.goto(`/?search=E2E&company=${first}`);
        await expect(view(page)).toBeVisible();
        const origin = new URL(page.url()).origin;
        await page.getByRole('button', {name: 'Back to results'}).click();
        await expect(view(page)).toHaveCount(0);
        expect(new URL(page.url()).origin).toBe(origin);
        expect(new URL(page.url()).searchParams.get('search')).toBe('E2E');
        expect(await companyParam(page)).toBeNull();
    });

    test('a 404 shows "no longer listed" with no evidence request and no retry; ids that cannot be ids make no request', async ({page}) => {
        await login(page);
        const requests: string[] = [];
        page.on('request', (request) => {
            if (/\/api\/organizations\/[^?]/.test(request.url())) requests.push(request.url());
        });
        for (const bad of ['abc', '1.5', '-1', '1%2F..%2F2', '']) {
            await page.goto(`/?company=${bad}`);
            await expect(page.getByTestId('organization-results-region')).toBeVisible();
            await expect(view(page)).toHaveCount(0);
        }
        expect(requests).toEqual([]);

        await page.goto('/?company=999999');
        await expect(page.getByTestId('company-not-found')).toHaveText('This company is no longer listed.');
        await expect(page.getByRole('button', {name: 'Retry'})).toHaveCount(0);
        expect(requests.filter((url) => /provenance|scores/.test(url))).toEqual([]);
    });

    test('an active company that is not in the ranked list opens and says "Not ranked in this list"', async ({page}) => {
        await login(page);
        // The seeded rating source is active and public but gives no ranked row; find its id by name.
        const id = await page.evaluate(async () => {
            for (let candidate = 1; candidate <= 30; candidate += 1) {
                const res = await fetch(`/api/organizations/${candidate}/`);
                if (res.ok && (await res.json()).name === 'E2E Rating Source') return candidate;
            }
            return null;
        });
        test.skip(id === null, 'the seeded rating source is not listed by the organizations API');
        await page.goto(`/?company=${id}`);
        await expect(view(page)).toBeVisible();
        await expect(page.getByTestId('not-ranked')).toHaveText(/Not ranked in this list/);
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
            if (size.width < 768) {
                expect(box.height).toBeGreaterThanOrEqual(44);
                // Every control in the view, not the first match: buttons, buttons-as-links, fields.
                const small = await view(page).locator('button, a.btn, input, select, textarea, summary, [role="button"]').evaluateAll((nodes) => nodes
                    .map((node) => ({node, rect: node.getBoundingClientRect()}))
                    .filter(({rect}) => rect.width > 0 && rect.height > 0)
                    .filter(({rect}) => rect.height < 44 || rect.width < 44)
                    .map(({node, rect}) => `${node.getAttribute('data-testid') ?? node.textContent?.trim().slice(0, 30)} ${Math.round(rect.width)}x${Math.round(rect.height)}`));
                expect(small, 'controls smaller than 44px').toEqual([]);
            }
            await expect(page.locator('main')).not.toHaveAttribute('inert', /.*/);
            await expect(page.getByRole('dialog')).toHaveCount(0);
            if (size.width >= 1280) {
                const main = (await page.locator('main#main-content').boundingBox())!;
                expect(main.width).toBeGreaterThanOrEqual(640);
            }
        });
    }
});
