// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// E2E tests for the company view (issues #464, #486). The company opens as a
// view inside main#main-content, not as a blocking dialog: it must sit beside
// the navigation rail, leave the page scrollable and un-inert, add exactly one
// history entry, return to the results with Back, and be reachable by
// keyboard at both desktop and mobile widths. The correction and suggest
// dialogs stay blocking and keep their own isolation checks.
import {test, expect, Page} from '@playwright/test';

const POPUP_FIXTURE = '/e2e/fixtures/organization-popup.html';

const view = (page: Page) => page.getByTestId('company-view');

async function openCompanyView(page: Page): Promise<void> {
    // The rankings table is hidden at narrow widths; the mobile layout shows
    // cards instead. Pick whichever opener the current viewport renders.
    // Whichever is visible is resolved at each step rather than decided from
    // one read during page load, when neither layout has settled yet.
    const opener = page.locator('tr.organization-row:visible, .organization-card:visible').first();
    await expect(opener).toBeVisible();
    // Enter/Space on a company row opens the view (issue #464 validation).
    await opener.focus();
    await opener.press('Enter');
    await expect(view(page)).toBeVisible();
    await expect(page.locator('#organization-details-title')).toBeFocused();
}

const historyLength = (page: Page) => page.evaluate(() => window.history.length);
const companyParam = (page: Page) => page.evaluate(() => new URLSearchParams(window.location.search).get('company'));

// Boxes of the first visible results row or card, read in one evaluate so the
// scroll position and the rectangle belong to the same moment.
const anchorSnapshot = (page: Page, id = '1') => page.evaluate((organizationId) => {
    const element = Array.from(document.querySelectorAll<HTMLElement>(`[data-organization-id="${organizationId}"]`))
        .find((candidate) => candidate.getBoundingClientRect().height > 0);
    return {scrollY: Math.round(window.scrollY), top: element ? Math.round(element.getBoundingClientRect().top) : null};
}, id);

// Review r3: Bootstrap sets `:root { scroll-behavior: smooth }`, so
// window.scrollTo animates and an immediate exact read of window.scrollY
// catches a mid-animation offset — the tall-document sanity assertions
// failed in Firefox and WebKit (CI observed 0, 2, and 32 instead of 300
// across retries) before the dialog was even opened. Disable smooth
// scrolling on the test page — a page.evaluate scrollTo with instant
// behavior — and wait for the scroll to settle before reading it, so the
// setup is deterministic in chromium, firefox, and webkit.
async function scrollWindowTo(page: Page, top: number, message?: string): Promise<void> {
    await page.evaluate(() => {
        document.documentElement.style.scrollBehavior = 'auto';
    });
    await page.evaluate((y) => window.scrollTo(0, y), top);
    await expect.poll(
        () => page.evaluate(() => window.scrollY),
        {message: message ?? `window scroll must settle at ${top}`},
    ).toBe(top);
}

const LONG_SOURCE = 'https://a-very-long-source-domain-name-that-keeps-going-and-going-for-overflow-checks.example.com/careers/jobs/2026/remote-first-policy-announcement-with-an-extremely-long-path';
const LONG_VALUE = 'Hybrid three days in office with an extremely long qualifying clause that must wrap rather than overflow the narrow dialog at three hundred twenty pixels';

const iso = (daysAgo: number) => new Date(Date.now() - daysAgo * 86400000).toISOString();

function provenanceStub(kind: 'populated' | 'stale' | 'pending' | 'long') {
    const field = (key: string, value: string, extra: Record<string, unknown> = {}) => ({
        field_key: key, value, source_domain: 'acme.example', source_url: 'https://acme.example/careers',
        observed_at: iso(3), scope: {}, last_checked_at: iso(2), last_successful_fetch_at: iso(2),
        last_changed_at: iso(30), last_verified_at: iso(2), stale: false, status: 'verified', review: 'none',
        agrees_with_displayed: true, policy_days: 90, ...extra,
    });
    const base = {
        organization_id: 1, organization_modified: iso(1), organization_created: iso(400), latest_observation: null,
        evidence_schema: 2, unverified_fields: [], pending_review: [] as unknown[],
        displayed_values: {rto_policy: 'Hybrid', accelerated_vesting: 'No'},
        summary: {verified: 2, stale: 0, unknown: 5, total: 7, fact_coverage: 2, last_verified_at: iso(2), pending_review: 0},
    };
    if (kind === 'stale') {
        return {...base, fields: [field('rto_policy', 'Hybrid', {stale: true, status: 'stale', last_verified_at: iso(200)})],
            summary: {...base.summary, verified: 0, stale: 1, fact_coverage: 1, last_verified_at: iso(200)}};
    }
    if (kind === 'pending') {
        const claim = (value: string, domain: string, review: string, days: number) =>
            ({field_key: 'rto_policy', review, observed_at: iso(days), source_domain: domain, observed_value: value});
        return {...base, fields: [field('rto_policy', 'Hybrid')],
            pending_review: [claim('Remote', 'one.example', 'pending', 1), claim('On-site', 'two.example', 'conflicted', 2)],
            summary: {...base.summary, pending_review: 2}};
    }
    if (kind === 'long') {
        return {...base, fields: [field('rto_policy', LONG_VALUE, {source_domain: LONG_SOURCE.replace('https://', '').split('/')[0], source_url: LONG_SOURCE})],
            pending_review: [{field_key: 'rto_policy', review: 'pending', observed_at: iso(1), source_domain: 'x.example', observed_value: LONG_VALUE}]};
    }
    return {...base, fields: [field('rto_policy', 'Hybrid'), field('accelerated_vesting', 'true')]};
}

async function stubProvenance(page: Page, kind: 'populated' | 'stale' | 'pending' | 'long'): Promise<void> {
    await page.route('**/api/organizations/*/provenance/', route =>
        route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(provenanceStub(kind))}));
}

test.beforeEach(async ({page}) => {
    await stubProvenance(page, 'populated');
});

test.describe('company view in the main workspace (issues #464, #486) — desktop', () => {
    test.use({viewport: {width: 1280, height: 800}});

    test('the heading and Back to results are hit targets inside the main content, beside the nav rail', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openCompanyView(page);

        const rail = page.locator('[data-nav-rail]');
        await expect(rail).toBeVisible();
        const railBox = (await rail.boundingBox())!;
        const viewport = page.viewportSize()!;
        const targets = [
            {name: 'heading', locator: page.locator('#organization-details-title')},
            {name: 'Back to results', locator: view(page).getByRole('button', {name: 'Back to results'})},
        ] as const;
        for (const {name, locator} of targets) {
            const box = await locator.boundingBox();
            expect(box, `${name} must be visible`).not.toBeNull();
            // Beside the rail, never under it, and inside the viewport.
            expect(box!.x, `${name} must start to the right of the nav rail`).toBeGreaterThanOrEqual(railBox.x + railBox.width - 1);
            expect(box!.x + box!.width, `${name} inside viewport`).toBeLessThanOrEqual(viewport.width);
            expect(box!.y + box!.height / 2, `${name} inside viewport`).toBeLessThanOrEqual(viewport.height);

            const hit = await page.evaluate(([x, y]) => {
                const el = document.elementFromPoint(x, y);
                return {inView: Boolean(el && el.closest('[data-testid="company-view"]')), inRail: Boolean(el && el.closest('[data-nav-rail]'))};
            }, [box!.x + box!.width / 2, box!.y + box!.height / 2]);
            expect(hit, `${name} hit target resolves inside the view, not the rail`).toEqual({inView: true, inRail: false});
        }
    });

    test('the view is an in-flow region of main#main-content: no dialog, no overlay, no fixed positioning', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openCompanyView(page);

        const region = view(page);
        await expect(page.locator('main#main-content').getByTestId('company-view')).toBeVisible();
        await expect(page.getByRole('region', {name: 'Acme Robotics'})).toHaveCount(1);
        await expect(page.getByRole('dialog')).toHaveCount(0);
        await expect(page.locator('.popup-overlay')).toHaveCount(0);
        await expect(page.locator('[aria-modal]')).toHaveCount(0);
        const styles = await region.evaluate((el) => {
            const style = getComputedStyle(el);
            return {position: style.position, maxHeight: style.maxHeight, overflowY: style.overflowY};
        });
        expect(styles.position).toBe('static');
        expect(styles.maxHeight).toBe('none');
        expect(styles.overflowY).toBe('visible');
        // The results are hidden, not gone: search, filters and page stay.
        await expect(page.getByTestId('organization-results-region')).toBeHidden();
        await expect(page.getByTestId('organization-results-region')).toHaveCount(1);
    });

    test('the page behind keeps working: not inert, not scroll-locked, the document scrolls and focus can leave the view', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        // The fixture is exactly viewport-high: make the document provably tall.
        await page.evaluate(() => {
            const filler = document.createElement('div');
            filler.setAttribute('data-testid', 'document-filler');
            filler.setAttribute('aria-hidden', 'true');
            filler.style.cssText = 'height: 2400px;';
            document.querySelector('main.app-content')!.appendChild(filler);
        });
        const overflowBefore = await page.evaluate(() => ({
            body: getComputedStyle(document.body).overflow,
            root: getComputedStyle(document.documentElement).overflow,
        }));
        await openCompanyView(page);

        const state = await page.evaluate(() => {
            const roots = Array.from(document.querySelectorAll<HTMLElement>('.app-shell, main.app-content, .skip-to-content'));
            return {
                count: roots.length,
                inert: roots.filter((el) => el.hasAttribute('inert')).length,
                ariaHidden: roots.filter((el) => el.hasAttribute('aria-hidden')).length,
                overflow: {body: getComputedStyle(document.body).overflow, root: getComputedStyle(document.documentElement).overflow},
            };
        });
        expect(state.count).toBeGreaterThanOrEqual(3);
        expect(state.inert).toBe(0);
        expect(state.ariaHidden).toBe(0);
        expect(state.overflow).toEqual(overflowBefore);

        // Wheel input moves the document.
        await page.evaluate(() => {
            document.documentElement.style.scrollBehavior = 'auto';
        });
        await page.mouse.move(700, 400);
        await page.mouse.wheel(0, 600);
        await expect.poll(() => page.evaluate(() => window.scrollY), {message: 'the document scrolls while the view is open'})
            .toBeGreaterThan(0);

        // The navigation behind it can take focus.
        await page.evaluate(() => document.getElementById('nav-rankings')?.focus());
        await expect(page.locator('#nav-rankings')).toBeFocused();
    });

    test('Tab from the heading reaches Back to results first; Escape does nothing', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openCompanyView(page);

        await page.keyboard.press('Escape');
        await expect(view(page)).toBeVisible();
        await expect(page.locator('#organization-details-title')).toBeFocused();

        await page.keyboard.press('Tab');
        await expect(view(page).getByRole('button', {name: 'Back to results'})).toBeFocused();
        // Close button, overlay and focus trap are gone with the dialog.
        await expect(view(page).getByRole('button', {name: 'Close'})).toHaveCount(0);
        await page.keyboard.press('Tab');
        const next = await page.evaluate(() => document.activeElement?.closest('[data-testid="company-view"]') !== null);
        expect(next, 'the next stop is still inside the view, in document order').toBe(true);
    });

    test('opening a row adds one history entry; Back returns to the results with the row focused and in place, Forward reopens', async ({page}) => {
        await page.goto(`${POPUP_FIXTURE}?search=a`);
        await page.evaluate(() => {
            document.documentElement.style.scrollBehavior = 'auto';
            const filler = document.createElement('div');
            filler.setAttribute('aria-hidden', 'true');
            filler.style.cssText = 'height: 2400px;';
            document.querySelector('main.app-content')!.appendChild(filler);
        });
        const row = page.locator('tr.organization-row:visible').nth(2);
        await row.scrollIntoViewIfNeeded();
        await page.evaluate(() => window.scrollBy(0, 40));
        const before = await anchorSnapshot(page, '3');
        const lengthBefore = await historyLength(page);

        await row.focus();
        await row.press('Enter');
        await expect(view(page)).toBeVisible();
        await expect.poll(() => companyParam(page)).toBe('3');
        expect(await historyLength(page)).toBe(lengthBefore + 1);

        await page.goBack();
        await expect(view(page)).toHaveCount(0);
        await expect.poll(() => companyParam(page)).toBeNull();
        expect(new URL(page.url()).searchParams.get('search')).toBe('a');
        await expect(row).toBeFocused();
        await expect.poll(async () => (await anchorSnapshot(page, '3')).top, {message: 'the row is back at the same viewport offset'})
            .toBeGreaterThanOrEqual(before.top! - 2);
        const after = await anchorSnapshot(page, '3');
        expect(Math.abs(after.top! - before.top!)).toBeLessThanOrEqual(2);

        await page.goForward();
        await expect(view(page)).toBeVisible();
        await expect.poll(() => companyParam(page)).toBe('3');
    });

    test('the Back to results button does the same as the browser Back, once', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        const lengthBefore = await historyLength(page);
        await openCompanyView(page);

        await view(page).getByRole('button', {name: 'Back to results'}).click();

        await expect(view(page)).toHaveCount(0);
        await expect.poll(() => companyParam(page)).toBeNull();
        await expect(page.locator('tr.organization-row').first()).toBeFocused();
        // Back moved within the entries the open added: forward is still possible.
        await page.goForward();
        await expect(view(page)).toBeVisible();
        expect(await historyLength(page)).toBe(lengthBefore + 1);
    });

    test('Suggest correction opens the blocking correction dialog over the view; the view stays and focus returns to the trigger', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openCompanyView(page);
        const trigger = view(page).getByTestId('suggest-correction-link');
        await trigger.focus();
        await trigger.press('Enter');

        const form = page.getByRole('dialog', {name: /Suggest a correction/});
        await expect(form).toBeVisible();
        // Exactly one modal at any time, and the view is still in the page under it.
        await expect(page.locator('[aria-modal="true"]')).toHaveCount(1);
        await expect(view(page)).toHaveCount(1);

        await page.keyboard.press('Escape');
        await expect(form).toHaveCount(0);
        await expect(view(page)).toBeVisible();
        await expect(trigger).toBeFocused();
    });

    test('the suggest modal opens from the results and Escape returns focus to the button that opened it', async ({page}) => {
        // Real-browser assertion of the #464 focus-restore contract on the
        // suggest modal: the opener must be captured BEFORE lockBackground()
        // inerts the shell, because inerting blurs the trigger and resets
        // activeElement to <body>.
        await page.goto(POPUP_FIXTURE);
        // The results, including Suggest a company, are hidden while a view is open.
        await openCompanyView(page);
        await expect(page.getByTestId('suggest-company-btn')).toBeHidden();
        await view(page).getByRole('button', {name: 'Back to results'}).click();
        await expect(view(page)).toHaveCount(0);

        await page.getByTestId('suggest-company-btn').click();
        await expect(page.getByTestId('suggest-company-modal')).toBeVisible();
        const isolation = await page.evaluate(() => ({
            anyNotInert: Array.from(document.querySelectorAll<HTMLElement>('.app-shell, main.app-content, .skip-to-content'))
                .some((el) => !el.hasAttribute('inert')),
            rootOverflow: getComputedStyle(document.documentElement).overflow,
        }));
        expect(isolation.anyNotInert).toBe(false);
        expect(isolation.rootOverflow).toBe('hidden');
        await page.keyboard.press('Escape');
        await expect(page.getByTestId('suggest-company-modal')).toHaveCount(0);
        await expect(page.getByTestId('suggest-company-btn')).toBeFocused();
    });

    test('a company that is no longer listed says so, with Back to results and no evidence request', async ({page}) => {
        let evidenceRequests = 0;
        await page.route('**/api/organizations/*/provenance/', route => {
            evidenceRequests += 1;
            return route.fulfill({status: 404, body: '{}'});
        });
        await page.route('**/api/organizations/99/', route => route.fulfill({status: 404, contentType: 'application/json', body: '{}'}));
        await page.goto(`${POPUP_FIXTURE}?company=99`);

        await expect(page.getByTestId('company-not-found')).toHaveText('This company is no longer listed.');
        await expect(page.locator('#organization-details-title')).toBeFocused();
        expect(evidenceRequests).toBe(0);
        await view(page).getByRole('button', {name: 'Back to results'}).click();
        await expect(view(page)).toHaveCount(0);
        await expect.poll(() => companyParam(page)).toBeNull();
        await expect(page.locator('tr.organization-row').first()).toBeVisible();
    });
});

test.describe('company view in the main workspace (issues #464, #486) — mobile', () => {
    test.use({viewport: {width: 375, height: 667}, isMobile: true, hasTouch: true});

    test('heading and Back to results are fully visible in the first screen and at least 44px tall', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openCompanyView(page);

        const viewport = page.viewportSize()!;
        const titleBox = await page.locator('#organization-details-title').boundingBox();
        const backBox = await view(page).getByRole('button', {name: 'Back to results'}).boundingBox();

        for (const [name, box] of [['title', titleBox], ['Back to results', backBox]] as const) {
            expect(box, `${name} must be visible`).not.toBeNull();
            expect(box!.x, `${name} must start inside the viewport`).toBeGreaterThanOrEqual(0);
            expect(box!.x + box!.width, `${name} must end inside the viewport`).toBeLessThanOrEqual(viewport.width);
            expect(box!.y, `${name} must start inside the viewport`).toBeGreaterThanOrEqual(0);
            expect(box!.y + box!.height, `${name} must end inside the first screen`).toBeLessThanOrEqual(viewport.height);
        }
        expect(backBox!.height).toBeGreaterThanOrEqual(44);
    });

    test('the view opens via keyboard and Back to results returns focus to the opener card', async ({page}) => {
        await page.goto(POPUP_FIXTURE);

        // Mobile view renders cards instead of the table.
        const card = page.locator('.organization-card').first();
        await expect(card).toBeVisible();
        await card.focus();
        await card.press('Enter');
        await expect(view(page)).toBeVisible();

        await view(page).getByRole('button', {name: 'Back to results'}).click();
        await expect(view(page)).toHaveCount(0);
        await expect(page.locator('.organization-card').first()).toBeFocused();
    });

    test('nothing is inert or scroll-locked and the document scrolls at mobile width', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await page.evaluate(() => {
            const filler = document.createElement('div');
            filler.setAttribute('data-testid', 'document-filler');
            filler.setAttribute('aria-hidden', 'true');
            filler.style.cssText = 'height: 2400px;';
            document.querySelector('main.app-content')!.appendChild(filler);
        });
        const overflowBefore = await page.evaluate(() => ({
            body: getComputedStyle(document.body).overflow,
            root: getComputedStyle(document.documentElement).overflow,
        }));
        await openCompanyView(page);

        const state = await page.evaluate(() => ({
            inert: document.querySelectorAll('[inert]').length,
            ariaHidden: document.querySelectorAll('.app-shell[aria-hidden], main.app-content[aria-hidden], .skip-to-content[aria-hidden]').length,
            overflow: {body: getComputedStyle(document.body).overflow, root: getComputedStyle(document.documentElement).overflow},
        }));
        expect(state.inert).toBe(0);
        expect(state.ariaHidden).toBe(0);
        expect(state.overflow).toEqual(overflowBefore);
        await scrollWindowTo(page, 150, 'the document scrolls while the view is open');
    });
});

test.describe('evidence states in the company view (#473a, #486, fixture tier)', () => {
    test.use({viewport: {width: 1280, height: 800}});

    test('populated state shows rating coverage and verified facts', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openCompanyView(page);
        await expect(view(page).getByTestId('rating-coverage')).toContainText('5 of 5');
        await expect(view(page).getByTestId('coverage-summary')).toBeVisible();
    });

    test('stale state labels the fact as stale', async ({page}) => {
        await stubProvenance(page, 'stale');
        await page.goto(POPUP_FIXTURE);
        await openCompanyView(page);
        await expect(view(page).getByTestId('field-stale-rto_policy')).toBeVisible();
    });

    test('pending state lists every open claim with its own source', async ({page}) => {
        await stubProvenance(page, 'pending');
        await page.goto(POPUP_FIXTURE);
        await openCompanyView(page);
        await expect(view(page)).toContainText('Remote');
        await expect(view(page)).toContainText('one.example');
        await expect(view(page)).toContainText('On-site');
        await expect(view(page)).toContainText('two.example');
    });

    test('the failure state keeps rating coverage visible', async ({page}) => {
        await page.route('**/api/organizations/*/provenance/', route => route.fulfill({status: 500, body: 'no'}));
        await page.goto(POPUP_FIXTURE);
        await openCompanyView(page);
        await expect(view(page).getByTestId('rating-coverage')).toContainText('of');
    });

    const describedNodes = async (page: Page, roles: string[]) => {
        const client = await page.context().newCDPSession(page);
        const {nodes} = await client.send('Accessibility.getFullAXTree');
        return nodes.filter((n: any) => roles.includes(n.role?.value))
            .filter((n: any) => /Rating coverage \d+ of \d+/.test(n.description?.value ?? ''));
    };

    test('table rows expose their evidence summary as the accessible description', async ({page, browserName}) => {
        test.skip(browserName !== 'chromium', 'CDP accessibility tree is Chromium-only');
        await page.goto(POPUP_FIXTURE);
        await expect(page.locator('tr.organization-row').first()).toHaveAttribute('aria-describedby', /organization-summary-\d+/);
        await expect(page.locator('.organization-cards')).toBeHidden();
        expect((await describedNodes(page, ['row', 'button'])).length).toBeGreaterThan(0);
    });

    test('cards expose their evidence summary as the accessible description', async ({page, browserName}) => {
        test.skip(browserName !== 'chromium', 'CDP accessibility tree is Chromium-only');
        await page.setViewportSize({width: 375, height: 800});
        await page.goto(POPUP_FIXTURE);
        const card = page.locator('.organization-card').first();
        await expect(card).toBeVisible();
        await expect(card).toHaveAttribute('aria-describedby', /organization-card-summary-\d+/);
        await expect(page.locator('tr.organization-row').first()).toBeHidden();
        // Only cards are rendered at this width, so every described node is a card.
        const described = await describedNodes(page, ['button']);
        const cards = await page.locator('.organization-card').count();
        expect(cards).toBeGreaterThan(0);
        expect(described.length).toBe(cards);
    });
});

test.describe('evidence states at 320px (#473a, #486, fixture tier)', () => {
    test.use({viewport: {width: 320, height: 640}});

    test('long source and value text wraps without horizontal overflow', async ({page}) => {
        await stubProvenance(page, 'long');
        await page.goto(POPUP_FIXTURE);
        await openCompanyView(page);
        await expect(view(page)).toContainText('extremely long qualifying clause');
        const overflow = await page.evaluate(() => {
            const region = document.querySelector('[data-testid="company-view"]') as HTMLElement;
            return {
                view: region.scrollWidth - region.clientWidth,
                page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            };
        });
        expect(overflow.view).toBeLessThanOrEqual(1);
        expect(overflow.page).toBeLessThanOrEqual(1);
    });
});
