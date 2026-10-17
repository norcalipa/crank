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
        // A ranked row carries no address: the view shows the seeded one from its record.
        await expect(view(page).locator('.row', {hasText: 'URL:'}).getByRole('link')).toHaveText('https://e2e.example.test/');
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
        // The turn names this company: the body's organization_id is the `company` in the page URL.
        await expect.poll(() => {
            const companyId = new URL(page.url()).searchParams.get('company');
            return bodies.some((body) => {
                try {
                    const {context} = JSON.parse(body);
                    return context?.surface === 'company' && companyId !== null && String(context.organization_id) === companyId;
                } catch {
                    return false;
                }
            });
        }).toBe(true);
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
        // Closed in place: no company in the address, no marker on the entry, and nothing to go Forward to.
        await expect.poll(() => companyParam(page)).toBeNull();
        expect(await page.evaluate(() => window.history.state?.crankCompanyView)).toBeUndefined();
        await page.goForward();
        await expect(view(page)).toHaveCount(0);
        expect(await companyParam(page)).toBeNull();
    });

    test('a purge with the account check failed (the account never known) closes the view without any history traversal', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.addInitScript(() => {
            const calls: string[] = [];
            (window as unknown as {__historyCalls: string[]}).__historyCalls = calls;
            for (const method of ['back', 'go', 'forward'] as const) {
                const original = window.history[method].bind(window.history);
                window.history[method] = ((...args: number[]) => {
                    calls.push(method);
                    return original(...args);
                }) as typeof window.history[typeof method];
            }
            window.addEventListener('popstate', () => calls.push('popstate'));
        });
        await page.route('**/api/account/whoami/**', (route) => route.abort());
        await page.goto('/');
        await opener(page).click();
        await expect(view(page)).toBeVisible();
        await expect.poll(() => page.evaluate(() => window.history.state?.crankCompanyView)).toBe(true);

        await page.evaluate(() => document.dispatchEvent(new CustomEvent('crank:private-state-purged')));

        await expect(view(page)).toHaveCount(0);
        await expect.poll(() => companyParam(page)).toBeNull();
        await expect.poll(() => page.evaluate(() => window.history.state?.crankCompanyView)).toBeUndefined();
        expect(await page.evaluate(() => window.history.state?.crankPosition)).toBeUndefined();
        expect(await page.evaluate(() => (window as unknown as {__historyCalls: string[]}).__historyCalls)).toEqual([]);
    });

    test('signing out with a company open sends the sign-out request and ends the session', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/');
        await opener(page).click();
        await expect(view(page)).toBeVisible();
        await expect.poll(() => companyParam(page)).not.toBeNull();
        const signOut = page.waitForRequest((request) => request.method() === 'POST'
            && new URL(request.url()).pathname === '/accounts/logout/');

        // The real form of the navigation, nothing stubbed.
        await page.locator('form[data-nav-logout-form] button[type="submit"]').locator('visible=true').first().click();

        await signOut;
        // The page may still be navigating: a check that lost its context asks again.
        await expect.poll(() => page.evaluate(() => fetch('/api/account/whoami/', {credentials: 'same-origin'})
            .then((response) => response.json()).then((body) => body.authenticated)).catch(() => null)).toBe(false);
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
        expect(id, 'the seeded rating source is listed by the organizations API').not.toBeNull();
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
        expect(unlisted, 'the seeded rating source is listed by the organizations API').not.toBeNull();
        await page.route(detailUrl, (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
        await page.evaluate((id) => window.dispatchEvent(new CustomEvent('crank:company-open', {detail: {organizationId: id}})), unlisted);
        await expect(page.getByTestId('company-view-retry')).toBeVisible();
        await expect(page.getByText(`About ${COMPANY}`)).toHaveCount(0);
        await expect(view(page).getByRole('heading', {name: 'Company not loaded'})).toBeVisible();
    });

    // Issue #486, review round 1: where the results land and which row is focused when they come back.
    // A short phone, so the seeded list's last row lies below the first screen.
    const SHORT_PHONE = {width: 375, height: 480};
    const RESULT_ROWS = 'tr.organization-row[data-organization-id], article.organization-card[data-organization-id]';
    const listedIds = (page: Page) => page.evaluate((selector) => [...document.querySelectorAll(selector)]
        .filter((row) => row.getBoundingClientRect().height > 0)
        .map((row) => row.getAttribute('data-organization-id') as string), RESULT_ROWS);
    // One evaluate reads the row's rectangle, the viewport and the focus together, and what covers two corners.
    const rowState = (page: Page, id: string) => page.evaluate(({selector, id}) => {
        const row = [...document.querySelectorAll(selector)]
            .find((el) => el.getAttribute('data-organization-id') === id && el.getBoundingClientRect().height > 0);
        if (!row) return null;
        const r = row.getBoundingClientRect();
        const covered = (y: number) => {
            const hit = document.elementFromPoint(r.left + 2, y);
            return !hit || !(hit === row || row.contains(hit));
        };
        return {
            top: r.top, bottom: r.bottom, viewport: window.innerHeight,
            inside: r.top >= 0 && r.bottom <= window.innerHeight,
            covered: covered(Math.max(r.top, 0) + 2) || covered(Math.min(r.bottom, window.innerHeight) - 2),
            focused: document.activeElement === row,
        };
    }, {selector: RESULT_ROWS, id});
    const expectRowShownAndFocused = async (page: Page, id: string) => {
        await expect.poll(async () => {
            const state = await rowState(page, id);
            return state !== null && state.inside && !state.covered && state.focused;
        }, {message: `row ${id} is on screen, uncovered and focused`}).toBe(true);
    };
    const lastListedId = async (page: Page) => {
        await page.goto('/');
        await expect(opener(page)).toBeVisible();
        const ids = await listedIds(page);
        expect(ids.length).toBeGreaterThan(2);
        const last = ids[ids.length - 1];
        const state = await rowState(page, last);
        expect(state?.top, 'the last row lies below the first screen').toBeGreaterThanOrEqual(SHORT_PHONE.height);
        return last;
    };

    test('from a direct link, Back to results after scrolling the company puts its row on screen with focus', async ({page}) => {
        await page.setViewportSize(SHORT_PHONE);
        await login(page);
        await lastListedId(page);
        // A row above the last: the old restore put the list at the end, where the last row happens to be on screen.
        const id = (await listedIds(page))[1];
        await page.goto(`/?company=${id}`);
        await expect(view(page).getByTestId('provenance-section')).toBeVisible();
        await page.evaluate(() => window.scrollTo({top: 500, behavior: 'instant' as ScrollBehavior}));
        await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(100);
        // A position saved on the entry while the company was open exists.
        await expect.poll(() => page.evaluate(
            () => (window.history.state?.crankPosition?.scrollY as number | undefined) ?? 0,
        )).toBeGreaterThan(100);

        await view(page).getByRole('button', {name: 'Back to results'}).click();

        await expect(view(page)).toHaveCount(0);
        await expectRowShownAndFocused(page, id);
    });

    test('from a direct link to a row below the first screen, Back to results puts it on screen with focus', async ({page}) => {
        await page.setViewportSize(SHORT_PHONE);
        await login(page);
        const id = await lastListedId(page);
        await page.goto(`/?company=${id}`);
        await expect(view(page).getByTestId('provenance-section')).toBeVisible();

        await view(page).getByRole('button', {name: 'Back to results'}).click();

        await expect(view(page)).toHaveCount(0);
        await expectRowShownAndFocused(page, id);
    });

    test('after a switch from inside the view, Back returns to the row the view was opened from, where it was', async ({page}) => {
        await page.setViewportSize(SHORT_PHONE);
        await login(page);
        const other = await lastListedId(page);
        await opener(page).scrollIntoViewIfNeeded();
        const first = await opener(page).getAttribute('data-organization-id') as string;
        expect(first).not.toBe(other);
        const before = await rowState(page, first);
        expect(before).not.toBeNull();
        await opener(page).click();
        await expect(view(page)).toBeVisible();
        await page.evaluate((id) => window.dispatchEvent(new CustomEvent('crank:company-open', {detail: {organizationId: Number(id)}})), other);
        await expect.poll(() => companyParam(page)).toBe(other);
        await expect(view(page).getByTestId('provenance-section')).toBeVisible();

        await page.goBack();

        await expect(view(page)).toHaveCount(0);
        await expectRowShownAndFocused(page, first);
        await expect.poll(async () => Math.abs(((await rowState(page, first))?.top ?? Infinity) - (before?.top ?? 0)), {
            message: 'the opened row is where it was',
        }).toBeLessThanOrEqual(2);
    });

    test('Retry keeps the focus in the view: the heading, then Back to results on the next Tab', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/');
        const id = await unlistedId(page);
        expect(id, 'the seeded rating source is listed by the organizations API').not.toBeNull();
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
        const rootStyle = getComputedStyle(root);
        const viewWidth = root.clientWidth - parseFloat(rootStyle.paddingLeft) - parseFloat(rootStyle.paddingRight);
        const broken: string[] = [];
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            const text = (node.textContent ?? '').trim();
            const parent = node.parentElement;
            if (!text || !parent || !parent.offsetParent || parent.closest('.visually-hidden, [hidden]')) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            // Rects of one line can differ in height and top by a pixel or two (a fallback font for one
            // glyph), so a new line starts only when a rect begins below the middle of the current one.
            let lines = 0;
            let lineTop = -Infinity;
            let lineHeight = 0;
            for (const r of [...range.getClientRects()].filter((rect) => rect.width > 0.5).sort((a, b) => a.top - b.top)) {
                if (r.top > lineTop + lineHeight / 2) {
                    lines += 1;
                    lineTop = r.top;
                    lineHeight = r.height;
                }
            }
            // A web address has break opportunities after its slashes and punctuation, so it may take more
            // lines than it has words when the text starts part-way along a line, without any column being
            // crushed: count its segments.
            const words = text.split(/\s+|(?<=[/?&=#])/).filter(Boolean).length;
            if (lines <= words) continue;
            // Only a word wider than the view itself has to break. A word that fits the view but not its own
            // column is a crushed column, and it is reported.
            const probe = document.createElement('span');
            probe.style.cssText = `position:absolute;visibility:hidden;white-space:nowrap;font:${getComputedStyle(parent).font}`;
            probe.textContent = text.split(/\s+/).sort((a, b) => b.length - a.length)[0];
            document.body.appendChild(probe);
            const wider = probe.getBoundingClientRect().width > viewWidth + 0.5;
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

    // CI's Chromium has no web font for the page and falls back to a wider one: check that font here too.
    const WIDE_FONT_CSS = "body, button, input, textarea, select { font-family: 'DejaVu Sans', sans-serif !important; }";
    for (const [width, wide] of [768, 800, 820, 834, 853, 900, 991, 992, 1000, 1008, 1024, 1100, 1120, 1248, 1279, 1280, 1366, 1440, 375, 320]
        .flatMap((w) => [[w, false], [w, true]] as const)) {
        test(`no text in the view is crushed at ${width}px${wide ? ' in the wider font' : ''}, assistant open and closed`, async ({page}) => {
            await page.setViewportSize({width, height: 800});
            await login(page);
            for (const assistant of width >= 768 ? ['closed', 'open'] : ['closed']) {
                await page.goto('/');
                if (wide) await page.addStyleTag({content: WIDE_FONT_CSS});
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

    // Round 3, V1: in "Check history" a date or "Never" is printed on one line and no label reaches its value.
    for (const size of [
        {width: 1316, height: 900, assistant: true},
        {width: 1000, height: 800, assistant: true},
        {width: 1080, height: 800, assistant: false},
        {width: 784, height: 800, assistant: false},
        {width: 320, height: 640, assistant: false},
    ]) {
        for (const wide of [false, true]) {
            test(`"Check history" keeps every date on one line at ${size.width}x${size.height}${size.assistant ? ' with the assistant open' : ''}${wide ? ' in the wider font' : ''}`, async ({page}) => {
                await page.setViewportSize({width: size.width, height: size.height});
                await login(page);
                await page.goto('/');
                if (wide) await page.addStyleTag({content: WIDE_FONT_CSS});
                if (size.assistant) await page.locator('[data-testid="assistant-launcher"]').click();
                await page.locator('tr[aria-label="View details for E2E Beta Labs"], article[aria-label="View details for E2E Beta Labs"]').locator('visible=true').first().click();
                await expect(view(page).getByTestId('provenance-section')).toBeVisible();

                const m = await page.evaluate(() => {
                    const root = document.querySelector('[data-testid="company-view"]')!;
                    root.querySelectorAll('details').forEach((node) => { node.open = true; });
                    const rectsOf = (node: Element) => {
                        const range = document.createRange();
                        range.selectNodeContents(node);
                        return [...range.getClientRects()].filter((rect) => rect.width > 0.5);
                    };
                    const linesOf = (node: Element) => {
                        let lines = 0;
                        let lineTop = -Infinity;
                        let lineHeight = 0;
                        for (const r of rectsOf(node).sort((a, b) => a.top - b.top)) {
                            if (r.top > lineTop + lineHeight / 2) {
                                lines += 1;
                                lineTop = r.top;
                                lineHeight = r.height;
                            }
                        }
                        return lines;
                    };
                    const values = [...root.querySelectorAll('.evidence-timestamps dl dd')].map((dd) => ({
                        text: dd.textContent, lines: linesOf(dd),
                    }));
                    const reaching = [...root.querySelectorAll('.evidence-timestamps dl dt')].flatMap((dt) => {
                        const dd = dt.nextElementSibling!;
                        const label = rectsOf(dt);
                        const value = rectsOf(dd);
                        const labelRight = Math.max(...label.map((r) => r.right));
                        const valueLeft = Math.min(...value.map((r) => r.left));
                        return labelRight > valueLeft + 0.5 ? [`${dt.textContent} reaches ${dd.textContent}`] : [];
                    });
                    return {
                        values, reaching,
                        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                    };
                });

                expect(m.values.length, 'dates in every "Check history"').toBeGreaterThan(0);
                expect(m.values.filter((value) => value.lines !== 1), 'dates not on exactly one line').toEqual([]);
                expect(m.reaching, 'labels printed over their values').toEqual([]);
                expect(m.overflow).toBeLessThanOrEqual(0);
            });
        }
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

// Fit, open jobs and scope in words (issue #486c). The seeded reader's saved
// priorities are one work-mode requirement, so the real fit has one row; the
// other states are stubbed answers of the two endpoints, never stored data.
test.describe('company view fit, jobs and scope (issue #486c)', () => {
    test.beforeEach(() => {
        requireDjangoTier();
    });

    const BETA = 'E2E Beta Labs';
    const openCompanyNamed = async (page: Page, name: string) => {
        await page.locator(`tr[aria-label="View details for ${name}"], article[aria-label="View details for ${name}"]`)
            .locator('visible=true').first().click();
        await expect(view(page)).toBeVisible();
    };
    const fitBody = (overrides: Record<string, unknown> = {}) => ({
        organization_id: 1, has_preferences: true, fit_score: 72.5, company_score: 4.2, coverage: 0.5,
        unsupported: [], revision: {preference_revision: 1, ranking_version: '1.0.0', data_revision: 1},
        requirements: [
            {path: 'funding_stage', status: 'match', hard: false, observed: 'b', scope_ok: true, scope: null,
                evidence_status: {state: 'verified', last_verified_at: null}},
            {path: 'work_location.max_in_office_days', status: 'mismatch', hard: true, observed: 3, scope_ok: true,
                scope: null, evidence_status: {state: 'profile'}},
            {path: 'compensation.require_public_company', status: 'mismatch', hard: false, observed: false,
                scope_ok: true, scope: null, evidence_status: {state: 'sourced'}},
            {path: 'industry', status: 'unknown', hard: false, observed: null, scope_ok: false,
                scope: {countries: ['US'], role_families: ['Engineering']}, evidence_status: {state: 'stale'}},
        ],
        ...overrides,
    });
    const stub = (page: Page, kind: 'fit' | 'jobs', respond: Parameters<Page['route']>[1]) =>
        page.route(`**/api/organizations/*/${kind}/`, respond);
    const json = (body: unknown, status = 200) => ({status, contentType: 'application/json', body: JSON.stringify(body)});

    test('the real fit and jobs answers render in the view, private and uncached', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/');
        const fit = page.waitForResponse((r) => /\/api\/organizations\/\d+\/fit\/$/.test(r.url()));
        const jobs = page.waitForResponse((r) => /\/api\/organizations\/\d+\/jobs\/$/.test(r.url()));
        await opener(page).click();
        const [fitResponse, jobsResponse] = await Promise.all([fit, jobs]);
        for (const response of [fitResponse, jobsResponse]) {
            expect(response.status()).toBe(200);
            expect(response.headers()['cache-control']).toBe('private, no-store');
        }
        await expect(page.getByTestId('fit-ready')).toBeVisible();
        for (const id of ['fit-score', 'fit-company-score', 'fit-coverage']) {
            await expect(page.getByTestId(id)).toBeVisible();
        }
        await expect(page.getByTestId('fit-row-work_location.modes')).toBeVisible();
        await expect(page.getByTestId('fit-scope-work_location.modes')).toHaveText(/Company-wide/);
        const jobRow = page.locator('[data-testid^="job-row-"]').first();
        await expect(jobRow).toContainText('E2E Seed Software Engineer');
        await expect(jobRow).toContainText('USD 120,000–160,000 per year');
        const link = jobRow.getByRole('link', {name: /View posting/});
        await expect(link).toHaveAttribute('target', '_blank');
        await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
        await expect(link).toHaveAttribute('href', /^https?:\/\//);
        // Headings: the view's h2, then its sections.
        const levels = await view(page).evaluate((section) => [...section.querySelectorAll('h2, h3')]
            .map((h) => `${h.tagName}:${h.textContent}`));
        expect(levels.indexOf('H2:E2E Alpha Corp')).toBe(0);
        expect(levels.indexOf('H3:Fit with your priorities')).toBeLessThan(levels.indexOf('H3:Open jobs'));
        expect(levels.indexOf('H3:Open jobs')).toBeLessThan(levels.indexOf('H3:Data Freshness & Sources'));
    });

    test('a company with no listings says "No open roles listed."', async ({page}) => {
        await login(page);
        await page.goto('/');
        await openCompanyNamed(page, BETA);
        await expect(page.getByTestId('jobs-empty')).toHaveText('No open roles listed.');
    });

    test('View jobs moves focus to the Jobs heading and puts it on screen', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 500});
        await login(page);
        await page.goto('/');
        await openCompanyNamed(page, BETA);
        await expect(page.getByTestId('jobs-empty')).toBeVisible();
        await page.getByTestId('company-view-jobs').click();
        await expect(page.getByRole('heading', {name: 'Open jobs'})).toBeFocused();
        await expect.poll(() => page.getByRole('heading', {name: 'Open jobs'}).evaluate((h) => {
            const box = h.getBoundingClientRect();
            return box.top >= 0 && box.bottom <= window.innerHeight;
        })).toBe(true);
    });

    test('scope is named in words: a scoped fact says where it applies, a company-wide one says so', async ({page}) => {
        await login(page);
        await page.goto('/');
        await opener(page).click();
        await expect(page.getByTestId('field-evidence')).toBeVisible();
        await expect(page.getByTestId('field-scope-funding_round')).toHaveText(/Location: US/);
        await expect(page.getByTestId('field-scope-rto_policy')).toHaveText(/Company-wide/);
        const text = await view(page).innerText();
        expect(text).not.toMatch(/countries|role_families/);
    });

    test('the four groups, a fact that does not cover the reader, and the three numbers', async ({page}) => {
        await login(page);
        await stub(page, 'fit', (route) => route.fulfill(json(fitBody())));
        await page.goto('/');
        await opener(page).click();
        await expect(page.getByTestId('fit-ready')).toBeVisible();
        await expect(page.getByTestId('fit-group-meets')).toContainText('Funding stage');
        await expect(page.getByTestId('fit-group-does-not-meet')).toContainText('In-office days');
        await expect(page.getByTestId('fit-group-tradeoffs')).toContainText('Public company');
        const unknown = page.getByTestId('fit-group-unknown');
        await expect(unknown).toContainText('Industry');
        // One sentence for a fact that does not cover the reader: no value, no status word, the scope said once.
        await expect(page.getByTestId('fit-out-of-scope-industry')).toHaveText(
            'Not counted: the fact on record covers Location: US; Roles: Engineering only.');
        await expect(page.getByTestId('fit-scope-industry')).toHaveCount(0);
        // The engine's own values read as words.
        await expect(page.getByTestId('fit-row-funding_stage')).toContainText('Series B');
        await expect(page.getByTestId('fit-row-work_location.max_in_office_days')).toContainText('3 days a week in the office');
        await expect(page.getByTestId('fit-score')).toHaveText('72.5');
        await expect(page.getByTestId('fit-company-score')).toHaveText('4.2 / 5');
        await expect(page.getByTestId('fit-coverage')).toHaveText('50%');
        expect(await view(page).innerText()).not.toMatch(/countries|role_families/);
    });

    test('empty groups, no saved priorities, and the control that opens the editor', async ({page}) => {
        await login(page);
        await stub(page, 'fit', (route) => route.fulfill(json(fitBody({requirements: [fitBody().requirements[0]]}))));
        await page.goto('/');
        await opener(page).click();
        await expect(page.getByTestId('fit-empty-tradeoffs')).toBeVisible();
        await expect(page.getByTestId('fit-empty-does-not-meet')).toBeVisible();
        await expect(page.getByTestId('fit-empty-unknown')).toBeVisible();
        await page.unroute('**/api/organizations/*/fit/');
        await stub(page, 'fit', (route) => route.fulfill(json(fitBody({has_preferences: false, requirements: []}))));
        await page.getByRole('button', {name: 'Back to results'}).click();
        await opener(page).click();
        await expect(page.getByTestId('fit-no-priorities')).toContainText('Set your priorities to see how E2E Alpha Corp fits.');
        await page.getByTestId('fit-set-priorities').click();
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        await expect(page.getByTestId('priorities-sidebar')).toBeVisible();
        await expect(view(page)).toBeVisible();
    });

    test('a failing fit shows a fixed sentence and Retry; the rest of the view is unaffected', async ({page}) => {
        await login(page);
        let calls = 0;
        await stub(page, 'fit', (route) => {
            calls += 1;
            return calls === 1 ? route.fulfill(json({detail: 'internal secret'}, 500)) : route.fulfill(json(fitBody()));
        });
        await page.goto('/');
        await opener(page).click();
        await expect(page.getByTestId('fit-error')).toHaveText(/Fit could not be loaded\./);
        expect(await view(page).innerText()).not.toContain('internal secret');
        await expect(page.getByTestId('popup-details-grid')).toBeVisible();
        await page.getByTestId('fit-retry').click();
        await expect(page.getByTestId('fit-ready')).toBeVisible();
    });

    test('an expired session (redirect or 401) is the sign-in prompt, never an error that cannot clear', async ({page}) => {
        await login(page);
        await stub(page, 'fit', (route) => route.fulfill(json({}, 401)));
        await stub(page, 'jobs', (route) => route.fulfill({status: 302, headers: {location: '/accounts/login/?next=/'}}));
        await page.goto('/');
        await opener(page).click();
        await expect(page.getByTestId('fit-sign-in')).toBeVisible();
        await expect(page.getByTestId('jobs-sign-in')).toBeVisible();
        await expect(page.getByTestId('fit-error')).toHaveCount(0);
        await expect(page.getByTestId('jobs-error')).toHaveCount(0);
    });

    test('signed out: sign-in prompts in both sections and no request to either endpoint', async ({page}) => {
        const asked: string[] = [];
        page.on('request', (request) => {
            if (/\/api\/organizations\/\d+\/(fit|jobs)\//.test(request.url())) asked.push(request.url());
        });
        await page.goto('/');
        await opener(page).click();
        await expect(page.getByTestId('fit-sign-in')).toBeVisible();
        await expect(page.getByTestId('jobs-sign-in')).toBeVisible();
        await expect(page.getByTestId('fit-sign-in').getByRole('link')).toHaveAttribute('href', /\/accounts\/login\//);
        await page.getByTestId('company-view-jobs').click();
        await expect(page.getByRole('heading', {name: 'Open jobs'})).toBeFocused();
        await expect(view(page).getByTestId('provenance-section')).toBeVisible();
        expect(asked).toEqual([]);
    });

    test('jobs: inventory unavailable is worded apart from empty; an error has Retry; a javascript: address has no link', async ({page}) => {
        await login(page);
        await stub(page, 'jobs', (route) => route.fulfill(json({organization_id: 1, inventory: 'unavailable', count: 0, results: []})));
        await page.goto('/');
        await opener(page).click();
        await expect(page.getByTestId('jobs-unavailable')).toContainText('Job listings are not available yet');
        await expect(page.getByTestId('jobs-empty')).toHaveCount(0);
        await page.unroute('**/api/organizations/*/jobs/');
        let calls = 0;
        await stub(page, 'jobs', (route) => {
            calls += 1;
            if (calls === 1) return route.fulfill(json({}, 500));
            return route.fulfill(json({organization_id: 1, inventory: 'available', count: 2, results: [
                {id: 1, title: 'Risky link', location_text: '', is_remote: false, compensation_min: null, compensation_max: null,
                    compensation_currency: '', compensation_interval: '', canonical_url: 'javascript:alert(1)', status: 'active'},
                {id: 2, title: 'Fine link', location_text: 'Remote', is_remote: true, compensation_min: '100000.00',
                    compensation_max: '120000.00', compensation_currency: 'USD', compensation_interval: 'year',
                    canonical_url: 'https://jobs.example.test/2', status: 'active'},
            ]}));
        });
        await page.getByRole('button', {name: 'Back to results'}).click();
        await opener(page).click();
        await expect(page.getByTestId('jobs-error')).toContainText('Open jobs could not be loaded.');
        await page.getByTestId('jobs-retry').click();
        await expect(page.getByTestId('job-row-1')).toContainText('Risky link');
        await expect(page.getByTestId('job-link-1')).toHaveCount(0);
        await expect(page.getByTestId('job-link-2')).toHaveAttribute('href', 'https://jobs.example.test/2');
        await expect(page.getByTestId('job-row-2')).toContainText('USD 100,000–120,000 per year');
    });

    test('opening another company shows the next company\'s sections, never the previous one\'s', async ({page}) => {
        await login(page);
        await stub(page, 'jobs', async (route) => {
            const id = /organizations\/(\d+)\/jobs/.exec(route.request().url())![1];
            if (id !== (await page.evaluate(() => new URLSearchParams(window.location.search).get('company')))) {
                await new Promise((resolve) => setTimeout(resolve, 600));
            }
            return route.fulfill(json({organization_id: Number(id), inventory: 'available', count: 1, results: [
                {id: Number(id), title: `Job of ${id}`, location_text: '', is_remote: false, compensation_min: null,
                    compensation_max: null, compensation_currency: '', compensation_interval: '',
                    canonical_url: 'https://jobs.example.test/x', status: 'active'}]}));
        });
        await page.goto('/');
        await opener(page).click();
        const first = await companyParam(page);
        await expect(page.getByTestId(`job-row-${first}`)).toBeVisible();
        await page.evaluate(() => window.dispatchEvent(new CustomEvent('crank:company-open', {detail: {organizationId: 999999}})));
        await expect(page.getByTestId('company-not-found')).toBeVisible();
        await expect(page.locator('[data-testid^="job-row-"]')).toHaveCount(0);
    });

    for (const size of [
        {width: 1280, height: 900, assistant: false},
        {width: 1280, height: 900, assistant: true},
        {width: 1024, height: 800, assistant: true},
        {width: 768, height: 1024, assistant: false},
        {width: 375, height: 667, assistant: false},
        {width: 320, height: 640, assistant: false},
    ]) {
        test(`the new sections fit at ${size.width}x${size.height}${size.assistant ? ' with the assistant open' : ''}: no sideways scroll, 44px controls on phones`, async ({page}) => {
            await page.setViewportSize(size);
            await login(page);
            await stub(page, 'fit', (route) => route.fulfill(json(fitBody())));
            await stub(page, 'jobs', (route) => route.fulfill(json({organization_id: 1, inventory: 'available', count: 1, results: [
                {id: 1, title: 'A very long job title that keeps going to test wrapping inside the narrow column',
                    location_text: 'Remote (United States and Canada only, with some overlap hours)', is_remote: true,
                    compensation_min: '100000.00', compensation_max: '120000.00', compensation_currency: 'USD',
                    compensation_interval: 'year', canonical_url: 'https://jobs.example.test/1', status: 'active'}]})));
            await page.goto('/');
            if (size.assistant) await page.locator('[data-testid="assistant-launcher"]').click();
            await opener(page).click();
            await expect(page.getByTestId('fit-ready')).toBeVisible();
            await expect(page.getByTestId('job-row-1')).toBeVisible();
            await expectNoHorizontalOverflow(page);
            const wide = await view(page).evaluate((section) => section.scrollWidth > section.clientWidth + 1);
            expect(wide).toBe(false);
            if (size.width <= 768) {
                const small = await view(page).evaluate((section) => [...section.querySelectorAll(
                    '.company-fit :is(button, a, summary), .company-jobs :is(button, a), [data-testid="company-view-jobs"], [data-testid="company-see-fit"]')]
                    .map((el) => ({name: el.textContent?.trim(), height: el.getBoundingClientRect().height}))
                    .filter((box) => box.height < 44));
                expect(small).toEqual([]);
            }
        });
    }
});

// What the page says on a real server, never a stub (issue #486c, visual review
// round 1). These cases write the priorities of the seeded `e2e_prefs_user`,
// which only priorities.spec.ts, assistant-actions.spec.ts and chat-layout.spec.ts
// also read; every case puts the account back as seeded (one remote-work
// priority) in `finally`.
const PREFS_USER = 'e2e_prefs_user';
const E2E_PASSWORD = 'e2e-throwaway-password';
const SIX = {set: {
    'work_location.modes': ['remote'], 'work_location.max_in_office_days': 2, funding_stage: ['public'],
    'compensation.require_public_company': true, industry: ['Software'], 'vesting.prefer_accelerated': true,
    importance: {'work_location.max_in_office_days': 1.0, 'compensation.require_public_company': 1.0},
}};
const SEEDED = {set: {'work_location.modes': ['remote']}};

/** Resets the signed-in account's priorities through the API, then applies `patch` (null: none). */
async function setPriorities(page: Page, patch: object | null): Promise<void> {
    const failed = await page.evaluate(async (todo) => {
        const csrf = document.cookie.split('; ').find((c) => c.startsWith('csrftoken='))?.split('=')[1] || '';
        const headers = {'Content-Type': 'application/json', 'X-CSRFToken': decodeURIComponent(csrf)};
        const send = (url: string, body: unknown) => fetch(url, {method: 'POST', headers, body: JSON.stringify(body)});
        for (let attempt = 0; attempt < 4; attempt += 1) {
            const current = await (await fetch('/api/agent/preferences/')).json();
            let status = 0;
            if (current.chips.length > 0) {
                status = (await send('/api/agent/preferences/reset/', {expected_revision: current.revision})).status;
                if (status >= 400) { if (status === 409) continue; return `reset ${status}`; }
            }
            if (!todo) return '';
            const proposed = await send('/api/agent/preferences/propose/', {patch: todo, scope: 'account'});
            if (!proposed.ok) return `propose ${proposed.status}`;
            const applied = await send('/api/agent/preferences/apply/', {proposal: (await proposed.json()).token, decision: 'apply'});
            if (applied.ok) return '';
            if (applied.status !== 409) return `apply ${applied.status}`;
        }
        return 'conflict';
    }, patch);
    expect(failed).toBe('');
}

const BG = [33, 37, 41];
const luminance = ([r, g, b]: number[]) => {
    const lin = [r, g, b].map((v) => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
};
const contrastOf = (a: number[], b: number[]) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
};
const colourOf = (locator: ReturnType<Page['locator']>) => locator.evaluate((el) =>
    (getComputedStyle(el).color.match(/\d+(\.\d+)?/g) || []).slice(0, 3).map(Number));

/** The page's smooth scrolling has come to rest. */
async function scrollRested(page: Page): Promise<void> {
    let last = -1;
    await expect.poll(async () => {
        const now = await page.evaluate(() => window.scrollY);
        const rested = now === last;
        last = now;
        return rested;
    }, {intervals: [150]}).toBe(true);
}

const SIZES = [
    {width: 1280, height: 900}, {width: 1024, height: 800}, {width: 768, height: 1024},
    {width: 375, height: 667}, {width: 320, height: 640},
];

test.describe('company view on a real server, round 2 (issue #486c)', () => {
    test.beforeEach(() => {
        requireDjangoTier();
    });

    test('real fit rows print words for the engine\'s own values, one sentence for a fact that does not cover the reader, and "No accepted evidence"', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page, PREFS_USER, E2E_PASSWORD);
        try {
            await setPriorities(page, SIX);
            await page.goto('/');
            await opener(page).click();
            await expect(page.getByTestId('fit-ready')).toBeVisible();
            const value = (path: string) => page.getByTestId(`fit-row-${path}`).locator('.company-fit-value');
            await expect(value('work_location.modes')).toHaveText('Remote');
            await expect(value('work_location.max_in_office_days')).toHaveText('0 days a week in the office');
            await expect(value('vesting.prefer_accelerated')).toHaveText('No');
            // A fact limited to one country does not decide this reader's funding stage: one sentence.
            const notCounted = page.getByTestId('fit-row-funding_stage');
            await expect(notCounted).toContainText('Not counted: the fact on record covers Location: US only.');
            await expect(notCounted.locator('.company-fit-value')).toHaveCount(0);
            await expect(page.getByTestId('fit-group-unknown')).toContainText('Funding stage');
            await expect(page.getByTestId('fit-group-meets')).not.toContainText('Funding stage');
            // No fact behind a requirement is said as such, not "Company-wide".
            await expect(page.getByTestId('fit-scope-compensation.require_public_company')).toHaveText('No accepted evidence');
            // A value is never a bare lower-case code or a bare number, and "In-office days: In-Office" cannot occur.
            const values = await view(page).locator('.company-fit-value').allInnerTexts();
            expect(values.length).toBeGreaterThan(0);
            for (const text of values) expect(text).not.toMatch(/^([a-z]|\d+|in-office|remote|hybrid)$/);
            await expect(page.getByTestId('fit-row-work_location.max_in_office_days')).not.toContainText(/In-office days\s*In-Office/);

            // Another company, a different path of the same requirements: a funding letter becomes a stage.
            await page.getByRole('button', {name: 'Back to results'}).click();
            await page.locator(`tr[aria-label="View details for E2E Beta Labs"], article[aria-label="View details for E2E Beta Labs"]`)
                .locator('visible=true').first().click();
            await expect(page.getByTestId('fit-ready')).toBeVisible();
            await expect(value('funding_stage')).toHaveText('Series B');
        } finally {
            await setPriorities(page, SEEDED);
        }
    });

    test('real answers for priorities decided per job, a country, and priorities the engine cannot check', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page, PREFS_USER, E2E_PASSWORD);
        try {
            await setPriorities(page, {set: {
                'work_location.modes': ['remote'], 'work_location.countries': ['US'], 'compensation.minimum_salary': 100000,
                'geography.remote_friendly': true, 'roles.titles': ['Engineer'], 'compensation.minimum_total_compensation': 150000,
            }});
            await page.goto('/');
            await opener(page).click();
            await expect(page.getByTestId('fit-ready')).toBeVisible();
            // Decided from a job's own pay, place and remote flag, so a company is never given an answer: the row says where.
            for (const path of ['compensation.minimum_salary', 'geography.remote_friendly']) {
                await expect(page.getByTestId(`fit-scope-${path}`)).toHaveText('Checked for each job, not for the company. See Open jobs.');
            }
            // The reader's own country in capitals, not the engine's lower-case token.
            await expect(page.getByTestId('fit-row-work_location.countries').locator('.company-fit-value')).not.toHaveText(/^[a-z]+$/);
            // Priorities the engine cannot check are named, not left out.
            await expect(page.getByTestId('fit-unsupported'))
                .toHaveText('Not checked for a company: Role titles, Minimum total compensation.');
            expect(await view(page).innerText()).not.toContain('No fact on record');

            // A reader who has only priorities of that kind is not told to set priorities.
            await setPriorities(page, {set: {'roles.titles': ['Engineer']}});
            await page.reload();
            await expect(page.getByTestId('fit-unsupported-only')).toContainText('Role titles');
            await expect(page.getByTestId('fit-no-priorities')).toHaveCount(0);
            await expect(page.getByRole('button', {name: 'Edit your priorities'})).toBeVisible();
        } finally {
            await setPriorities(page, SEEDED);
        }
    });

    for (const [kind, button, heading] of [['fit', 'fit-retry', 'company-fit-title'], ['jobs', 'jobs-retry', 'company-jobs-title']]) {
        test(`Retry loading ${kind} keeps the keyboard focus in the section, on its heading`, async ({page}) => {
            await page.setViewportSize({width: 375, height: 667});
            await login(page);
            let calls = 0;
            await page.route(`**/api/organizations/*/${kind}/`, (route) => {
                calls += 1;
                return calls === 1 ? route.fulfill({status: 500, contentType: 'application/json', body: '{}'}) : route.continue();
            });
            await page.goto('/');
            await opener(page).click();
            const retry = page.getByTestId(button);
            await expect(retry).toBeVisible();
            await retry.focus();
            await page.keyboard.press('Enter');
            await expect(page.locator(`#${heading}`)).toBeFocused();
            await expect(page.getByTestId(kind === 'fit' ? 'fit-ready' : 'company-jobs').first()).toBeVisible();
            await expect(page.locator(`#${heading}`)).toBeFocused();
        });
    }

    test('a reader who scrolled past the fit is not moved when its answer arrives and grows the page', async ({page}) => {
        await page.setViewportSize({width: 375, height: 667});
        await login(page, PREFS_USER, E2E_PASSWORD);
        try {
            await setPriorities(page, SIX);
            let release: () => void = () => {};
            const held = new Promise<void>((resolve) => { release = resolve; });
            await page.route('**/api/organizations/*/fit/', async (route) => {
                await held;
                await route.continue();
            });
            await page.goto('/');
            await opener(page).click();
            await expect(page.getByTestId('fit-loading')).toBeVisible();
            const evidence = page.getByTestId('field-evidence');
            await evidence.scrollIntoViewIfNeeded();
            await evidence.evaluate((el) => el.scrollIntoView({block: 'start'}));
            await scrollRested(page);
            const top = () => evidence.evaluate((el) => el.getBoundingClientRect().top);
            const before = await top();
            release();
            await expect(page.getByTestId('fit-ready')).toBeVisible();
            await scrollRested(page);
            // The answer is more than 500 px tall; what the reader was looking at stays where it was.
            expect(Math.abs((await top()) - before)).toBeLessThanOrEqual(40);
        } finally {
            await page.unroute('**/api/organizations/*/fit/');
            await setPriorities(page, SEEDED);
        }
    });

    test('the summary names the fact it counts as unknown for covering only part of the company', async ({page}) => {
        await login(page);
        await page.goto('/');
        await opener(page).click();
        await expect(page.getByTestId('field-evidence')).toBeVisible();
        const line = page.getByTestId('coverage-scoped');
        await expect(line).toHaveText('Counted as unknown because it covers only part of the company: Funding Round (Location: US).');
        await expect(page.getByTestId('coverage-facts')).toContainText('2 of 7 tracked facts');
        const scoped = page.getByTestId('field-scope-funding_round');
        await expect(scoped).toHaveText('Covers only Location: US');
        // Not the muted colour, and readable on the view's background.
        expect(contrastOf(await colourOf(scoped), BG)).toBeGreaterThanOrEqual(4.5);
        expect(contrastOf(await colourOf(line), BG)).toBeGreaterThanOrEqual(4.5);
        expect(await colourOf(scoped)).not.toEqual(await colourOf(page.getByTestId('field-scope-rto_policy')));
        await expect(page.getByTestId('field-scope-rto_policy')).toHaveText(/Company-wide/);
        for (const size of SIZES) {
            await page.setViewportSize(size);
            await expectNoHorizontalOverflow(page);
        }
    });

    for (const path of ['/', '/algo/1/']) {
        test(`"View jobs" and "See your fit" put the heading on screen below the top bar, with a ring from the keyboard (${path})`, async ({page}) => {
            await login(page);
            for (const size of [...SIZES, {width: 1280, height: 500}]) {
                await page.setViewportSize(size);
                for (const [button, heading, name] of [['company-view-jobs', 'company-jobs-title', 'Open jobs'],
                    ['company-see-fit', 'company-fit-title', 'Fit with your priorities']]) {
                    for (const how of ['pointer', 'enter']) {
                        await page.goto(path);
                        await opener(page).click();
                        await expect(page.getByTestId('jobs-empty').or(page.locator('[data-testid^="job-row-"]').first())).toBeVisible();
                        await expect(page.getByTestId('fit-ready')).toBeVisible();
                        if (how === 'pointer') {
                            await page.getByTestId(button).click();
                        } else {
                            await page.getByTestId(button).focus();
                            await page.keyboard.press('Enter');
                        }
                        const h = page.locator(`#${heading}`);
                        await expect(h).toBeFocused();
                        await scrollRested(page);
                        await page.mouse.move(0, 0);
                        const m = await h.evaluate((el) => {
                            const bar = document.querySelector('.app-mobile-topbar');
                            const barBottom = bar && getComputedStyle(bar).display !== 'none' ? bar.getBoundingClientRect().bottom : 0;
                            const box = el.getBoundingClientRect();
                            const hits = [[box.left + 2, box.top + 2], [box.left + box.width / 2, box.top + box.height / 2],
                                [Math.min(box.right, innerWidth) - 2, box.bottom - 2]]
                                .map(([x, y]) => { const t = document.elementFromPoint(x, y); return !!t && (t === el || el.contains(t)); });
                            return {top: box.top, bottom: box.bottom, barBottom, hits, outline: getComputedStyle(el).outlineStyle,
                                focusVisible: el.matches(':focus-visible'), inner: innerHeight};
                        });
                        const label = `${path} ${size.width}x${size.height} ${button} by ${how}: ${JSON.stringify(m)}`;
                        expect(m.top, label).toBeGreaterThanOrEqual(m.barBottom);
                        expect(m.bottom, label).toBeLessThanOrEqual(m.inner);
                        expect(m.hits, label).toEqual([true, true, true]);
                        if (how === 'enter') {
                            expect(m.focusVisible, label).toBe(true);
                            expect(m.outline, label).not.toBe('none');
                        }
                        expect(name).toBeTruthy();
                    }
                }
            }
        });
    }

    test('with no listings the Jobs heading is on screen above "No open roles listed."', async ({page}) => {
        await login(page);
        for (const size of [{width: 375, height: 667}, {width: 320, height: 640}, {width: 1280, height: 900}]) {
            await page.setViewportSize(size);
            await page.goto('/');
            await page.locator('tr[aria-label="View details for E2E Beta Labs"], article[aria-label="View details for E2E Beta Labs"]')
                .locator('visible=true').first().click();
            await expect(page.getByTestId('jobs-empty')).toBeVisible();
            await expect(page.getByTestId('fit-ready')).toBeVisible();
            await page.getByTestId('company-view-jobs').click();
            await scrollRested(page);
            const [heading, empty] = await Promise.all([
                page.locator('#company-jobs-title').boundingBox(), page.getByTestId('jobs-empty').boundingBox()]);
            expect(heading!.y).toBeGreaterThanOrEqual(0);
            expect(heading!.y + heading!.height).toBeLessThanOrEqual(empty!.y);
            expect(empty!.y + empty!.height).toBeLessThanOrEqual(size.height);
        }
    });

    test('the two jump buttons share one row on phones, each 44px high with its label on one line', async ({page}) => {
        await login(page);
        for (const size of [{width: 375, height: 667}, {width: 320, height: 640}]) {
            await page.setViewportSize(size);
            await page.goto('/');
            await opener(page).click();
            const [fit, jobs] = await Promise.all([
                page.getByTestId('company-see-fit').boundingBox(), page.getByTestId('company-view-jobs').boundingBox()]);
            expect(fit!.y).toBe(jobs!.y);
            expect(fit!.x).toBeLessThan(jobs!.x);
            for (const box of [fit!, jobs!]) {
                expect(box.height).toBeGreaterThanOrEqual(44);
                expect(box.height).toBeLessThan(60);
            }
            await expectNoHorizontalOverflow(page);
        }
    });

    test('a late fit answer does not carry the focused heading off the screen; a reader who scrolled is left alone', async ({page}) => {
        await login(page);
        for (const size of [{width: 375, height: 667}, {width: 320, height: 640}]) {
            await page.setViewportSize(size);
            let release: () => void = () => {};
            const held = new Promise<void>((resolve) => { release = resolve; });
            await page.route('**/api/organizations/*/fit/', async (route) => {
                await held;
                await route.continue();
            });
            await page.goto('/');
            // The browser's own scroll anchoring would keep a fully visible heading in place by itself; this
            // case is about what the page does when it does not (a heading under the bar, a browser without it).
            await page.addStyleTag({content: '* { overflow-anchor: none !important; }'});
            await opener(page).click();
            await expect(page.getByTestId('fit-loading')).toBeVisible();
            await page.getByTestId('company-view-jobs').click();
            await scrollRested(page);
            const position = () => page.locator('#company-jobs-title').evaluate((el) => {
                const box = el.getBoundingClientRect();
                return {top: box.top, bottom: box.bottom, inner: innerHeight};
            });
            const before = await position();
            expect(before.top).toBeGreaterThanOrEqual(49);
            release();
            await expect(page.getByTestId('fit-ready')).toBeVisible();
            // While the answer is out the page is short, so the heading can only reach where the page ends; once
            // the answer lands the heading is brought to its scroll margin (4.5rem below the top), not left
            // wherever the grown page put it.
            await expect.poll(async () => {
                const after = await position();
                return after.top >= 49 && after.top <= 110 && after.bottom <= after.inner;
            }).toBe(true);
            await page.unroute('**/api/organizations/*/fit/');
        }
    });

    test('a reader who scrolled after the jump is not moved when the answer lands', async ({page}) => {
        await login(page);
        await page.setViewportSize({width: 375, height: 667});
        let release: () => void = () => {};
        const held = new Promise<void>((resolve) => { release = resolve; });
        await page.route('**/api/organizations/*/fit/', async (route) => {
            await held;
            await route.continue();
        });
        await page.goto('/');
        await opener(page).click();
        await expect(page.getByTestId('fit-loading')).toBeVisible();
        await page.getByTestId('company-view-jobs').click();
        await scrollRested(page);
        const jumpTop = await page.locator('#company-jobs-title').evaluate((el) => el.getBoundingClientRect().top);
        await page.mouse.move(180, 400);
        await page.mouse.wheel(0, 120);
        await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
        await scrollRested(page);
        const scrolled = await page.evaluate(() => window.scrollY);
        release();
        await expect(page.getByTestId('fit-ready')).toBeVisible();
        await scrollRested(page);
        // The page did not jump back to the heading: it is where the reader left it, give or take
        // the browser's own scroll anchoring for content that grew above it.
        const top = await page.locator('#company-jobs-title').evaluate((el) => el.getBoundingClientRect().top);
        expect(scrolled).toBeGreaterThan(0);
        expect(Math.abs(top - jumpTop)).toBeGreaterThan(40);
    });

    test('the disclosure under the numbers opens by keyboard and answers the two questions', async ({page}) => {
        await page.setViewportSize({width: 375, height: 667});
        await login(page);
        await page.goto('/');
        await opener(page).click();
        const legend = page.getByTestId('fit-legend');
        await expect(legend).toBeVisible();
        const summary = legend.locator('summary');
        expect((await summary.boundingBox())!.height).toBeGreaterThanOrEqual(44);
        await summary.focus();
        await page.keyboard.press('Enter');
        await expect(legend).toHaveAttribute('open', '');
        await expect(legend).toContainText('so it cannot reach 100');
        await expect(legend).toContainText('Why the fit and the rows can disagree');
        await expect(legend).toContainText('Sourced, not confirmed describes how a row was decided from it');
        await expect(legend).not.toContainText('Refresh matches');
        await expect(legend).toContainText('Sourced, not confirmed');
        await expectNoHorizontalOverflow(page);
    });

    test('a save of the priorities on a phone: Set your priorities, save, Back to the company shows the numbers; one request on load, one more per save', async ({page}) => {
        await page.setViewportSize({width: 375, height: 667});
        await login(page, PREFS_USER, E2E_PASSWORD);
        try {
            await setPriorities(page, null);
            const asked: string[] = [];
            page.on('request', (request) => {
                if (/\/api\/organizations\/\d+\/(fit|jobs)\//.test(request.url())) asked.push(request.url().replace(/^.*\/api\//, ''));
            });
            await page.goto('/');
            await opener(page).click();
            await expect(page.getByTestId('fit-no-priorities')).toBeVisible();
            // The jobs answer is the other read of the page: both have landed before the count.
            await expect(page.locator('[data-testid="jobs-list"], [data-testid="jobs-empty"], [data-testid="jobs-unavailable"]')).toBeVisible();
            // A plain page load: exactly one fit request and one jobs request.
            expect(asked.filter((url) => url.endsWith('/fit/')).length).toBe(1);
            expect(asked.filter((url) => url.endsWith('/jobs/')).length).toBe(1);
            await page.getByTestId('fit-set-priorities').click();
            const panel = page.getByTestId('assistant-panel');
            await expect(panel).toBeVisible();
            await panel.getByLabel('Public companies only').selectOption({index: 1});
            await panel.getByRole('button', {name: 'Review changes'}).click();
            await panel.getByRole('button', {name: /^Apply to account/}).first().click();
            await expect(panel).toContainText('Priorities saved');
            await panel.locator('.assistant-back-to-results').first().click();
            await expect(page.getByTestId('fit-ready')).toBeVisible();
            for (const id of ['fit-score', 'fit-company-score', 'fit-coverage']) await expect(page.getByTestId(id)).toBeVisible();
            for (const key of ['meets', 'tradeoffs', 'does-not-meet', 'unknown']) await expect(page.getByTestId(`fit-group-${key}`)).toBeVisible();
            await expect(page.getByTestId('fit-no-priorities')).toHaveCount(0);
            // One save, one more fit request, and never another read of the jobs.
            expect(asked.filter((url) => url.endsWith('/fit/')).length).toBe(2);
            expect(asked.filter((url) => url.endsWith('/jobs/')).length).toBe(1);
        } finally {
            await setPriorities(page, SEEDED);
        }
    });

    test('with the assistant docked, a save changes the coverage and the rows without a reload', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page, PREFS_USER, E2E_PASSWORD);
        try {
            await setPriorities(page, SEEDED);
            await page.goto('/');
            await page.locator('[data-testid="assistant-launcher"]').click();
            await opener(page).click();
            await expect(page.getByTestId('fit-ready')).toBeVisible();
            const rows = () => page.locator('[data-testid^="fit-row-"]').count();
            const before = {coverage: await page.getByTestId('fit-coverage').innerText(), rows: await rows()};
            const panel = page.getByTestId('assistant-panel');
            await expect(panel).toBeVisible();
            await panel.getByRole('button', {name: /Edit priorities|Add priorities/}).first().click();
            await panel.getByLabel('Public companies only').selectOption({index: 1});
            await panel.getByRole('button', {name: 'Review changes'}).click();
            await panel.getByRole('button', {name: /^Apply to account/}).first().click();
            await expect(panel).toContainText('Priorities saved');
            await expect.poll(rows).toBe(before.rows + 1);
            expect(await page.getByTestId('fit-coverage').innerText()).not.toBe(before.coverage);
            await expect(page.getByTestId('company-view')).toBeVisible();
        } finally {
            await setPriorities(page, SEEDED);
        }
    });
});

// Adversarial review round 1 and the screenshot review's round-3 findings (issue #486c).
test.describe('company view, review round 1 (issue #486c)', () => {
    test.beforeEach(() => {
        requireDjangoTier();
    });

    for (const size of [{width: 375, height: 667}, {width: 320, height: 640}]) {
        test(`${size.width}x${size.height}: after View jobs and a small scroll, the fit arriving does not push Open jobs away`, async ({page}) => {
            await page.setViewportSize(size);
            await login(page, PREFS_USER, E2E_PASSWORD);
            try {
                await setPriorities(page, SIX);
                let release: () => void = () => {};
                const held = new Promise<void>((resolve) => { release = resolve; });
                await page.route('**/api/organizations/*/fit/', async (route) => {
                    await held;
                    await route.continue();
                });
                await page.goto('/');
                await opener(page).click();
                await expect(page.getByTestId('fit-loading')).toBeVisible();
                await page.getByTestId('company-view-jobs').click();
                // The jump scrolls smoothly: wait for the heading to arrive below the top bar, then for rest.
                await expect.poll(() => page.locator('#company-jobs-title').evaluate((el) => el.getBoundingClientRect().top))
                    .toBeGreaterThan(49);
                await scrollRested(page);
                await page.mouse.move(size.width / 2, size.height / 2);
                const scrolledTo = await page.evaluate(() => window.scrollY);
                await page.mouse.wheel(0, 30);
                await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(scrolledTo + 20);
                await scrollRested(page);
                // The strip under the sticky top bar: the fit ends below the top of the window
                // but above the bottom of the bar, so the reader sees none of it.
                const strip = await page.evaluate(() => ({
                    fitBottom: document.querySelector('[data-testid="company-fit"]')!.getBoundingClientRect().bottom,
                    barBottom: document.querySelector('header.app-mobile-topbar')!.getBoundingClientRect().bottom,
                }));
                expect(strip.fitBottom, 'the fit ends in the strip under the bar').toBeGreaterThan(0);
                expect(strip.fitBottom).toBeLessThanOrEqual(strip.barBottom);
                const heading = page.locator('#company-jobs-title');
                const before = await heading.evaluate((el) => el.getBoundingClientRect().top);
                release();
                await expect(page.getByTestId('fit-ready')).toBeVisible();
                await scrollRested(page);
                const after = await heading.evaluate((el) => el.getBoundingClientRect().top);
                // Round 3 measured 718 px (375x667) and 782 px (320x640) here.
                expect(Math.abs(after - before)).toBeLessThanOrEqual(40);
            } finally {
                await page.unroute('**/api/organizations/*/fit/');
                await setPriorities(page, SEEDED);
            }
        });
    }

    test('a reader who scrolled past the jobs is not moved when a long list arrives; the pay reads in words', async ({page}) => {
        await page.setViewportSize({width: 375, height: 667});
        await login(page);
        const listings = Array.from({length: 25}, (_, n) => ({
            id: n + 1, title: `Role ${n + 1}`, location_text: 'Remote (US)', is_remote: true,
            compensation_min: '85000', compensation_max: '95000', compensation_currency: 'USD',
            compensation_interval: n % 2 ? 'PA' : 'per year', canonical_url: `https://jobs.example.test/${n + 1}`, status: 'active',
        }));
        let release: () => void = () => {};
        const held = new Promise<void>((resolve) => { release = resolve; });
        await page.route('**/api/organizations/*/jobs/', async (route) => {
            await held;
            await route.fulfill({json: {organization_id: 1, inventory: 'available', count: 25, results: listings}});
        });
        try {
            await page.goto('/');
            await opener(page).click();
            await expect(page.getByTestId('fit-ready')).toBeVisible();
            await expect(page.getByTestId('jobs-loading')).toBeVisible();
            // The browser's own scroll anchoring would hold a fully hidden section anyway; the case is
            // about the page's own correction.
            await page.addStyleTag({content: '* { overflow-anchor: none !important; }'});
            const evidence = page.getByTestId('field-evidence');
            await evidence.evaluate((el) => el.scrollIntoView({block: 'start', behavior: 'instant'}));
            await scrollRested(page);
            const jobsBottom = await page.getByTestId('company-jobs').evaluate((el) => el.getBoundingClientRect().bottom);
            expect(jobsBottom, 'the Jobs section is above the reader').toBeLessThanOrEqual(0);
            const top = () => evidence.evaluate((el) => el.getBoundingClientRect().top);
            const before = await top();
            release();
            await expect(page.getByTestId('jobs-list')).toBeVisible();
            await scrollRested(page);
            // Twenty-five rows are well over a screen tall; what the reader was reading stays put.
            expect(Math.abs((await top()) - before)).toBeLessThanOrEqual(40);
            const rows = page.getByTestId('job-row-1');
            await expect(rows).toContainText('USD 85,000–95,000 per year');
            await expect(page.getByTestId('job-row-2')).toContainText('USD 85,000–95,000 per year');
            await expect(page.getByTestId('jobs-list')).not.toContainText(/per PA|per per/);
        } finally {
            await page.unroute('**/api/organizations/*/jobs/');
        }
    });

    test('a reader who excluded this company is told where the exclusion applies; one with only that priority gets a sentence, not empty groups', async ({page}) => {
        await login(page, PREFS_USER, E2E_PASSWORD);
        try {
            await setPriorities(page, {set: {'exclusions.companies': [COMPANY]}});
            await page.goto('/');
            await opener(page).click();
            const sentence = page.getByTestId('fit-excluded');
            await expect(sentence).toBeVisible();
            // Opened from Company rankings, which never use the reader's priorities.
            await expect(sentence).toContainText(`You excluded ${COMPANY} in your priorities, so the assistant’s company matches leave it out.`);
            await expect(sentence).toContainText('Company rankings do not use your priorities.');
            await expect(sentence).not.toContainText('ranked results');
            await expect(page.getByTestId('fit-no-priorities')).toHaveCount(0);
            await expect(page.getByTestId('fit-exclusions-only')).toContainText('only exclusions');
            await expect(page.getByTestId('fit-ready')).toHaveCount(0);
            await expect(page.getByTestId('fit-coverage')).toHaveCount(0);
            await expect(page.getByTestId('fit-group-meets')).toHaveCount(0);
        } finally {
            await setPriorities(page, SEEDED);
        }
    });

    test('the Fit number is explained where it is read, and the explanation is true of this scoring', async ({page}) => {
        await login(page);
        await page.goto('/');
        await opener(page).click();
        const note = page.getByTestId('fit-number-note');
        await expect(note).toBeVisible();
        await expect(note).toHaveText('Fit is the ranking’s score, not a count of the rows below, and the two can disagree.');
        await expect(page.locator('.job-match-figure-label').nth(1)).toHaveText('Average rating');
        const legend = page.getByTestId('fit-legend');
        await legend.locator('summary').click();
        await expect(legend).toContainText('Only four of the ranking’s eight factors can score for a company');
        await expect(legend).not.toContainText(/industry|five of/i);
    });
});
