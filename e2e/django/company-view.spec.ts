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

    const listRow = (page: Page) => page.locator('tr[aria-label^="View details for"], article[aria-label^="View details for"]').locator('visible=true').last();
    const detailUrl = /\/api\/organizations\/\d+\/$/;
    // The seeded rating source is active but not a ranked row, so only its
    // detail request can say what it is: the one company a failing request can break.
    const unlistedId = (page: Page) => page.evaluate(async () => {
        for (let candidate = 1; candidate <= 40; candidate += 1) {
            const res = await fetch(`/api/organizations/${candidate}/`);
            if (res.ok && (await res.json()).name === 'E2E Rating Source') return candidate;
        }
        return null;
    });

    for (const path of ['/', '/algo/2/']) {
        for (const size of [
            {width: 1280, height: 900, top: 56},
            {width: 1024, height: 800, top: 56},
            {width: 375, height: 667, top: 80},
            {width: 320, height: 640, top: 80},
        ]) {
            test(`the view starts at the top of the scroll area on ${path} at ${size.width}x${size.height}; the introduction returns after Back`, async ({page}) => {
                await page.setViewportSize({width: size.width, height: size.height});
                await login(page);
                await page.goto(path);
                const row = listRow(page);
                const label = (await row.getAttribute('aria-label'))!;
                await row.evaluate((node) => node.scrollIntoView({block: 'center', behavior: 'instant'}));
                const intro = page.locator('.homepage-container > h1 + p');
                const how = page.locator('.homepage-container > .how-ranking-works');
                await expect(intro).toBeVisible();
                const before = await row.evaluate((node) => node.getBoundingClientRect().top);

                await row.click();
                await expect(view(page)).toBeVisible();
                await expect(intro).toBeHidden();
                await expect(how).toBeHidden();
                await expect(page.getByRole('heading', {level: 1})).toHaveCount(1);
                const top = await view(page).evaluate((node) => node.getBoundingClientRect().top);
                expect(top).toBeLessThanOrEqual(size.top);
                if (size.width < 768) {
                    const back = (await view(page).getByRole('button', {name: 'Back to results'}).boundingBox())!;
                    expect(back.y + back.height).toBeLessThanOrEqual(size.height);
                    const share = await view(page).evaluate((node, vh) => {
                        const heading = node.querySelector('h2')!.getBoundingClientRect();
                        return (vh - heading.top) / vh;
                    }, size.height);
                    expect(share).toBeGreaterThanOrEqual(0.55);
                }

                await page.goBack();
                await expect(view(page)).toHaveCount(0);
                await expect(intro).toBeVisible();
                await expect(how).toBeVisible();
                const again = page.locator(`[aria-label="${label}"]`).locator('visible=true').first();
                await expect.poll(() => again.evaluate((node) => document.activeElement === node)).toBe(true);
                await expect.poll(async () => Math.abs((await again.evaluate((node) => node.getBoundingClientRect().top)) - before)).toBeLessThanOrEqual(2);
            });
        }
    }

    // One label/value row at a time: side by side, the label ends before the
    // value starts; stacked, it ends above it; it never overflows its box and
    // nothing in the row leaves the view.
    const measureRows = (page: Page) => page.evaluate(() => {
        const root = document.querySelector('[data-testid="company-view"]')!;
        const box = root.getBoundingClientRect();
        const problems: string[] = [];
        let rows = 0;
        root.querySelectorAll('.row').forEach((row) => {
            const label = row.querySelector(':scope > .text-end');
            const value = label?.nextElementSibling;
            if (!label || !value) return;
            rows += 1;
            const l = label.getBoundingClientRect();
            const v = value.getBoundingClientRect();
            const stacked = v.top >= l.bottom - 0.5 && Math.abs(v.left - l.left) < 2;
            const text = (label.textContent ?? '').trim();
            if (stacked ? l.bottom > v.top + 0.5 : l.right > v.left + 0.5) problems.push(`${text} overlaps its value`);
            if (label.scrollWidth > label.clientWidth + 0.5) problems.push(`${text} is clipped`);
            row.querySelectorAll('*').forEach((node) => {
                const r = node.getBoundingClientRect();
                if (r.width > 0 && (r.right > box.right + 0.5 || r.left < box.left - 0.5)) problems.push(`${text}: a box leaves the view`);
            });
        });
        return {rows, problems, overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
    });

    for (const width of [768, 800, 820, 834, 853, 900, 1000, 1024, 1100, 1280, 1366, 1440, 375, 320]) {
        test(`no label is printed over its value at ${width}px, assistant open and closed`, async ({page}) => {
            await page.setViewportSize({width, height: 800});
            await login(page);
            for (const assistant of width >= 768 ? ['closed', 'open'] : ['closed']) {
                await page.goto('/');
                if (assistant === 'open') await page.locator('[data-testid="assistant-launcher"]').click();
                await page.locator('tr[aria-label="View details for E2E Beta Labs"], article[aria-label="View details for E2E Beta Labs"]').locator('visible=true').first().click();
                await expect(view(page).getByTestId('provenance-section')).toBeVisible();
                await view(page).locator('details').evaluateAll((nodes) => nodes.forEach((node) => { (node as HTMLDetailsElement).open = true; }));
                const result = await measureRows(page);
                expect(result.rows, `rows measured (${assistant})`).toBeGreaterThan(8);
                expect(result.problems, `${width}px, assistant ${assistant}`).toEqual([]);
                expect(result.overflow).toBeLessThanOrEqual(0);
            }
        });
    }

    test('switching to a company that is gone or fails to load clears the assistant\'s company; Back focuses the opened row', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        // The list shows only this company, so the next id is not in it and the detail request decides.
        await page.goto('/?search=E2E+Alpha');
        await opener(page).click();
        // The assistant opened from its launcher follows the view; "Ask" would
        // make the company the assistant's own, which a switch does not take away.
        await page.locator('[data-testid="assistant-launcher"]').click();
        await expect(page.getByTestId('assistant-context-line')).toHaveText(`About ${COMPANY}`);

        await page.evaluate(() => window.dispatchEvent(new CustomEvent('crank:company-open', {detail: {organizationId: 999999}})));
        await expect(page.getByTestId('company-not-found')).toBeVisible();
        await expect.poll(() => companyParam(page)).toBe('999999');
        await expect(page.getByText(`About ${COMPANY}`)).toHaveCount(0);

        await page.goBack();
        await expect(view(page)).toHaveCount(0);
        await expect(opener(page)).toBeFocused();

        // A record that fails to load clears it the same way.
        await opener(page).click();
        await expect(page.getByTestId('assistant-context-line')).toHaveText(`About ${COMPANY}`);
        const unlisted = await unlistedId(page);
        test.skip(unlisted === null, 'the seeded rating source is not listed by the organizations API');
        await page.route(detailUrl, (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
        await page.evaluate((id) => window.dispatchEvent(new CustomEvent('crank:company-open', {detail: {organizationId: id}})), unlisted);
        await expect(page.getByTestId('company-view-retry')).toBeVisible();
        await expect(page.getByText(`About ${COMPANY}`)).toHaveCount(0);
        await expect(view(page).getByRole('heading', {name: 'Company not loaded'})).toBeVisible();
    });

    test('Retry keeps the focus in the view: the heading, then Back to results on the next Tab', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/');
        const id = await unlistedId(page);
        test.skip(id === null, 'the seeded rating source is not listed by the organizations API');
        let failing = true;
        await page.route(detailUrl, (route) => (failing
            ? route.fulfill({status: 500, contentType: 'application/json', body: '{}'})
            : route.continue()));
        await page.goto(`/?company=${id}`);
        const retry = page.getByTestId('company-view-retry');
        await expect(retry).toBeVisible();
        failing = false;
        await retry.focus();
        await page.keyboard.press('Enter');
        await expect(retry).toHaveCount(0);
        await expect(view(page).getByRole('heading', {name: 'E2E Rating Source'})).toBeVisible();
        await expect(page.locator('#organization-details-title')).toBeFocused();
        await page.keyboard.press('Tab');
        await expect(view(page).getByRole('button', {name: 'Back to results'})).toBeFocused();
    });

    for (const size of [{width: 375, height: 667}, {width: 320, height: 640}, {width: 390, height: 844}]) {
        test(`on a phone at ${size.width}x${size.height} the Assistant bar stays at the bottom edge while the view loads, is not found or fails`, async ({page}) => {
            await page.setViewportSize(size);
            await login(page);
            const settle = async (name: string) => {
                await expect(view(page)).toBeVisible();
                const m = await page.evaluate(() => {
                    const dock = document.querySelector('.assistant-dock')!.getBoundingClientRect();
                    return {dockBottom: dock.bottom, vh: window.innerHeight, scrollHeight: document.documentElement.scrollHeight};
                });
                expect(Math.abs(m.dockBottom - m.vh), `${name}: bar bottom ${m.dockBottom} vs ${m.vh}`).toBeLessThanOrEqual(1);
                expect(m.scrollHeight, `${name}: document height`).toBeLessThanOrEqual(m.vh);
            };

            let release: () => void = () => undefined;
            const gate = new Promise<void>((resolve) => { release = resolve; });
            await page.route(detailUrl, async (route) => {
                await gate;
                await route.fulfill({status: 500, contentType: 'application/json', body: '{}'}).catch(() => undefined);
            });
            await page.goto('/?company=1');
            await expect(page.getByTestId('company-view-loading')).toBeVisible();
            await settle('loading');
            release();
            await expect(page.getByTestId('company-view-retry')).toBeVisible();
            await settle('load error');
            await page.unroute(detailUrl);

            await page.goto('/?company=999999');
            await expect(page.getByTestId('company-not-found')).toBeVisible();
            await settle('not found');
        });
    }

    test('on a phone the sheet\'s first button names the company view it returns to', async ({page}) => {
        await page.setViewportSize({width: 375, height: 667});
        await login(page);
        await page.goto('/?search=E2E');
        await opener(page).click();
        await expect(view(page)).toBeVisible();
        await page.locator('[data-testid="assistant-launcher"]').click();
        const back = page.getByTestId('assistant-back-to-results');
        await expect(back).toHaveText(`Back to ${COMPANY}`);
        const box = (await back.boundingBox())!;
        expect(box.height).toBeGreaterThanOrEqual(44);
        await back.click();
        await expect(page.getByTestId('assistant-panel')).toHaveCount(0);
        await expect(view(page)).toBeVisible();
        await expect(view(page).getByRole('heading', {name: COMPANY})).toBeVisible();
    });

    // Round 2, G1: on a phone a company opened from inside the sheet closes the
    // sheet and is shown; the saved scroll position of the page the sheet was
    // opened over is only restored when that page is still there.
    const send = async (page: Page, text: string) => {
        await page.getByTestId('assistant-composer').fill(text);
        await page.getByRole('button', {name: 'Send message'}).click();
    };
    const pressOpen = async (page: Page) => {
        const open = page.getByRole('button', {name: /^Open E2E Alpha/});
        await expect(open).toBeVisible();
        await open.click();
    };
    // The assistant holds the company (Ask keeps it after Back), so the demo
    // provider can answer "Open this company" with an open_company button.
    const askThenLeave = async (page: Page) => {
        await opener(page).click();
        await expect(view(page)).toBeVisible();
        await page.getByTestId('company-chat-cta').click();
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        return page.getByTestId('assistant-back-to-results');
    };
    const sheetState = (page: Page) => page.evaluate(() => {
        const v = document.querySelector('[data-testid="company-view"]');
        const mid = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
        return {
            sheet: document.body.classList.contains('assistant-sheet'),
            centreInView: !!mid?.closest('[data-testid="company-view"]'),
            top: v ? v.getBoundingClientRect().top : null,
            id: v?.getAttribute('data-organization-id') ?? null,
            y: window.scrollY,
        };
    });

    for (const size of [{width: 375, height: 667}, {width: 320, height: 640}]) {
        test(`on a phone at ${size.width}x${size.height} "Open" pressed in the sheet closes it and shows the company at the top with the heading focused`, async ({page}) => {
            await page.setViewportSize(size);
            await login(page);
            await page.goto('/');
            const back = await askThenLeave(page);
            await back.click();
            await view(page).getByRole('button', {name: 'Back to results'}).click();
            await expect(view(page)).toHaveCount(0);
            await page.evaluate(() => window.scrollTo(0, 900));
            await page.locator('[data-testid="assistant-launcher"]').click();
            await expect(page.getByTestId('assistant-panel')).toBeVisible();
            await send(page, 'Open this company');
            await pressOpen(page);

            await expect(page.getByTestId('assistant-panel')).toHaveCount(0);
            await expect(page.locator('#organization-details-title')).toBeFocused();
            const state = await sheetState(page);
            expect(state.sheet).toBe(false);
            expect(state.centreInView).toBe(true);
            expect(state.top).toBeLessThanOrEqual(80);
            expect(state.y).toBe(0);
            await page.keyboard.press('Tab');
            await expect(view(page).getByRole('button', {name: 'Back to results'})).toBeFocused();
        });
    }

    test('on a phone the same company opened from the sheet keeps its place; the sheet still closes', async ({page}) => {
        await page.setViewportSize({width: 375, height: 667});
        await login(page);
        await page.goto('/');
        await askThenLeave(page);
        await page.getByTestId('assistant-back-to-results').click();
        await page.evaluate(() => window.scrollTo(0, 150));
        const before = await sheetState(page);
        await page.locator('[data-testid="assistant-launcher"]').click();
        await send(page, 'Open this company');
        await pressOpen(page);

        await expect(page.getByTestId('assistant-panel')).toHaveCount(0);
        const after = await sheetState(page);
        expect(after.id).toBe(before.id);
        expect(Math.abs(after.y - 150)).toBeLessThanOrEqual(2);
    });

    test('on a phone, Back under an open sheet and then closing it leaves the results at the opened row, not at the top', async ({page}) => {
        await page.setViewportSize({width: 375, height: 667});
        await login(page);
        await page.goto('/');
        // The first row, scrolled to the top of the screen: a position to lose, and far from the end of
        // the page, where the sheet's own change of the page's height (the bar under it) would clamp it.
        const row = page.locator('article[aria-label^="View details for"]').locator('visible=true').first();
        await row.evaluate((node) => node.scrollIntoView({block: 'start', behavior: 'instant'}));
        await page.evaluate(() => window.scrollBy(0, -60));
        const before = await row.evaluate((node) => node.getBoundingClientRect().top);
        const label = (await row.getAttribute('aria-label'))!;
        await row.click({position: {x: 20, y: 10}});
        await expect(view(page).getByTestId('provenance-section')).toBeVisible();
        // The view is at the top of the page, so that is the position the sheet saves.
        await expect.poll(() => page.evaluate(() => window.scrollY)).toBeLessThanOrEqual(2);
        await page.locator('[data-testid="assistant-launcher"]').click();
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        await page.goBack();
        await expect(view(page)).toHaveCount(0);
        // The page puts the results back under the sheet; closing the sheet must not move them
        // (a jump to the top that the page then repairs is still a jump).
        const again = page.locator(`[aria-label="${label}"]`).locator('visible=true').first();
        await expect.poll(async () => Math.abs((await again.evaluate((node) => node.getBoundingClientRect().top)) - before)).toBeLessThanOrEqual(2);
        // Every scroll the page is asked for from here on, whether or not it lands in the same frame
        // as the page's own repair.
        await page.evaluate(() => {
            const w = window as unknown as {__scrolls: number[]};
            w.__scrolls = [];
            const original = window.scrollTo.bind(window);
            window.scrollTo = ((...args: unknown[]) => {
                const first = args[0] as {top?: number} | number;
                w.__scrolls.push(typeof first === 'object' ? first.top ?? 0 : (args[1] as number));
                return (original as (...a: unknown[]) => void)(...args);
            }) as typeof window.scrollTo;
        });
        const placed = await page.evaluate(() => window.scrollY);
        await page.getByTestId('assistant-back-to-results').click();
        await expect(page.getByTestId('assistant-panel')).toHaveCount(0);
        // The scroll and the focus are settled in the same commit that removes the sheet.
        await expect(page.getByTestId('assistant-launcher')).toBeFocused();
        const asked = await page.evaluate(() => (window as unknown as {__scrolls: number[]}).__scrolls);
        expect(asked.filter((y) => Math.abs(y - placed) > 2), 'scrolls away from the results\' place').toEqual([]);

        await expect.poll(async () => Math.abs((await again.evaluate((node) => node.getBoundingClientRect().top)) - before)).toBeLessThanOrEqual(2);
        expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
    });

    for (const size of [{width: 1280, height: 900, name: 'docked'}, {width: 1024, height: 800, name: 'drawer'}]) {
        test(`at ${size.width}x${size.height} (${size.name}) "Open" pressed in the assistant leaves it open and shows the view at the top`, async ({page}) => {
            await page.setViewportSize(size);
            await login(page);
            await page.goto('/');
            const back = await askThenLeave(page);
            expect(await back.count()).toBe(0);
            await view(page).getByRole('button', {name: 'Back to results'}).click();
            await send(page, 'Open this company');
            await pressOpen(page);

            await expect(view(page)).toBeVisible();
            await expect(page.getByTestId('assistant-panel')).toBeVisible();
            await expect(page.locator('#organization-details-title')).toBeFocused();
            expect(await view(page).evaluate((node) => node.getBoundingClientRect().top)).toBeLessThanOrEqual(56);
        });
    }

    // Round 2, G2: every text run in the view, with every disclosure open, has
    // no more lines than words; the evidence text keeps room beside its link.
    const measureText = (page: Page) => page.evaluate(() => {
        const root = document.querySelector('[data-testid="company-view"]')!;
        root.querySelectorAll('details').forEach((node) => { node.open = true; });
        const box = root.getBoundingClientRect();
        const broken: string[] = [];
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            const text = (node.textContent ?? '').trim();
            const parent = node.parentElement;
            if (!text || !parent || !parent.offsetParent || parent.closest('.visually-hidden, [hidden]')) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            const lines = new Set([...range.getClientRects()].filter((r) => r.width > 0.5).map((r) => Math.round(r.top))).size;
            const words = text.split(/\s+/).length;
            if (lines <= words) continue;
            // A word wider than its column has to break; anything else is a crushed column.
            const column = parent.closest('.row > *, td, th, li, p, .evidence-row-value > *') ?? parent;
            const probe = document.createElement('span');
            probe.style.cssText = `position:absolute;visibility:hidden;white-space:nowrap;font:${getComputedStyle(parent).font}`;
            probe.textContent = text.split(/\s+/).sort((a, b) => b.length - a.length)[0];
            document.body.appendChild(probe);
            const wider = probe.getBoundingClientRect().width > column.getBoundingClientRect().width + 0.5;
            probe.remove();
            if (!wider) broken.push(`${text.slice(0, 30)}: ${lines} lines for ${words} words`);
        }
        const rows = [...root.querySelectorAll('.evidence-row-value')];
        const links = rows.map((row) => row.querySelector(':scope > .correction-action')).filter((a): a is Element => !!a);
        const beside = rows.flatMap((row) => {
            const text = row.firstElementChild;
            const link = row.querySelector(':scope > .correction-action');
            if (!text || !link) return [];
            const t = text.getBoundingClientRect();
            const l = link.getBoundingClientRect();
            return l.left >= t.right - 0.5 && l.top < t.bottom ? [t.width] : [];
        });
        return {
            broken,
            evidenceRows: rows.length,
            besideWidths: beside,
            links: links.map((a) => {
                const r = a.getBoundingClientRect();
                return {height: r.height, inside: r.left >= box.left - 0.5 && r.right <= box.right + 0.5};
            }),
            overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        };
    });

    for (const width of [768, 800, 820, 834, 853, 900, 991, 992, 1000, 1008, 1024, 1100, 1120, 1248, 1279, 1280, 1366, 1440, 375, 320]) {
        test(`no text in the view is crushed at ${width}px, assistant open and closed`, async ({page}) => {
            await page.setViewportSize({width, height: 800});
            await login(page);
            for (const assistant of width >= 768 ? ['closed', 'open'] : ['closed']) {
                await page.goto('/');
                if (assistant === 'open') await page.locator('[data-testid="assistant-launcher"]').click();
                await page.locator('tr[aria-label="View details for E2E Beta Labs"], article[aria-label="View details for E2E Beta Labs"]').locator('visible=true').first().click();
                await expect(view(page).getByTestId('provenance-section')).toBeVisible();
                const m = await measureText(page);
                expect(m.evidenceRows, `evidence rows (${assistant})`).toBeGreaterThan(0);
                expect(m.broken, `${width}px, assistant ${assistant}`).toEqual([]);
                for (const beside of m.besideWidths) expect(beside, `evidence text beside its link, ${width}px ${assistant}`).toBeGreaterThanOrEqual(200);
                for (const link of m.links) {
                    expect(link.height).toBeGreaterThanOrEqual(44);
                    expect(link.inside).toBe(true);
                }
                expect(m.overflow).toBeLessThanOrEqual(0);
            }
        });
    }

    // Round 2, G3: when the view is narrow the pending suggestion stacks and
    // every part sits at the section heading's left edge.
    for (const size of [
        {width: 375, height: 667, assistant: false},
        {width: 320, height: 640, assistant: false},
        {width: 768, height: 1024, assistant: true},
    ]) {
        test(`pending suggestion rows are left-aligned when they stack at ${size.width}x${size.height}${size.assistant ? ' with the drawer open' : ''}`, async ({page}) => {
            await page.setViewportSize(size);
            await login(page);
            await stubCorrections(page, [{id: 1, field_key: 'rto_policy', field_label: 'RTO Policy', proposed_value: 'Remote first',
                status: 'pending', status_label: 'Pending review'}]);
            await page.goto('/');
            if (size.assistant) await page.locator('[data-testid="assistant-launcher"]').click();
            await page.locator('tr[aria-label="View details for E2E Beta Labs"], article[aria-label="View details for E2E Beta Labs"]').locator('visible=true').first().click();
            const pending = view(page).getByTestId('your-pending-corrections');
            await expect(pending).toContainText('Remote first');
            const rows = await pending.evaluate((section) => {
                const left = (el: Element) => el.getBoundingClientRect().left;
                const heading = left(section.querySelector('h3, h4, h5, strong, [role="heading"]') ?? section.firstElementChild!);
                return [...section.querySelectorAll('[data-testid^="your-pending-correction-"]')].map((row) => ({
                    alignItems: getComputedStyle(row).alignItems,
                    stacked: [...row.children].every((c, i, all) => i === 0 || c.getBoundingClientRect().top >= all[i - 1].getBoundingClientRect().bottom - 0.5),
                    offsets: [...row.children].map((c) => Math.abs(left(c) - heading)),
                }));
            });
            expect(rows.length).toBeGreaterThan(0);
            for (const row of rows) {
                expect(row.stacked).toBe(true);
                expect(row.alignItems).toBe('flex-start');
                for (const offset of row.offsets) expect(offset).toBeLessThanOrEqual(1);
            }
        });
    }

    // Round 2, G5: the heading is phone-sized whenever the view is 28rem wide or less.
    for (const size of [
        {width: 768, height: 1024, assistant: true},
        {width: 375, height: 667, assistant: false},
        {width: 1280, height: 900, assistant: false},
    ]) {
        test(`the heading follows the view's width at ${size.width}x${size.height}${size.assistant ? ' with the drawer open' : ''}`, async ({page}) => {
            await page.setViewportSize(size);
            await login(page);
            await page.goto('/');
            if (size.assistant) await page.locator('[data-testid="assistant-launcher"]').click();
            await opener(page).click();
            await expect(view(page)).toBeVisible();
            const m = await page.evaluate(() => ({
                viewWidth: document.querySelector('[data-testid="company-view"]')!.getBoundingClientRect().width,
                fontSize: parseFloat(getComputedStyle(document.querySelector('#organization-details-title')!).fontSize),
            }));
            expect(m.fontSize).toBe(m.viewWidth <= 448 ? 20 : 28);
        });
    }
});
