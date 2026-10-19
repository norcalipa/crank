// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Viewport regression harness for the job-search chat (issue #431). Replaces the
// manual-only viewport/zoom/keyboard/IME verification recorded in
// docs/rollout-gates.md §4 with automated cross-browser assertions.
import {test, expect, Page} from '@playwright/test';
import {CONFIRM_ARM_DELAY_MS} from '../static/js/chat/confirmArmDelay';
import {
    expectPressOnConfirm, expectStartedInside, inside, mouseDown, mousePress, readPressGaps, recordPresses, trackDeletes,
    waitForConfirmArmed, waitUntil,
} from './django/support';

const CHAT_FIXTURE = '/e2e/fixtures/job-search-chat.html';
const PANEL_CHAT_FIXTURE = '/e2e/fixtures/job-search-chat-panel.html';
const ORG_FIXTURE = '/e2e/fixtures/organization-list.html';

interface ConversationFixture {
    id: number;
    active: boolean;
    created: string | null;
    modified: string | null;
    messages: Array<Record<string, unknown>>;
    preferences_changed: boolean;
}

const emptyConversation: ConversationFixture = {
    id: 1,
    active: true,
    created: '2026-08-20T00:00:00Z',
    modified: '2026-08-20T00:00:00Z',
    messages: [],
    preferences_changed: false,
};

const populatedConversation: ConversationFixture = {
    id: 1,
    active: true,
    created: '2026-08-20T00:00:00Z',
    modified: '2026-08-20T00:00:00Z',
    messages: [
        {
            id: 11,
            role: 'user',
            content: 'I want a remote-friendly Series B company in health tech.',
            preferences_changed: false,
            created: '2026-08-20T00:01:00Z',
            results: null,
        },
        {
            id: 12,
            role: 'assistant',
            content: 'Here are some matches based on your preferences.',
            preferences_changed: false,
            created: '2026-08-20T00:01:01Z',
            results: {
                jobs: [
                    {
                        id: 101,
                        title: 'Senior Frontend Engineer',
                        organization_name: 'Beacon Health',
                        location: 'Remote (US)',
                        remote: true,
                        compensation: {min: 160000, max: 210000, currency: 'USD', interval: 'year'},
                        canonical_url: 'https://beacon.example/jobs/101',
                        observed_at: '2026-08-19T00:00:00Z',
                        updated_at: '2026-08-19T00:00:00Z',
                    },
                ],
                organizations: [
                    {id: 2, name: 'Beacon Health', url: 'https://beacon.example', funding_round: 'B', rto_policy: 'R'},
                    {id: 1, name: 'Acme Robotics', url: 'https://acme.example', funding_round: 'C', rto_policy: 'H'},
                ],
            },
        },
    ],
    preferences_changed: false,
};

const longConversation: ConversationFixture = {
    ...populatedConversation,
    messages: Array.from({length: 24}, (_, i) => ({
        id: 200 + i,
        role: i % 2 === 0 ? 'user' : 'assistant',
        content: `Message ${i + 1}: a line of conversation long enough to wrap onto more than one row on a phone.`,
        preferences_changed: false,
        created: '2026-08-20T00:01:00Z',
        results: null,
    })),
};

/**
 * Stub the job-search chat API so the component runs without a Django backend.
 * `scenario` selects the resume shape: `empty` (no history) or `populated`.
 */
async function mockJobSearchApi(page: Page, scenario: 'empty' | 'populated' | 'long'): Promise<void> {
    await page.route('**/api/agent/conversations/**', async (route) => {
        const request = route.request();
        const method = request.method();
        const pathname = new URL(request.url()).pathname;

        if (method === 'GET' && pathname === '/api/agent/assistant-status/') {
            // Advisory assistant-status endpoint (issue #457): the fixture
            // renders the healthy baseline; per-state scenarios stub this route
            // explicitly.
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({state: 'ready', actions: [], checked_at: '2026-08-20T00:00:00Z'}),
            });
            return;
        }

        if (method === 'GET' && pathname === '/api/agent/conversations/') {
            if (scenario === 'long') {
                await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(longConversation)});
            } else if (scenario === 'populated') {
                await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(populatedConversation)});
            } else {
                // No existing conversation -> the component will POST to create one.
                await route.fulfill({status: 404, contentType: 'application/json', body: '{}'});
            }
            return;
        }

        if (method === 'POST' && pathname === '/api/agent/conversations/') {
            await route.fulfill({status: 201, contentType: 'application/json', body: JSON.stringify(emptyConversation)});
            return;
        }

        if (method === 'POST' && /^\/api\/agent\/conversations\/\d+\/$/.test(pathname)) {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    message: {
                        id: 99,
                        role: 'assistant',
                        content: 'Thanks — I found a few matches for you.',
                        preferences_changed: false,
                        created: '2026-08-20T00:02:00Z',
                        results: null,
                    },
                    preferences_changed: false,
                }),
            });
            return;
        }

        await route.fulfill({status: 200, contentType: 'application/json', body: '{}'});
    });
}

async function mockOrganizationListApi(page: Page): Promise<void> {
    await page.route('**/api/funding-round-choices/**', (route) =>
        route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({S: 'Seed', A: 'Series A', B: 'Series B', C: 'Series C', D: 'Series D', E: 'Series E', F: 'Series F'}),
        }),
    );
    await page.route('**/api/rto-policy-choices/**', (route) =>
        route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({R: 'Remote', H: 'Hybrid', O: 'In-Office'}),
        }),
    );
}

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
    const dims = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
    }));
    expect(dims.scrollWidth, 'document must not scroll horizontally').toBeLessThanOrEqual(dims.clientWidth + 1);
}

async function expectComposerUsable(page: Page): Promise<void> {
    const composer = page.locator('textarea[aria-label="Message"]');
    await expect(composer).toBeVisible();
    await expect(composer).toBeEnabled();
    await expect(page.locator('button[aria-label="Send message"]')).toBeVisible();
    const box = await composer.boundingBox();
    expect(box, 'composer should have a bounding box').not.toBeNull();
    const viewport = page.viewportSize();
    expect(box!.y + box!.height, 'composer should not be buried below the fold').toBeLessThanOrEqual(viewport!.height + 1);
}

const viewports: Array<{name: string; width: number; height: number; isMobile?: boolean; hasTouch?: boolean}> = [
    {name: 'desktop', width: 1280, height: 800},
    {name: 'tablet', width: 768, height: 1024},
    {name: 'mobile', width: 375, height: 667, isMobile: true, hasTouch: true},
    {name: 'short-height', width: 375, height: 560, isMobile: true, hasTouch: true},
];

for (const vp of viewports) {
    test.describe(`viewport ${vp.name} (${vp.width}x${vp.height})`, () => {
        test.use({viewport: {width: vp.width, height: vp.height}, isMobile: vp.isMobile, hasTouch: vp.hasTouch});

        test('chat shell has no horizontal overflow and the composer stays usable', async ({page}) => {
            await mockJobSearchApi(page, 'empty');
            await page.goto(CHAT_FIXTURE);
            await expectComposerUsable(page);
            await expectNoHorizontalOverflow(page);
        });

        test('populated history renders job + organization results without overflow', async ({page}) => {
            await mockJobSearchApi(page, 'populated');
            await page.goto(CHAT_FIXTURE);
            await expect(page.locator('[data-testid="result-cards"]')).toBeVisible();
            await expect(page.getByText('Beacon Health', {exact: true}).first()).toBeVisible();
            await expect(page.getByText('Senior Frontend Engineer')).toBeVisible();
            await expectNoHorizontalOverflow(page);
        });

        test('organization list shows no inner vertical scrollbar', async ({page}) => {
            await mockOrganizationListApi(page);
            await page.goto(ORG_FIXTURE);
            const wrap = page.locator('.organization-table-wrap');
            const cards = page.locator('.organization-cards');
            // The table-to-card switch follows the results container width
            // (issues #478, #473): cards when it is at most 56rem (896px) wide.
            // The width and the layout it implies are read together and
            // retried: a width read while the page is still laying out (seen
            // in WebKit) must not be paired with the settled layout.
            await expect(async () => {
                const resultsWidth = await page.locator('.organization-results').evaluate((el) => el.getBoundingClientRect().width);
                if (resultsWidth <= 896) {
                    // Narrow results: cards are shown, table is hidden.
                    await expect(cards).toBeVisible({timeout: 500});
                    await expect(wrap).toBeHidden({timeout: 500});
                } else {
                    // Wide results: table is shown and the wrapper owns horizontal
                    // scrolling with no inner vertical scrollbar (overflow-y hidden).
                    await expect(wrap).toBeVisible({timeout: 500});
                    await expect(cards).toBeHidden({timeout: 500});
                    const overflowY = await wrap.evaluate((el) => getComputedStyle(el).overflowY);
                    expect(overflowY, 'organization list must not introduce an inner vertical scrollbar').toBe('hidden');
                }
            }).toPass({timeout: 10_000});
            await expectNoHorizontalOverflow(page);
        });
    });
}

test.describe('assistant availability layout (#457)', () => {
    // Scroll-yield contract (visual round 2, coordinated with #496): the card
    // body never scrolls; the history is the only yielding region and the
    // availability notice + composer stay in the non-scrolling bottom stack,
    // fully visible on initial load at every supported viewport.
    const unavailableScenarios = [
        {name: 'replies-disabled', status: {state: 'replies_disabled', actions: ['browse_rankings']}, gated: true},
        {name: 'inventory-unavailable', status: {state: 'inventory_unavailable', actions: ['browse_rankings']}, gated: true},
        // Transiently unavailable is retryable but not gated: sending stays possible.
        {name: 'temporarily-unavailable', status: {state: 'temporarily_unavailable', actions: ['retry']}, gated: false},
    ];

    for (const vp of viewports.filter((v) => ['desktop', 'mobile'].includes(v.name))) {
        test.describe(`layout ${vp.name} (${vp.width}x${vp.height})`, () => {
            test.use({viewport: {width: vp.width, height: vp.height}, isMobile: vp.isMobile, hasTouch: vp.hasTouch});

            for (const scenario of unavailableScenarios) {
                test(`${scenario.name}: notice, action, and composer visible without scrolling the card body`, async ({page}) => {
                    await mockJobSearchApi(page, 'empty');
                    // Registered after mockJobSearchApi so this handler wins for
                    // the assistant-status route in this scenario.
                    await page.route('**/api/agent/assistant-status/', (route) =>
                        route.fulfill({
                            status: 200,
                            contentType: 'application/json',
                            body: JSON.stringify({...scenario.status, checked_at: '2026-08-20T00:00:00Z'}),
                        }),
                    );
                    await page.goto(CHAT_FIXTURE);

                    const notice = page.locator('[data-testid="assistant-status-notice"]');
                    await expect(notice).toBeVisible();
                    await expect(notice.locator('a, button').first()).toBeVisible();

                    // Wait for deterministic layout: the chat has measured (it
                    // publishes its notice cap) and web fonts have finished loading.
                    await page.waitForFunction(() => {
                        const card = document.getElementById('job-search-chat')?.firstElementChild as HTMLElement | null;
                        return !!card && card.style.getPropertyValue('--chat-notices-max') !== '' && document.fonts.status === 'loaded';
                    });
                    await page.evaluate(() => new Promise<void>((resolve) => {
                        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
                    }));

                    const layout = await page.evaluate(() => {
                        const cardBody = document.querySelector('#job-search-chat .card-body') as HTMLElement;
                        const panelBody = document.querySelector('.assistant-panel-body') as HTMLElement;
                        const history = document.querySelector('[role="log"][aria-label="Message history"]') as HTMLElement;
                        const noticeEl = document.querySelector('[data-testid="assistant-status-notice"]') as HTMLElement;
                        const composer = document.querySelector('textarea[aria-label="Message"]') as HTMLElement;
                        const viewportHeight = window.innerHeight;
                        const withinViewport = (el: HTMLElement) => {
                            const r = el.getBoundingClientRect();
                            return r.top >= -1 && r.bottom <= viewportHeight + 1;
                        };
                        return {
                            // The chat mounts in the assistant panel (issue #483): the panel
                            // body is what would scroll if the notice pushed the composer out.
                            // (On the sheet the composer band reaches into the panel's padding
                            // on purpose, so the card body's own scrollHeight says nothing.)
                            panelBodyScrolls: panelBody.scrollHeight > panelBody.clientHeight + 1,
                            cardBodyOverflowY: getComputedStyle(cardBody).overflowY,
                            historyOverflowY: getComputedStyle(history).overflowY,
                            noticeWithinViewport: withinViewport(noticeEl),
                            composerWithinViewport: withinViewport(composer),
                        };
                    });

                    expect(layout.cardBodyOverflowY, 'card body must not become a scroll container').toBe('visible');
                    expect(layout.panelBodyScrolls, 'the panel body must not scroll; history yields instead').toBe(false);
                    expect(layout.historyOverflowY, 'history must be the sole scroll region').toBe('auto');
                    expect(layout.noticeWithinViewport, 'complete notice must be visible on initial load').toBe(true);
                    expect(layout.composerWithinViewport, 'composer must be visible on initial load').toBe(true);

                    // The advisory status gates futile states on purpose: the
                    // composer stays visible but disabled, and never below the
                    // fold. Transient states keep the composer enabled.
                    const composer = page.locator('textarea[aria-label="Message"]');
                    await expect(composer).toBeVisible();
                    if (scenario.gated) {
                        await expect(composer).toBeDisabled();
                        await expect(page.locator('button[aria-label="Send message"]')).toBeDisabled();
                    } else {
                        await expect(composer).toBeEnabled();
                    }
                    const box = await composer.boundingBox();
                    expect(box, 'composer should have a bounding box').not.toBeNull();
                    expect(box!.y + box!.height, 'composer should not be buried below the fold').toBeLessThanOrEqual(vp.height + 1);
                    await expectNoHorizontalOverflow(page);
                });
            }
        });
    }
});

test.describe('empty state', () => {
    test('empty history shows guidance text when there are no messages', async ({page}) => {
        await mockJobSearchApi(page, 'empty');
        await page.goto(CHAT_FIXTURE);
        await expect(page.locator('[data-testid="empty-history"]')).toBeVisible();
        await expect(page.getByText('Ask about compensation, work location, funding, or culture to get started.')).toBeVisible();
    });
});

test.describe('composer behavior', () => {
    test('submits on Enter and grows for multi-line input without horizontal overflow', async ({page}) => {
        await mockJobSearchApi(page, 'empty');
        await page.goto(CHAT_FIXTURE);
        await expectComposerUsable(page);

        const composer = page.locator('textarea[aria-label="Message"]');
        await composer.fill('Find me a hybrid Series B company');
        await composer.press('Enter');

        const userMessage = page.locator('article[aria-label="Your message"]');
        await expect(userMessage).toHaveCount(1);
        await expect(userMessage).toContainText('Find me a hybrid Series B company');
        await expectNoHorizontalOverflow(page);
    });

    test('does not submit during IME composition (Enter with isComposing)', async ({page}) => {
        await mockJobSearchApi(page, 'empty');
        await page.goto(CHAT_FIXTURE);
        await expectComposerUsable(page);

        const composer = page.locator('textarea[aria-label="Message"]');
        await composer.fill('你好');

        // Simulate an in-progress IME composition: Enter should be swallowed.
        await page.evaluate(() => {
            const textarea = document.querySelector('textarea[aria-label="Message"]') as HTMLTextAreaElement;
            const ev = new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true});
            Object.defineProperty(ev, 'isComposing', {get: () => true});
            textarea.dispatchEvent(ev);
        });

        // No user message may be submitted during composition.
        await expect(page.locator('article[aria-label="Your message"]')).toHaveCount(0);
        await expect(composer).toHaveValue('你好');
    });
});

test.describe('integrated suggest-company entry points (issue #471)', () => {
    // Executable cross-bundle coverage for the job surface: the JobMatchPanel
    // actions dispatch the crank:suggest-company window event, and the
    // main-bundle bridge opens the single host. The /chat/ assistant surface
    // has no suggest trigger to exercise — JobSearchChat renders result cards
    // and an availability notice only; the review thread records the blocker.
    const PANEL_FIXTURE = '/e2e/fixtures/job-match-panel.html';

    test('job empty state action opens the shared suggest form', async ({page}) => {
        await page.goto(`${PANEL_FIXTURE}?state=no_matches`);
        await page.getByTestId('action-suggest_company').click();
        await expect(page.getByTestId('suggest-company-modal')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.getByTestId('suggest-company-modal')).toHaveCount(0);
    });

    test('job outage (no source) state action opens the shared suggest form', async ({page}) => {
        await page.goto(`${PANEL_FIXTURE}?state=no_source`);
        await page.getByTestId('action-suggest_company').click();
        await expect(page.getByTestId('suggest-company-modal')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.getByTestId('suggest-company-modal')).toHaveCount(0);
    });
});

test.describe('ranked organizations follow the priorities met (issue #584)', () => {
    const PANEL_FIXTURE = '/e2e/fixtures/job-match-panel.html';

    test('rows print "n of m" in the server order, a tie shows its rating, and nothing reads "/ 100"', async ({page}) => {
        await page.goto(`${PANEL_FIXTURE}?state=org_ties`);
        const orgs = page.getByTestId('ranked-org-matches');
        await expect(orgs.locator('.job-match-card')).toHaveCount(3);
        await expect(orgs.locator('.job-match-name')).toHaveText(['Acme Corp', 'Birch Labs', 'Cedar Works']);
        await expect(page.getByTestId('org-7-priorities-met')).toHaveText('2 of 2');
        await expect(page.getByTestId('org-8-priorities-met')).toHaveText('1 of 2');
        await expect(page.getByTestId('org-9-priorities-met')).toHaveText('1 of 2');
        await expect(page.getByTestId('org-8-company-score')).toHaveText('4.6 / 5');
        await expect(page.getByTestId('org-9-company-score')).toHaveText('3.2 / 5');
        await expect(orgs.locator('.job-match-figure-label', {hasText: 'Average rating'})).toHaveCount(3);
        await expect(orgs.locator('.job-match-card').first().locator('.job-match-figure-label')).toHaveText(
            ['Priorities met', 'Average rating', 'Requirement coverage']);
        await expect(page.getByTestId('ranked-job-matches').locator('.job-match-card').first().locator('.job-match-figure-label')).toHaveText(
            ['Average rating', 'Fit', 'Requirement coverage']);
        await expect(page.getByTestId('ranked-org-order-note')).toContainText(
            'Most priorities met first, then the higher average rating. Checked for each job, not counted: Minimum salary.');
        await expect(orgs).not.toContainText('/ 100');
        await expect(page.getByTestId('ranked-job-matches')).toContainText('/ 100');
    });

    test('the order note balances its lines so no word is left alone', async ({page}) => {
        await page.goto(`${PANEL_FIXTURE}?state=org_ties`);
        const wrap = await page.getByTestId('ranked-org-order-note').evaluate(
            (el) => getComputedStyle(el).getPropertyValue('text-wrap') || getComputedStyle(el).getPropertyValue('text-wrap-style'),
        );
        expect(wrap).toMatch(/balance|pretty/);
    });

    test('a reader with only per-job priorities sees a dash and the reason for the order', async ({page}) => {
        await page.goto(`${PANEL_FIXTURE}?state=org_per_job_only`);
        await expect(page.getByTestId('org-7-priorities-met')).toHaveText('—');
        await expect(page.getByTestId('ranked-org-order-note')).toContainText('companies are listed by average rating');
    });
});

test.describe('200% zoom', () => {
    test.skip(({browserName}) => browserName !== 'chromium', 'page-scale zoom emulation is Chromium-only');

    test('no horizontal overflow at 200% page zoom', async ({page, context}) => {
        await mockJobSearchApi(page, 'populated');
        await page.goto(CHAT_FIXTURE);
        await expect(page.locator('[data-testid="result-cards"]')).toBeVisible();

        const cdp = await context.newCDPSession(page);
        await cdp.send('Emulation.setPageScaleFactor', {pageScaleFactor: 2});

        await expectNoHorizontalOverflow(page);
        await expectComposerUsable(page);
    });
});

// One reading of the panel, taken in a single evaluate so no number is paired
// with a later layout (issue #483).
const readPanel = (page: Page) => page.evaluate(() => {
    const body = document.querySelector('.assistant-panel-body') as HTMLElement;
    const card = body.querySelector('[data-testid="job-search-chat"]') as HTMLElement;
    const log = card.querySelector('[role="log"]') as HTMLElement;
    const header = card.querySelector('.card-header') as HTMLElement;
    const footer = card.querySelector('.chat-footer') as HTMLElement;
    const form = card.querySelector('form') as HTMLElement;
    const block = body.querySelector('[data-testid="priorities-sidebar"]') as HTMLElement | null;
    const formRect = form.getBoundingClientRect();
    const hit = document.elementFromPoint(formRect.left + formRect.width / 2, formRect.top + formRect.height / 2);
    const top = log.getBoundingClientRect().top;
    const articles = Array.from(log.querySelectorAll('article'));
    const firstVisible = articles.findIndex((a) => a.getBoundingClientRect().bottom > top + 1);
    return {
        bodyScrollRange: body.scrollHeight - body.clientHeight,
        bodyScrollTop: body.scrollTop,
        bodyHeight: body.clientHeight,
        logHeight: log.clientHeight,
        logScrollRange: log.scrollHeight - log.clientHeight,
        logScrollTop: log.scrollTop,
        logFromEnd: log.scrollHeight - log.clientHeight - log.scrollTop,
        logOverflowY: getComputedStyle(log).overflowY,
        blockHeight: block ? block.getBoundingClientRect().height : 0,
        blockBottom: block ? block.getBoundingClientRect().bottom : 0,
        headerTop: header.getBoundingClientRect().top,
        formTop: formRect.top,
        formBottom: formRect.bottom,
        composerInViewport: formRect.top >= -0.5 && formRect.bottom <= window.innerHeight + 0.5,
        composerHit: !!hit && form.contains(hit),
        // An inert footer (an open confirmation, issue #547) is skipped by hit testing: the panel stack is then
        // checked against the composer band by geometry.
        footerInert: footer.hasAttribute('inert'),
        stackBottom: (card.querySelector('.chat-panel-stack') as HTMLElement | null)?.getBoundingClientRect().bottom ?? null,
        positions: [header, footer, log].map((el) => getComputedStyle(el).position),
        firstVisible,
        firstVisibleOffset: firstVisible < 0 ? 0 : articles[firstVisible].getBoundingClientRect().top - top,
        pill: !!card.querySelector('[data-testid="jump-to-latest"]'),
    };
});

// Polls until two consecutive readings agree, then returns the settled one.
async function settledPanel(page: Page): Promise<Awaited<ReturnType<typeof readPanel>>> {
    await page.evaluate(() => document.fonts.ready.then(() => undefined));
    let previous = '';
    let reading = await readPanel(page);
    await expect.poll(async () => {
        reading = await readPanel(page);
        const now = JSON.stringify(reading);
        const same = now === previous;
        previous = now;
        return same;
    }, {message: 'the panel layout settles'}).toBe(true);
    return reading;
}

const expectOneScroller = (reading: Awaited<ReturnType<typeof readPanel>>, label: string) => {
    expect(reading.bodyScrollRange, `${label}: the panel body does not scroll`).toBeLessThanOrEqual(1);
    expect(reading.bodyScrollTop, `${label}: the panel body is not scrolled`).toBe(0);
    expect(reading.logOverflowY, `${label}: the transcript scrolls`).toBe('auto');
    expect(reading.composerInViewport, `${label}: the composer is inside the viewport`).toBe(true);
    if (reading.footerInert) {
        // The confirmation may cover the composer band it has made inert (visual review round 1), never more than the card.
        expect(reading.stackBottom ?? 0, `${label}: the panel stack ends within the composer band`).toBeLessThanOrEqual(reading.formBottom + 0.5);
    } else {
        expect(reading.composerHit, `${label}: nothing covers the composer`).toBe(true);
    }
    expect(reading.positions, `${label}: no chat bar is pinned`).not.toContain('sticky');
};

async function loadPanel(page: Page, query: string, scenario: 'empty' | 'populated' | 'long' = 'long') {
    await mockJobSearchApi(page, scenario);
    await page.goto(`${PANEL_CHAT_FIXTURE}?${query}`);
    if (scenario === 'empty') await expect(page.getByTestId('empty-history')).toBeVisible();
    else await expect(page.getByRole('log').locator('article').first()).toBeVisible();
}

// Runs in Chromium, Firefox and WebKit: the nested flex minimums that keep the
// transcript the only scroller must resolve the same way in all three.
test.describe('one transcript scroller (issue #483)', () => {
    for (const size of [{width: 320, height: 420}, {width: 320, height: 640}, {width: 375, height: 380}, {width: 375, height: 667},
        {width: 1024, height: 800}, {width: 1280, height: 900}]) {
        test(`only the transcript scrolls and the composer is on screen, ${size.width}x${size.height}`, async ({page}) => {
            await page.setViewportSize(size);
            await loadPanel(page, 'priorities=57');
            const reading = await settledPanel(page);
            expectOneScroller(reading, 'on load');
            expect(reading.logScrollRange, 'the long conversation overflows the transcript, not the panel').toBeGreaterThan(100);
            expect(reading.logHeight, 'the transcript keeps a usable height').toBeGreaterThanOrEqual(56);
            expect(reading.logFromEnd, 'the conversation opens at its latest message').toBeLessThanOrEqual(2);
            expect(reading.pill, 'a reader at the end sees no jump pill').toBe(false);
            // The transcript takes the wheel; the panel body stays where it is.
            await page.getByRole('log').evaluate((el) => { el.scrollTop -= 80; });
            await expect(page.getByTestId('jump-to-latest')).toBeVisible();
            const scrolled = await settledPanel(page);
            expect(scrolled.bodyScrollTop).toBe(0);
            expect(scrolled.logFromEnd).toBeGreaterThan(48);
            const pillHit = await page.getByTestId('jump-to-latest').evaluate((el) => {
                const r = el.getBoundingClientRect();
                const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                return !!top && (top === el || el.contains(top));
            });
            expect(pillHit, 'nothing covers the jump pill').toBe(true);
        });
    }

    test('an empty conversation does not scroll the panel and shows no jump pill at 320x420', async ({page}) => {
        await page.setViewportSize({width: 320, height: 420});
        await loadPanel(page, 'priorities=57', 'empty');
        const reading = await settledPanel(page);
        expectOneScroller(reading, 'empty');
        await page.getByRole('log').evaluate((el) => { el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll')); el.scrollTop = 0; });
        await expect(page.getByTestId('jump-to-latest')).toHaveCount(0);
    });

    test('the article the reader was on stays where it was when the viewport shrinks', async ({page}) => {
        await page.setViewportSize({width: 375, height: 700});
        await loadPanel(page, 'priorities=57');
        await settledPanel(page);
        await page.getByRole('log').evaluate((el) => { el.scrollTop -= 300; });
        await expect(page.getByTestId('jump-to-latest')).toBeVisible();
        const before = await settledPanel(page);
        expect(before.firstVisible).toBeGreaterThan(0);
        await page.setViewportSize({width: 375, height: 380});
        await expect.poll(async () => (await readPanel(page)).bodyHeight).toBeLessThan(before.bodyHeight);
        const after = await settledPanel(page);
        expectOneScroller(after, 'after the shrink');
        expect(after.firstVisible, 'the same article is first in view').toBe(before.firstVisible);
        expect(Math.abs(after.firstVisibleOffset - before.firstVisibleOffset)).toBeLessThanOrEqual(1);
        expect(Math.abs(after.logScrollTop - before.logScrollTop)).toBeLessThanOrEqual(1);
        expect(after.pill, 'the reader is still away from the end').toBe(true);
    });

    test('a reader at the end stays at the end when the viewport shrinks and grows', async ({page}) => {
        await page.setViewportSize({width: 375, height: 700});
        await loadPanel(page, 'priorities=57');
        const before = await settledPanel(page);
        await page.setViewportSize({width: 375, height: 380});
        await expect.poll(async () => (await readPanel(page)).bodyHeight).toBeLessThan(before.bodyHeight);
        const short = await settledPanel(page);
        expectOneScroller(short, 'short');
        expect(short.logFromEnd).toBeLessThanOrEqual(2);
        await page.setViewportSize({width: 375, height: 700});
        await expect.poll(async () => (await readPanel(page)).bodyHeight).toBe(before.bodyHeight);
        const tall = await settledPanel(page);
        expect(tall.logFromEnd).toBeLessThanOrEqual(2);
        expect(tall.pill).toBe(false);
    });

    test('focus moving into the chat header or the composer scrolls nothing', async ({page}) => {
        await page.setViewportSize({width: 375, height: 420});
        await loadPanel(page, 'priorities=57');
        await settledPanel(page);
        await page.getByRole('log').evaluate((el) => { el.scrollTop -= 400; });
        await expect(page.getByTestId('jump-to-latest')).toBeVisible();
        const before = await settledPanel(page);
        for (const target of ['conversation-more', 'jump-to-latest', 'assistant-composer']) {
            await page.getByTestId(target).focus();
            await expect(page.getByTestId(target)).toBeFocused();
            const after = await settledPanel(page);
            expect(after.bodyScrollTop, `focus on ${target}`).toBe(0);
            expect(Math.abs(after.logScrollTop - before.logScrollTop), `focus on ${target}`).toBeLessThanOrEqual(1);
        }
    });

    for (const priorities of [120, 240, 400]) {
        test(`an open block of ${priorities}px above the chat leaves the transcript 4.5rem or gives way itself`, async ({page}) => {
            await page.setViewportSize({width: 375, height: 600});
            await loadPanel(page, `priorities=${priorities}&open=1`);
            const reading = await settledPanel(page);
            expectOneScroller(reading, `block ${priorities}`);
            // 4.5rem box less its 1px borders.
            expect(reading.logHeight).toBeGreaterThanOrEqual(70);
            expect(reading.blockHeight / reading.bodyHeight, 'the block never takes more than 60% of the panel').toBeLessThanOrEqual(0.6 + 0.005);
            expect(reading.blockBottom, 'the chat header is under the block, not behind it').toBeLessThanOrEqual(reading.headerTop + 0.5);
            if (priorities === 400) {
                // The content is taller than the room: the block gave way and scrolls inside.
                expect(reading.blockHeight).toBeLessThan(400);
                expect(reading.blockHeight).toBeGreaterThanOrEqual(176);
                const inner = await page.getByTestId('priorities-scroll').evaluate((el) => el.scrollHeight - el.clientHeight);
                expect(inner).toBeGreaterThan(0);
            } else {
                expect(reading.blockHeight).toBeGreaterThanOrEqual(priorities);
            }
        });
    }

    test('below the floor the panel body scrolls as a plain scroller, keeping the focused composer whole, with nothing pinned', async ({page}) => {
        await page.setViewportSize({width: 320, height: 420});
        await loadPanel(page, 'priorities=400&open=1');
        const reading = await settledPanel(page);
        expect(reading.bodyScrollRange).toBeGreaterThan(1);
        // The composer takes focus on load, so the body is scrolled only by what cuts the composer band off.
        expect(reading.composerInViewport, 'the focused composer is whole').toBe(true);
        const cutOff = await page.evaluate(() => {
            const body = document.querySelector('.assistant-panel-body') as HTMLElement;
            const form = document.querySelector('#job-search-chat form') as HTMLElement;
            const saved = body.scrollTop;
            body.scrollTop = 0;
            const need = form.getBoundingClientRect().bottom - Math.min(body.getBoundingClientRect().bottom, window.innerHeight);
            body.scrollTop = saved;
            return need;
        });
        expect(Math.abs(reading.bodyScrollTop - cutOff), `scrolled ${reading.bodyScrollTop}px for a composer cut off by ${cutOff}px`).toBeLessThanOrEqual(2);
        expect(reading.blockHeight, 'the open block keeps 11rem').toBeGreaterThanOrEqual(176);
        expect(reading.logHeight).toBeGreaterThanOrEqual(70);
        expect(reading.positions).not.toContain('sticky');
        expect(reading.blockBottom).toBeLessThanOrEqual(reading.headerTop + 0.5);
        expect(reading.formTop, 'no chat bar overlaps another').toBeGreaterThanOrEqual(reading.headerTop);
    });

    test('with the composer focused, a shrink below the floor keeps the composer on screen', async ({page}) => {
        await page.setViewportSize({width: 375, height: 667});
        await loadPanel(page, 'priorities=400&open=1');
        await settledPanel(page);
        await page.getByTestId('assistant-composer').focus();
        await page.setViewportSize({width: 375, height: 380});
        await expect.poll(async () => (await readPanel(page)).bodyScrollRange).toBeGreaterThan(1);
        await expect.poll(async () => (await readPanel(page)).composerInViewport, {message: 'the composer is brought back'}).toBe(true);
        const reading = await settledPanel(page);
        expect(reading.composerHit).toBe(true);
        expect(reading.logFromEnd, 'the reader is still at the end').toBeLessThanOrEqual(2);
    });

    test.describe('on-screen keyboard that does not resize the page', () => {
        test('a visual viewport 300px shorter moves the composer up 300px; without one the layout is unchanged', async ({page}) => {
            await page.setViewportSize({width: 375, height: 700});
            await loadPanel(page, 'priorities=57');
            const plain = await settledPanel(page);

            // The same page with a visualViewport the test controls.
            await page.addInitScript(() => {
                const state = {inset: 0};
                (window as unknown as {__keyboard: typeof state}).__keyboard = state;
                const target = new EventTarget();
                const fake = {
                    get height() { return window.innerHeight - state.inset; },
                    get width() { return window.innerWidth; },
                    offsetTop: 0, offsetLeft: 0, pageTop: 0, pageLeft: 0, scale: 1,
                    addEventListener: target.addEventListener.bind(target),
                    removeEventListener: target.removeEventListener.bind(target),
                    dispatchEvent: target.dispatchEvent.bind(target),
                };
                Object.defineProperty(window, 'visualViewport', {configurable: true, get: () => fake});
            });
            await page.reload();
            await expect(page.getByRole('log').locator('article').first()).toBeVisible();
            const before = await settledPanel(page);
            expect(before.formBottom, 'a visual viewport as tall as the page changes nothing').toBe(plain.formBottom);
            expect(before.logHeight).toBe(plain.logHeight);

            await page.evaluate(() => {
                (window as unknown as {__keyboard: {inset: number}}).__keyboard.inset = 300;
                window.visualViewport!.dispatchEvent(new Event('resize'));
            });
            await expect.poll(async () => (await readPanel(page)).formBottom).toBeLessThan(before.formBottom);
            const up = await settledPanel(page);
            expect(Math.abs(before.formBottom - up.formBottom - 300)).toBeLessThanOrEqual(1);
            expect(up.formBottom, 'the composer band ends at the visual viewport').toBeLessThanOrEqual(400 + 1);
            expect(up.bodyScrollRange).toBeLessThanOrEqual(1);
            expect(up.logFromEnd, 'the reader is still at the end').toBeLessThanOrEqual(2);

            await page.evaluate(() => {
                (window as unknown as {__keyboard: {inset: number}}).__keyboard.inset = 0;
                window.visualViewport!.dispatchEvent(new Event('resize'));
            });
            await expect.poll(async () => (await readPanel(page)).formBottom).toBe(before.formBottom);
        });
    });

    // A visualViewport the test controls: `inset` is a keyboard's height, `scale` a pinch zoom (which also shrinks the
    // visual viewport, to the layout viewport divided by the scale).
    const installViewportStub = (page: Page) => page.addInitScript(() => {
        const state = {inset: 0, scale: 1};
        (window as unknown as {__viewport: typeof state}).__viewport = state;
        const target = new EventTarget();
        const fake = {
            get height() { return window.innerHeight / state.scale - state.inset; },
            get width() { return window.innerWidth / state.scale; },
            get scale() { return state.scale; },
            offsetTop: 0, offsetLeft: 0, pageTop: 0, pageLeft: 0,
            addEventListener: target.addEventListener.bind(target),
            removeEventListener: target.removeEventListener.bind(target),
            dispatchEvent: target.dispatchEvent.bind(target),
        };
        Object.defineProperty(window, 'visualViewport', {configurable: true, get: () => fake});
    });
    const setViewport = (page: Page, next: {inset?: number; scale?: number}) => page.evaluate((values) => {
        Object.assign((window as unknown as {__viewport: object}).__viewport, values);
        window.visualViewport!.dispatchEvent(new Event('resize'));
    }, next);

    test.describe('a visual viewport that is not a keyboard, or a keyboard after a reply', () => {
        for (const size of [{width: 375, height: 667}, {width: 320, height: 568}]) {
            test(`a pinch zoom to 2x leaves the transcript its height, ${size.width}x${size.height}`, async ({page}) => {
                await page.setViewportSize(size);
                await installViewportStub(page);
                await loadPanel(page, 'priorities=57');
                const before = await settledPanel(page);
                expect(before.logHeight).toBeGreaterThan(150);
                for (const scale of [1.5, 2]) {
                    await setViewport(page, {scale});
                    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
                    const zoomed = await settledPanel(page);
                    expect(zoomed.logHeight, `at ${scale}x the transcript is as tall as at 1x`).toBe(before.logHeight);
                    expect(zoomed.bodyScrollRange).toBe(before.bodyScrollRange);
                    expect(zoomed.logFromEnd).toBeLessThanOrEqual(2);
                }
                await expect(page.getByTestId('job-search-chat')).toHaveCSS('--chat-keyboard-inset', '0px');
                // The same shortfall without a zoom is a keyboard.
                await setViewport(page, {scale: 1, inset: 200});
                await expect.poll(async () => (await readPanel(page)).logHeight).toBeLessThan(before.logHeight);
            });
        }

        const sendAndWaitForReply = async (page: Page, text = 'Tell me more') => {
            await page.getByTestId('assistant-composer').fill(text);
            await page.getByRole('button', {name: 'Send message'}).click();
            await expect(page.getByText('Thanks — I found a few matches for you.')).toBeVisible();
            await settledPanel(page);
        };

        for (const [size, inset] of [[{width: 375, height: 667}, 290], [{width: 390, height: 844}, 300], [{width: 320, height: 568}, 250]] as const) {
            test(`a keyboard that opens after a reply leaves a following reader at the end, ${size.width}x${size.height}`, async ({page}) => {
                await page.setViewportSize(size);
                await installViewportStub(page);
                await loadPanel(page, 'priorities=57');
                await settledPanel(page);
                await sendAndWaitForReply(page);
                const before = await settledPanel(page);
                expect(before.logFromEnd).toBeLessThanOrEqual(2);
                await setViewport(page, {inset});
                await expect.poll(async () => (await readPanel(page)).logHeight).toBeLessThan(before.logHeight);
                const up = await settledPanel(page);
                expect(up.logFromEnd, 'the reader is still at the end').toBeLessThanOrEqual(2);
                expect(up.pill, 'no false "Jump to latest"').toBe(false);
                await setViewport(page, {inset: 0});
                await expect.poll(async () => (await readPanel(page)).logHeight).toBe(before.logHeight);
                const down = await settledPanel(page);
                expect(down.logFromEnd).toBeLessThanOrEqual(2);
                expect(down.pill).toBe(false);
            });
        }

        for (const size of [{width: 1280, height: 900}, {width: 375, height: 667}]) {
            test(`a five-line draft after a reply leaves a following reader at the end, ${size.width}x${size.height}`, async ({page}) => {
                await page.setViewportSize(size);
                await loadPanel(page, 'priorities=57');
                await settledPanel(page);
                await sendAndWaitForReply(page);
                const before = await settledPanel(page);
                await page.getByTestId('assistant-composer').fill('one\ntwo\nthree\nfour\nfive');
                await expect.poll(async () => (await readPanel(page)).logHeight).toBeLessThan(before.logHeight);
                const drafted = await settledPanel(page);
                expect(drafted.logFromEnd, 'the reader is still at the end').toBeLessThanOrEqual(2);
                expect(drafted.pill, 'no false "Jump to latest"').toBe(false);
            });
        }

        for (const [from, to] of [[{width: 375, height: 667}, {width: 375, height: 380}], [{width: 1280, height: 900}, {width: 1280, height: 600}]]) {
            test(`a layout resize after a reply leaves a following reader at the end, ${from.width}x${from.height} to ${to.height}`, async ({page}) => {
                await page.setViewportSize(from);
                await loadPanel(page, 'priorities=57');
                await settledPanel(page);
                await sendAndWaitForReply(page);
                const before = await settledPanel(page);
                await page.setViewportSize(to);
                await expect.poll(async () => (await readPanel(page)).logHeight).toBeLessThan(before.logHeight);
                const after = await settledPanel(page);
                expect(after.logFromEnd, 'the reader is still at the end').toBeLessThanOrEqual(2);
                expect(after.pill, 'no false "Jump to latest"').toBe(false);
            });
        }
    });

    test.describe('the More menu leaves its button pressable, so a second press closes it', () => {
        for (const [size, query] of [
            [{width: 375, height: 380}, ''], [{width: 667, height: 375}, ''], [{width: 320, height: 420}, ''],
            [{width: 1280, height: 480}, ''], [{width: 1024, height: 480}, '&mode=drawer'], [{width: 800, height: 480}, '&mode=drawer'],
        ] as const) {
            test(`${size.width}x${size.height}${query}`, async ({page}) => {
                await page.setViewportSize(size);
                await loadPanel(page, `priorities=57${query}`);
                await settledPanel(page);
                const more = page.getByTestId('conversation-more');
                const box = (await more.boundingBox())!;
                const centre = {x: box.x + box.width / 2, y: box.y + box.height / 2};
                await page.mouse.click(centre.x, centre.y);
                const menu = page.getByTestId('conversation-menu');
                await expect(menu).toBeVisible();
                // The menu leaves the button's centre uncovered and every item whole, with the panel at rest.
                const open = await page.evaluate(({x, y}) => {
                    const items = Array.from(document.querySelectorAll('[data-testid="conversation-menu"] [role="menuitem"], [data-testid="conversation-menu"] button'));
                    const hit = document.elementFromPoint(x, y);
                    const body = document.querySelector('.assistant-panel-body') as HTMLElement;
                    return {
                        buttonUncovered: !!hit && !!hit.closest('[data-testid="conversation-more"]'),
                        itemTops: items.map((el) => Math.round(el.getBoundingClientRect().top)),
                        itemBottoms: items.map((el) => Math.round(el.getBoundingClientRect().bottom)),
                        viewport: window.innerHeight,
                        bodyScrolls: body.scrollHeight > body.clientHeight + 1,
                    };
                }, centre);
                expect(open.buttonUncovered, 'the menu is not over the More button').toBe(true);
                expect(open.itemTops.length).toBeGreaterThanOrEqual(4);
                expect(Math.min(...open.itemTops)).toBeGreaterThanOrEqual(0);
                expect(Math.max(...open.itemBottoms)).toBeLessThanOrEqual(open.viewport);
                expect(open.bodyScrolls, 'the panel body does not gain a scroll range').toBe(false);
                await page.mouse.click(centre.x, centre.y);
                await expect(menu).toHaveCount(0);
                await expect(page.getByTestId('confirm-action')).toHaveCount(0);
            });
        }
    });

    test.describe('the unread pill can be pressed at its centre where the transcript is short', () => {
        const scrollUpToPill = async (page: Page) => {
            await page.getByRole('log').evaluate((el) => { el.dispatchEvent(new Event('wheel')); el.scrollTop = Math.max(0, el.scrollTop - 200); });
            await expect(page.getByTestId('jump-to-latest')).toBeVisible();
        };
        const pressCentre = async (page: Page) => {
            const pill = page.getByTestId('jump-to-latest');
            const hit = await pill.evaluate((el) => {
                const r = el.getBoundingClientRect();
                const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                return !!top && (top === el || el.contains(top));
            });
            expect(hit, 'the pill is topmost at its centre').toBe(true);
            const box = (await pill.boundingBox())!;
            await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
            await expect(pill).toHaveCount(0);
            expect((await readPanel(page)).logFromEnd, 'the press jumped to the end').toBeLessThanOrEqual(2);
        };
        const withNotice = async (page: Page) => {
            await page.route('**/api/agent/assistant-status/', (route) => route.fulfill({
                status: 200, contentType: 'application/json',
                body: JSON.stringify({state: 'temporarily_unavailable', actions: ['retry'], checked_at: '2026-08-20T00:00:00Z'}),
            }));
        };

        for (const notice of [false, true]) {
            test(`667x340${notice ? ' under a status notice' : ''}`, async ({page}) => {
                await page.setViewportSize({width: 667, height: 340});
                if (notice) await withNotice(page);
                await loadPanel(page, 'priorities=57');
                if (notice) await expect(page.getByTestId('assistant-status-notice')).toBeVisible();
                await settledPanel(page);
                await scrollUpToPill(page);
                await pressCentre(page);
            });

            test(`320x568 under a stubbed 250px keyboard${notice ? ' and a status notice' : ''}`, async ({page}) => {
                await page.setViewportSize({width: 320, height: 568});
                await installViewportStub(page);
                if (notice) await withNotice(page);
                await loadPanel(page, 'priorities=57');
                if (notice) await expect(page.getByTestId('assistant-status-notice')).toBeVisible();
                const plain = await settledPanel(page);
                // A 250px keyboard leaves the real panel's bars no room beside a status notice: the transcript would
                // sit wholly under the panel's header. The notice case takes the keyboard that leaves it about 40px.
                await setViewport(page, {inset: notice ? plain.logHeight - 40 : 250});
                await expect.poll(async () => (await readPanel(page)).logHeight).toBeLessThan(60);
                await scrollUpToPill(page);
                await pressCentre(page);
            });
        }
    });

    for (const size of [{width: 320, height: 420}, {width: 320, height: 640}]) {
        for (const [trigger, label] of [['conversation-new', 'new'], ['conversation-delete', 'delete']]) {
            test(`the ${label} confirm opens on its heading and consequence and its action and Cancel are reachable, ${size.width}x${size.height}`, async ({page}) => {
                await page.setViewportSize(size);
                await loadPanel(page, 'priorities=57');
                // CI's fallback font is wider than most hosts': the sentence wraps to more lines there.
                await page.addStyleTag({content: "body, button, input, textarea, select { font-family: 'DejaVu Sans', sans-serif !important; }"});
                await settledPanel(page);
                await page.getByTestId('conversation-more').click();
                await page.getByTestId(trigger).click();
                await expect(page.getByTestId('confirm-panel')).toBeVisible();
                const reading = await settledPanel(page);
                expectOneScroller(reading, 'with the confirmation open');
                // Nothing is pinned (issue #547): the heading and the whole consequence are in the stack on open, and the
                // action row is reached by scrolling the stack.
                const consequence = await page.getByTestId('confirm-panel').evaluate((panel) => {
                    const stack = panel.closest('.chat-panel-stack')!.getBoundingClientRect();
                    const heading = panel.querySelector('.chat-confirm-heading')!.getBoundingClientRect();
                    const copy = panel.querySelector('.chat-confirm-copy')!.getBoundingClientRect();
                    return {headingInside: heading.top >= stack.top - 0.5, copyInside: copy.bottom <= stack.bottom + 0.5};
                });
                expect(consequence, 'the heading and the whole consequence are visible on open').toEqual({headingInside: true, copyInside: true});
                for (const id of ['confirm-action', 'confirm-cancel']) {
                    await page.getByTestId(id).scrollIntoViewIfNeeded();
                    const topmost = await page.getByTestId(id).evaluate((el) => {
                        const r = el.getBoundingClientRect();
                        return [r.top + 2, r.top + r.height / 2, r.bottom - 2].map((y) => {
                            const hit = document.elementFromPoint(r.left + r.width / 2, y);
                            return !!hit && (hit === el || el.contains(hit));
                        });
                    });
                    expect(topmost, id).toEqual([true, true, true]);
                }
            });
        }
    }

    test('after a failed delete at 320x420 the focused retry is topmost and nothing covers it', async ({page}) => {
        await page.setViewportSize({width: 320, height: 420});
        await loadPanel(page, 'priorities=57');
        // Registered after the catch-all mock so it wins for the delete call.
        await page.route('**/api/agent/conversations/*/delete/**', (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
        await page.getByTestId('conversation-more').click();
        await page.getByTestId('conversation-delete').click();
        await waitForConfirmArmed(page);
        await page.getByTestId('confirm-action').click();
        const retry = page.getByTestId('confirm-action');
        await expect(page.getByRole('alert').filter({hasText: /delete/i})).toBeVisible();
        await expect(retry).toBeFocused();
        const reading = await settledPanel(page);
        expectOneScroller(reading, 'after the failed delete');
        // The failure text, the whole consequence and the retry are on screen together (visual review round 1, P1-1 / P1-2).
        const together = await page.getByTestId('confirm-panel').evaluate((panel) => {
            const stack = panel.closest('.chat-panel-stack')!.getBoundingClientRect();
            const inside = (sel: string) => {
                const r = panel.querySelector(sel)!.getBoundingClientRect();
                return r.top >= stack.top - 0.5 && r.bottom <= stack.bottom + 0.5;
            };
            return {error: inside('.chat-confirm-error'), copy: inside('.chat-confirm-copy'), retry: inside('[data-testid="confirm-action"]'), cancel: inside('[data-testid="confirm-cancel"]')};
        });
        expect(together).toEqual({error: true, copy: true, retry: true, cancel: true});
        const topmost = await retry.evaluate((el) => {
            const r = el.getBoundingClientRect();
            return [r.top + 2, r.top + r.height / 2, r.bottom - 2].map((y) => {
                const hit = document.elementFromPoint(r.left + r.width / 2, y);
                return !!hit && (hit === el || el.contains(hit));
            });
        });
        expect(topmost).toEqual([true, true, true]);
    });
});

// Issue #568: the confirmation opens under the pointer that chose its item, so
// the second press of a double press lands on the confirm button. It must be
// ignored, however late it is released or handled; a deliberate press after the
// delay still confirms. The fixture carries the real panel header and the real
// collapsed row (issue #483), so its menu sits where the real page's does and the
// confirm button opens under the item at the listed sizes: on the real page the
// centre of the item is on the button at 375x540 and 360x520, and not at 320x480,
// 375x380, 320x420, 375x440 or 412x460, which are therefore not cases here (since
// issue #547 the action row follows the consequence in normal flow instead of
// pinning to the bottom of a capped stack, so it no longer opens under the item
// where the stack is capped; the delay itself is pinned by Jest at 499 and 500 ms). 320x568 is not a case
// either: the overlap there depends on the fonts (about 15 px high with some
// hosts' fonts and none with CI's in Chromium and Firefox), which is under the
// 8 px the cases require.
test.describe('a double press on Delete conversation does not delete (issue #568)', () => {
    test.use({hasTouch: true});

    test.beforeEach(async ({page}) => { await recordPresses(page); });

    const SIZES = [
        {width: 375, height: 667}, {width: 390, height: 844}, {width: 1024, height: 800}, {width: 1280, height: 800},
        {width: 375, height: 540}, {width: 360, height: 520},
    ];

    type Press = (page: Page, x: number, y: number) => Promise<void>;
    const touchPress: Press = async (page, x, y) => { await page.touchscreen.tap(x, y); };

    const expectNothingDeleted = async (page: Page, deletes: string[], articlesBefore: number, what: string) => {
        // Give a (wrongly) accepted press the time to send its request.
        await page.waitForTimeout(150);
        expect(deletes, `${what} deleted nothing`).toEqual([]);
        await expect(page.getByTestId('confirm-panel')).toBeVisible();
        await expect(page.getByRole('log').locator('article')).toHaveCount(articlesBefore);
        await expect(page.getByRole('alert')).toHaveCount(0);
    };

    const prepare = async (page: Page, size: {width: number; height: number}) => {
        await page.setViewportSize(size);
        await loadPanel(page, 'priorities=57');
        const deletes = trackDeletes(page);
        await settledPanel(page);
        const articlesBefore = await page.getByRole('log').locator('article').count();
        const openDelete = async () => {
            await page.getByTestId('conversation-more').click();
            const item = await page.getByTestId('conversation-delete').boundingBox();
            expect(item, 'the menu item is on screen').not.toBeNull();
            return item!;
        };

        // Where the confirm button opens, and so where a press can land on both the item and the button.
        const item = await openDelete();
        await page.getByTestId('conversation-delete').click();
        const button = (await page.getByTestId('confirm-action').boundingBox())!;
        await page.getByTestId('confirm-cancel').click();
        const left = Math.max(item.x, button.x);
        const right = Math.min(item.x + item.width, button.x + button.width);
        const top = Math.max(item.y, button.y);
        const bottom = Math.min(item.y + item.height, button.y + button.height);
        expect(right - left, 'the confirm button opens under part of the item').toBeGreaterThan(8);
        expect(bottom - top, 'the confirm button opens under part of the item').toBeGreaterThan(8);
        return {deletes, articlesBefore, openDelete, x: (left + right) / 2, y: (top + bottom) / 2};
    };

    for (const size of SIZES) {
        // 400 is the gap closest to the delay. On an overloaded runner it is the first
        // to fail, with "the runner was too slow to prove anything"; do not answer that
        // with a retry or a longer timeout. Lowering it to 300 would lose nothing,
        // because Jest pins 499 and 500.
        for (const [input, press, gaps] of [['mouse', mousePress, [80, 150, 400]], ['touch', touchPress, [120]]] as const) {
            test(`${input} double press is ignored, then a deliberate press deletes, ${size.width}x${size.height}`, async ({page}) => {
                const {deletes, articlesBefore, openDelete, x, y} = await prepare(page, size);

                for (const gap of gaps) {
                    expect(inside(await openDelete(), x, y), 'the press point is on the menu item').toBe(true);
                    await press(page, x, y);
                    const openedAt = Date.now();
                    const panel = page.getByTestId('confirm-panel');
                    await expect(panel).toBeVisible();
                    await expectPressOnConfirm(page, x, y);
                    await waitUntil(page, openedAt, gap);
                    await press(page, x, y);
                    await expectStartedInside(page, `the second press (gap ${gap} ms)`);
                    await expectNothingDeleted(page, deletes, articlesBefore, `the second press (gap ${gap} ms)`);
                    await page.getByTestId('confirm-cancel').click();
                    await expect(panel).toHaveCount(0);
                }

                // A deliberate press after the delay still deletes.
                await openDelete();
                await page.getByTestId('conversation-delete').click();
                await expect(page.getByTestId('confirm-panel')).toBeVisible();
                await waitForConfirmArmed(page);
                await page.getByTestId('confirm-action').click();
                await expect.poll(() => deletes.length, {message: 'a press after the delay deletes'}).toBe(1);
            });
        }
    }

    // The finger goes down before the confirmation could be read and comes up after the delay.
    for (const size of [{width: 375, height: 667}, {width: 1280, height: 800}]) {
        test(`a mouse press held down from 150 ms to 560 ms is ignored, then a deliberate press deletes, ${size.width}x${size.height}`, async ({page}) => {
            const {deletes, articlesBefore, openDelete, x, y} = await prepare(page, size);
            expect(inside(await openDelete(), x, y), 'the press point is on the menu item').toBe(true);
            await mousePress(page, x, y);
            const openedAt = Date.now();
            await expect(page.getByTestId('confirm-panel')).toBeVisible();
            await expectPressOnConfirm(page, x, y);
            await waitUntil(page, openedAt, 150);
            await mouseDown(page, x, y);
            await waitUntil(page, openedAt, CONFIRM_ARM_DELAY_MS + 60);
            await page.mouse.up();
            await expectStartedInside(page, 'the held press');
            const {end} = await readPressGaps(page);
            expect(end, 'the held press was released after the delay').toBeGreaterThanOrEqual(CONFIRM_ARM_DELAY_MS);
            await expectNothingDeleted(page, deletes, articlesBefore, 'the held press');

            await waitForConfirmArmed(page);
            await page.getByTestId('confirm-action').click();
            await expect.poll(() => deletes.length, {message: 'a deliberate press deletes'}).toBe(1);
        });
    }

    // The confirmation opens when its item is chosen, not when More opens and not when its kind changes.
    test('a double press 700 ms after More opened is ignored: the delay counts from choosing the item', async ({page}) => {
        const {deletes, articlesBefore, x, y} = await prepare(page, {width: 375, height: 667});
        await page.getByTestId('conversation-more').click();
        const item = await page.getByTestId('conversation-delete').boundingBox();
        expect(inside(item!, x, y), 'the press point is on the menu item').toBe(true);
        await page.waitForTimeout(700);
        await mousePress(page, x, y);
        const openedAt = Date.now();
        await expect(page.getByTestId('confirm-panel')).toBeVisible();
        await expectPressOnConfirm(page, x, y);
        await waitUntil(page, openedAt, 150);
        await mousePress(page, x, y);
        await expectStartedInside(page, 'the second press (gap 150 ms)');
        await expectNothingDeleted(page, deletes, articlesBefore, 'the second press');
    });

    test('a double press on the item chosen again while its confirmation is open is ignored', async ({page}) => {
        const {deletes, articlesBefore, openDelete, x, y} = await prepare(page, {width: 375, height: 667});
        expect(inside(await openDelete(), x, y), 'the press point is on the menu item').toBe(true);
        await page.getByTestId('conversation-delete').click();
        await expect(page.getByTestId('confirm-panel')).toBeVisible();
        // Long enough that the first confirmation would accept a press.
        await page.waitForTimeout(CONFIRM_ARM_DELAY_MS + 100);
        expect(inside(await openDelete(), x, y), 'the press point is on the menu item').toBe(true);
        await mousePress(page, x, y);
        const openedAt = Date.now();
        await expect(page.getByTestId('confirm-panel')).toBeVisible();
        await expectPressOnConfirm(page, x, y);
        await waitUntil(page, openedAt, 150);
        await mousePress(page, x, y);
        await expectStartedInside(page, 'the second press (gap 150 ms)');
        await expectNothingDeleted(page, deletes, articlesBefore, 'the second press');
    });

    // A keyboard activation is judged by its own click; real keys, with focus put on the button directly.
    test('Enter on the confirm button right after it opens is ignored, then Enter after the delay deletes', async ({page}) => {
        await page.setViewportSize({width: 375, height: 667});
        await loadPanel(page, 'priorities=57');
        const deletes = trackDeletes(page);
        await settledPanel(page);
        const articlesBefore = await page.getByRole('log').locator('article').count();
        await page.getByTestId('conversation-more').focus();
        await page.keyboard.press('ArrowUp');
        await expect(page.getByTestId('conversation-delete')).toBeFocused();
        await page.keyboard.press('Enter');
        await expect(page.getByRole('heading', {name: 'Delete conversation?'})).toBeFocused();
        await page.getByTestId('confirm-action').focus();
        await page.keyboard.press('Enter');
        await expectStartedInside(page, 'the Enter on the confirm button');
        await expectNothingDeleted(page, deletes, articlesBefore, 'the Enter on the confirm button');

        await waitForConfirmArmed(page);
        await page.keyboard.press('Enter');
        await expect.poll(() => deletes.length, {message: 'Enter after the delay deletes'}).toBe(1);
    });
});

// Notices live in the transcript, and the footer notices collapse to one line (issue #483). Runs in Chromium,
// Firefox and WebKit on the fixture page, where the chat sits in the same panel markup the app uses.
test.describe('notices in the transcript and collapsing footer notices (issue #483)', () => {
    const CHANGES = [
        {path: 'compensation.minimum_salary', old: null, new: 150000},
        {path: 'work_location.modes', old: [], new: ['remote']},
        {path: 'notes', old: '', new: 'prefers remote-first teams'},
    ];
    const PROPOSAL = {
        id: 'prop-483', scope: 'account', changes: CHANGES, change_count: 3, base_revision: 2, unsupported_criteria: [], currency: 'USD',
        token: {patch: {set: {notes: 'prefers remote-first teams'}}, scope: 'account', base_revision: 2},
    };
    const LONG_ERROR = `${'The assistant could not finish this reply because the provider stopped answering. '.repeat(8)}`.slice(0, 600);
    const jsonRoute = (page: Page, url: string | RegExp, body: unknown, status = 200) => page.route(url, (route) => {
        if (route.request().method() === 'GET' && String(url).includes('conversations')) return route.fallback();
        return route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    });
    // Each reply has an id of its own: two replies with one id (a 1 in 900 draw, seen once in CI) put a notice under both.
    let replyId = 9000;
    const replyWith = (page: Page, extra: Record<string, unknown>, content = 'I can save these as your priorities.') => page.route(/\/api\/agent\/conversations\/\d+\/$/, (route) => (route.request().method() === 'POST'
        ? route.fulfill({status: 201, contentType: 'application/json', body: JSON.stringify({
            message: {id: replyId += 1, role: 'assistant', content, preferences_changed: false, created: null, results: null},
            preferences_changed: false, ...extra,
        })})
        : route.fallback()));
    const send = async (page: Page, text: string) => {
        await page.locator('textarea[aria-label="Message"]').fill(text);
        await page.getByRole('button', {name: 'Send message'}).click();
    };
    // Everything the checks need, read in one evaluate.
    const read = (page: Page) => page.evaluate(() => {
        const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
        const body = document.querySelector('.assistant-panel-body') as HTMLElement;
        const log = document.querySelector('[role="log"]') as HTMLElement;
        const notices = document.querySelector('[data-testid="chat-footer-notices"]') as HTMLElement | null;
        const form = document.querySelector('#job-search-chat form') as HTMLElement;
        const notice = document.querySelector('[data-testid="transcript-notice"]') as HTMLElement | null;
        const formRect = form.getBoundingClientRect();
        const item = notice?.querySelector('.pref-change-item') ?? null;
        const path = item?.querySelector('.pref-change-path') ?? null;
        const values = item?.querySelector('.pref-change-values') ?? null;
        const before = notice?.previousElementSibling ?? null;
        const alerts = Array.from(document.querySelectorAll('#job-search-chat [data-testid="chat-error"]'));
        return {
            bodyScrollRange: body.scrollHeight - body.clientHeight,
            logHeight: log.clientHeight,
            noticesHeight: notices ? notices.getBoundingClientRect().height : 0,
            collapsed: !!notices?.classList.contains('is-collapsed'),
            collapsible: !!notices?.classList.contains('is-collapsible'),
            composerInViewport: formRect.top >= -0.5 && formRect.bottom <= window.innerHeight + 0.5,
            noticeInLog: !!notice && log.contains(notice),
            noticeAfter: before ? before.getAttribute('aria-label') : null,
            noticeAfterText: before ? (before.textContent ?? '') : null,
            valuesRem: values ? values.getBoundingClientRect().width / rem : null,
            stacked: values && path ? values.getBoundingClientRect().top >= path.getBoundingClientRect().bottom - 1 : null,
            valuesFitName: values && path ? values.getBoundingClientRect().right <= (values.closest('.pref-change-notice') as HTMLElement).getBoundingClientRect().right + 1 : null,
            alertCount: alerts.length,
            alertText: alerts.length === 1 ? (alerts[0].textContent ?? '').replace(/Retry$/, '').trim() : null,
        };
    });
    const settled = async (page: Page) => {
        await page.evaluate(() => document.fonts.ready.then(() => undefined));
        let previous = '';
        let reading = await read(page);
        await expect.poll(async () => {
            reading = await read(page);
            const now = JSON.stringify(reading);
            const same = now === previous;
            previous = now;
            return same;
        }, {message: 'the layout settles'}).toBe(true);
        return reading;
    };

    for (const size of [{width: 1280, height: 900}, {width: 1024, height: 800}]) {
        test(`the proposal, the applied notice, the search-only notice and the banner are in the transcript and take no height from it, ${size.width}x${size.height}`, async ({page}) => {
            await page.setViewportSize(size);
            await loadPanel(page, 'priorities=57');
            const quiet = await settled(page);
            expect(quiet.noticeInLog).toBe(false);

            const expectInTranscript = (shown: Awaited<ReturnType<typeof settled>>, label: string, rows = true) => {
                expect(shown.noticeInLog, `${label}: the notice is inside the transcript`).toBe(true);
                expect(shown.noticeAfter, `${label}: the notice follows the assistant's reply`).toBe('Assistant message');
                expect(shown.noticeAfterText, `${label}: the notice follows the reply that made it`).toContain('I can save these');
                expect(shown.logHeight, `${label}: the transcript keeps its height`).toBe(quiet.logHeight);
                expect(shown.bodyScrollRange, `${label}: the panel body does not scroll`).toBeLessThanOrEqual(1);
                // AC 10: the values column is at least 8rem wide, or the values stack under the name.
                if (!rows) return;
                expect(shown.valuesRem! >= 8 || shown.stacked, `${label}: values ${shown.valuesRem}rem wide, stacked ${shown.stacked}`).toBe(true);
                expect(shown.valuesFitName, `${label}: values stay inside the notice`).toBe(true);
            };

            // The proposal, then apply it for the account.
            await replyWith(page, {preference_proposal: PROPOSAL});
            await send(page, 'Please keep it remote.');
            await expect(page.getByTestId('preference-proposal-notice')).toBeVisible();
            expectInTranscript(await settled(page), 'proposal');
            await jsonRoute(page, '**/api/agent/preferences/apply/', {applied: true, scope: 'account', revision: 3, changes: CHANGES, undo: {expected_revision: 3, document: {}}});
            await page.getByTestId('preference-apply-button').click();
            await expect(page.getByTestId('preference-change-notice')).toBeVisible();
            expectInTranscript(await settled(page), 'applied with undo');
            // Dismissing changes neither the transcript's height nor the body's scroll range.
            await page.getByRole('button', {name: 'Dismiss preference notice'}).first().click();
            await expect(page.getByTestId('transcript-notice')).toHaveCount(0);
            expect(await settled(page)).toMatchObject({logHeight: quiet.logHeight, bodyScrollRange: quiet.bodyScrollRange});

            // This search only.
            await page.unroute(/\/api\/agent\/conversations\/\d+\/$/);
            await mockJobSearchApi(page, 'long');
            await replyWith(page, {preference_proposal: PROPOSAL});
            await send(page, 'Only for this search.');
            await expect(page.getByTestId('preference-proposal-notice')).toBeVisible();
            await jsonRoute(page, '**/api/agent/preferences/apply/', {applied: false, scope: 'search', matches: {job_matches: [{listing_id: 1}], organization_matches: []}});
            await page.getByTestId('preference-proposal-search-only-button').click();
            await expect(page.getByTestId('preference-search-applied')).toBeVisible();
            expectInTranscript(await settled(page), 'this search only', false);
            await page.getByRole('button', {name: 'Dismiss search filter notice'}).click();

            // The plain banner.
            await page.unroute(/\/api\/agent\/conversations\/\d+\/$/);
            await mockJobSearchApi(page, 'long');
            await replyWith(page, {preferences_changed: true});
            await send(page, 'One more thing.');
            const banner = page.getByRole('status', {name: 'Preference update'});
            await expect(banner).toBeVisible();
            const shown = await settled(page);
            expect(shown.noticeInLog).toBe(true);
            expect(shown.logHeight).toBe(quiet.logHeight);
            expect(shown.bodyScrollRange).toBeLessThanOrEqual(1);
        });
    }

    for (const size of [{width: 1280, height: 900}, {width: 1024, height: 800}, {width: 375, height: 667}, {width: 320, height: 640}]) {
        test(`a status notice and a 600-character error collapse to one compact row behind Details, ${size.width}x${size.height}`, async ({page}) => {
            await page.setViewportSize(size);
            await page.route('**/api/agent/assistant-status/', (route) => route.fulfill({
                status: 200, contentType: 'application/json',
                body: JSON.stringify({state: 'temporarily_unavailable', actions: ['retry'], checked_at: '2026-10-13T00:00:00Z'}),
            }));
            await loadPanel(page, 'priorities=57');
            await expect(page.getByTestId('assistant-status-notice')).toBeVisible();
            const statusOnly = await settled(page);
            expect(statusOnly.collapsible).toBe(false);

            await page.route(/\/api\/agent\/conversations\/\d+\/$/, (route) => (route.request().method() === 'POST'
                ? route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: {type: 'assistant_unavailable', message: LONG_ERROR}})})
                : route.fallback()));
            await send(page, 'And which of them are hiring now?');
            await expect(page.getByTestId('chat-error')).toBeVisible();
            const details = page.getByTestId('chat-notices-details');
            await expect(details).toHaveText('Details');
            await expect(details).toHaveAttribute('aria-expanded', 'false');
            const collapsed = await settled(page);
            expect(collapsed.collapsed).toBe(true);
            expect(collapsed.noticesHeight, 'the collapsed notice area is one compact row').toBeLessThanOrEqual(56);
            expect(collapsed.composerInViewport, 'collapsed: the composer is inside the viewport').toBe(true);
            expect(collapsed.bodyScrollRange).toBeLessThanOrEqual(1);
            // One alert holds the whole text, collapsed.
            expect(collapsed.alertCount).toBe(1);
            expect(collapsed.alertText).toBe(LONG_ERROR);
            // The folded status notice lends the row its own title (one element, not a copy); its body and action are folded.
            await expect(page.getByTestId('assistant-status-notice')).toHaveCount(1);
            await expect(page.locator('#job-search-chat .assistant-status-title')).toBeVisible();
            await expect(page.locator('#job-search-chat .assistant-status-title')).toHaveText('Assistant temporarily unavailable');
            await expect(page.locator('#job-search-chat .assistant-status-body')).toBeHidden();
            await expect(page.getByTestId('assistant-status-retry')).toBeHidden();
            // The error is clipped by CSS only: its box is two lines, its text is whole.
            const clipped = await page.getByTestId('chat-error').locator('.chat-error-text').evaluate((el) => ({
                cut: el.scrollHeight > el.clientHeight + 1, lines: Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight)),
            }));
            expect(clipped).toEqual({cut: true, lines: 2});
            for (const id of ['retry-button', 'chat-notices-details']) {
                expect(Math.round((await page.getByTestId(id).boundingBox())!.height), `${id} is a 44px target`).toBeGreaterThanOrEqual(44);
            }

            await details.click();
            await expect(details).toHaveText('Hide details');
            await expect(details).toHaveAttribute('aria-expanded', 'true');
            await expect(page.locator('#job-search-chat .assistant-status-body')).toBeVisible();
            const expanded = await settled(page);
            expect(expanded.collapsed).toBe(false);
            expect(expanded.composerInViewport, 'expanded: the composer is inside the viewport').toBe(true);
            expect(expanded.bodyScrollRange).toBeLessThanOrEqual(1);
            expect(expanded.alertCount).toBe(1);
            expect(expanded.alertText).toBe(LONG_ERROR);
            // The expanded notices stop at the existing cap and scroll inside it.
            const capped = await page.getByTestId('chat-footer-notices').evaluate((el) => ({
                cap: parseFloat(getComputedStyle(el).maxHeight), height: el.getBoundingClientRect().height,
                scrolls: getComputedStyle(el.querySelector('.chat-notices-body')!).overflowY,
            }));
            expect(capped.height).toBeLessThanOrEqual(capped.cap + 1);
            expect(capped.scrolls).toBe('auto');

            await details.click();
            await expect(details).toHaveText('Details');
            expect((await settled(page)).collapsed).toBe(true);

            // A new error starts collapsed, even after the last one was expanded.
            await details.click();
            await page.unroute(/\/api\/agent\/conversations\/\d+\/$/);
            await page.route(/\/api\/agent\/conversations\/\d+\/$/, (route) => (route.request().method() === 'POST'
                ? route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: {type: 'assistant_unavailable', message: `Second failure. ${LONG_ERROR}`}})})
                : route.fallback()));
            await page.getByTestId('retry-button').click();
            await expect(page.getByTestId('chat-error')).toContainText('Second failure.');
            await expect(page.getByTestId('chat-notices-details')).toHaveAttribute('aria-expanded', 'false');
            expect((await settled(page)).collapsed).toBe(true);
        });
    }

    test('a failed send at 320x640 leaves the transcript at least 230px tall, and its Retry is in the compact row', async ({page}) => {
        await page.setViewportSize({width: 320, height: 640});
        await loadPanel(page, 'priorities=57');
        const quiet = await settled(page);
        await page.route(/\/api\/agent\/conversations\/\d+\/$/, (route) => (route.request().method() === 'POST'
            ? route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: {type: 'assistant_unavailable', message: 'The assistant is not available right now. Please try again later.'}})})
            : route.fallback()));
        await send(page, 'And which of them are hiring now?');
        await expect(page.getByTestId('retry-button')).toBeVisible();
        const failed = await settled(page);
        expect(failed.collapsed).toBe(true);
        expect(failed.noticesHeight).toBeLessThanOrEqual(56);
        // 230px, whatever the font: at 340px and under the disclaimer is 11px type and stays on one line.
        const disclaimerLines = await page.locator('#job-search-chat .chat-disclaimer').evaluate((el) => (
            Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight))));
        expect(disclaimerLines).toBeGreaterThan(0);
        expect(failed.logHeight, `the transcript was ${quiet.logHeight}px before the send`).toBeGreaterThanOrEqual(230);
        expect(quiet.logHeight - failed.logHeight, 'the failed send costs the transcript at most the compact row').toBeLessThanOrEqual(56);
        expect(failed.composerInViewport).toBe(true);
        // The Retry in the line works: it sends the same turn again.
        let posts = 0;
        await page.unroute(/\/api\/agent\/conversations\/\d+\/$/);
        await page.route(/\/api\/agent\/conversations\/\d+\/$/, (route) => {
            if (route.request().method() !== 'POST') return route.fallback();
            posts += 1;
            return route.fulfill({status: 201, contentType: 'application/json', body: JSON.stringify({
                message: {id: 9100, role: 'assistant', content: 'Here you go.', preferences_changed: false, created: null, results: null}, preferences_changed: false,
            })});
        });
        await page.getByTestId('retry-button').click();
        await expect(page.getByTestId('chat-error')).toHaveCount(0);
        expect(posts).toBe(1);
    });

    test('an error that fits in two lines is shown whole, and has no Details button', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await loadPanel(page, 'priorities=57');
        await page.route(/\/api\/agent\/conversations\/\d+\/$/, (route) => (route.request().method() === 'POST'
            ? route.fulfill({status: 429, contentType: 'application/json', body: JSON.stringify({error: {type: 'rate_limited', message: 'Too many messages. Wait a minute.'}})})
            : route.fallback()));
        await send(page, 'Hello again');
        await expect(page.getByTestId('chat-error')).toHaveText('Too many messages. Wait a minute.');
        const reading = await settled(page);
        expect(reading.collapsible).toBe(false);
        await expect(page.getByTestId('chat-notices-details')).toHaveCount(0);
    });

    // Visual review round 2: what takes a press, not what is drawn. A folded status notice used to be painted over
    // Retry's top 16px, and the expanded toggle's left third was where Retry appears after a collapse.
    const SHORT_ERROR = 'The assistant is unavailable right now. Please try again.';
    const failSendsWith = async (page: Page, message: string, counter: {posts: number}) => {
        await page.route(/\/api\/agent\/conversations\/\d+\/$/, (route) => {
            if (route.request().method() !== 'POST') return route.fallback();
            counter.posts += 1;
            return route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: {type: 'assistant_unavailable', message}})});
        });
    };
    const statusUnavailable = (page: Page) => page.route('**/api/agent/assistant-status/', (route) => route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({state: 'temporarily_unavailable', actions: ['retry'], checked_at: '2026-10-13T00:00:00Z'}),
    }));
    for (const size of [
        {width: 1280, height: 900}, {width: 1024, height: 800}, {width: 800, height: 600}, {width: 768, height: 1024},
        {width: 375, height: 667}, {width: 320, height: 640},
    ]) {
        for (const error of [{name: '57-character', text: SHORT_ERROR}, {name: '600-character', text: LONG_ERROR}]) {
            test(`with a folded status notice the whole of Retry and Details takes a press, ${error.name} error, ${size.width}x${size.height}`, async ({page}) => {
                await page.setViewportSize(size);
                const counter = {posts: 0};
                await statusUnavailable(page);
                await loadPanel(page, 'priorities=57');
                await expect(page.getByTestId('assistant-status-notice')).toBeVisible();
                await failSendsWith(page, error.text, counter);
                await send(page, 'And which of them are hiring now?');
                await expect(page.getByTestId('retry-button')).toBeVisible();
                await expect(page.getByTestId('chat-notices-details')).toBeVisible();
                await settled(page);
                for (const id of ['retry-button', 'chat-notices-details']) {
                    const box = (await page.getByTestId(id).boundingBox())!;
                    // Which element is under a point 4px inside each edge, over the whole width of the button.
                    for (const y of [box.y + 4, box.y + box.height - 4]) {
                        for (const x of [box.x + 4, box.x + box.width / 2, box.x + box.width - 4]) {
                            const owner = await page.evaluate(([px, py]) => {
                                const hit = document.elementFromPoint(px, py);
                                return hit?.closest('[data-testid]')?.getAttribute('data-testid') ?? null;
                            }, [x, y]);
                            expect(owner, `${id}: the element under (${Math.round(x)}, ${Math.round(y)}) of its ${Math.round(box.height)}px box`).toBe(id);
                        }
                    }
                }
                // A real press 4px under Retry's top edge sends the turn again.
                const retry = (await page.getByTestId('retry-button').boundingBox())!;
                const before = counter.posts;
                await page.mouse.click(retry.x + retry.width / 2, retry.y + 4);
                await expect.poll(() => counter.posts, {message: 'a press at the top edge of Retry sends the request'}).toBe(before + 1);
            });
        }
    }

    for (const size of [
        {width: 1280, height: 900}, {width: 800, height: 600}, {width: 768, height: 1024}, {width: 375, height: 667}, {width: 320, height: 640},
    ]) {
        test(`a second press where Hide details was never presses Retry, ${size.width}x${size.height}`, async ({page}) => {
            await page.setViewportSize(size);
            const counter = {posts: 0};
            await loadPanel(page, 'priorities=57');
            await failSendsWith(page, LONG_ERROR, counter);
            await send(page, 'And which of them are hiring now?');
            await expect(page.getByTestId('chat-notices-details')).toBeVisible();
            await settled(page);
            const details = page.getByTestId('chat-notices-details');
            const collapsedBox = (await details.boundingBox())!;
            await details.click();
            await expect(details).toHaveText('Hide details');
            await settled(page);
            const expandedBox = (await details.boundingBox())!;
            // The expanded toggle stays inside the box Details had (a pixel or two for the wrapped label's padding).
            expect(expandedBox.x, 'the toggle does not reach left of where Details was').toBeGreaterThanOrEqual(collapsedBox.x - 4);
            expect(expandedBox.x + expandedBox.width).toBeCloseTo(collapsedBox.x + collapsedBox.width, 0);
            expect(expandedBox.y + expandedBox.height).toBeCloseTo(collapsedBox.y + collapsedBox.height, 0);
            const sent = counter.posts;
            // Two presses at one point, 10px inside the toggle's left edge, 180ms apart: collapse, then expand again.
            const x = expandedBox.x + 10;
            const y = expandedBox.y + expandedBox.height / 2;
            await page.mouse.click(x, y);
            await page.waitForTimeout(180);
            await page.mouse.click(x, y);
            await page.waitForTimeout(250);
            expect(counter.posts, 'the second press reached no Retry').toBe(sent);
            await expect(details).toHaveText('Hide details');
        });
    }

    for (const size of [{width: 768, height: 1024}, {width: 800, height: 600}]) {
        test(`in the 300px drawer a one-sentence error reads whole with no Details, ${size.width}x${size.height}`, async ({page}) => {
            await page.setViewportSize(size);
            await loadPanel(page, 'priorities=57');
            await failSendsWith(page, SHORT_ERROR, {posts: 0});
            await send(page, 'And which of them are hiring now?');
            await expect(page.getByTestId('chat-error')).toContainText('Please try again.');
            const reading = await settled(page);
            expect(reading.noticesHeight, 'the compact row stays inside 56px').toBeLessThanOrEqual(56);
            const text = await page.getByTestId('chat-error').locator('.chat-error-text').evaluate((el) => ({
                cut: el.scrollHeight > el.clientHeight + 1, lines: Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight)),
            }));
            expect(text.cut, 'nothing of the sentence is cut').toBe(false);
            expect(text.lines).toBeLessThanOrEqual(3);
            await expect(page.getByTestId('chat-notices-details')).toHaveCount(0);
        });
    }
});

// Visual review round 1 of #483b-2: where a notice arrives, the jump pill against a notice's controls, the footer's
// Details toggle, the compact error row and the plain banner's close button.
test.describe('notices arrive on screen, the pill steps aside, the footer toggle stays reachable (issue #483)', () => {
    const SIZES = [{width: 1280, height: 900}, {width: 1024, height: 800}, {width: 375, height: 667}, {width: 320, height: 640}];
    const FIELDS: Array<{path: string; old: unknown; new: unknown}> = [
        {path: 'compensation.minimum_salary', old: 120000, new: 150000},
        {path: 'work_location.modes', old: [], new: ['remote', 'hybrid']},
        {path: 'industry', old: [], new: ['fintech']},
        {path: 'funding_stage', old: [], new: ['series_b']},
        {path: 'work_location.countries', old: [], new: ['US', 'CA']},
        {path: 'roles.titles', old: [], new: ['Staff Engineer']},
        {path: 'work_location.max_in_office_days', old: null, new: 2},
        {path: 'compensation.equity_minimum_percent', old: null, new: 0.5},
        {path: 'compensation.minimum_total_compensation', old: null, new: 250000},
        {path: 'notes', old: '', new: 'prefers remote-first teams'},
    ];
    const proposalOf = (count: number) => ({
        id: `prop-${count}`, scope: 'account', changes: FIELDS.slice(0, count), change_count: count, base_revision: 2, unsupported_criteria: [], currency: 'USD',
        token: {patch: {set: {notes: 'prefers remote-first teams'}}, scope: 'account', base_revision: 2},
    });
    const LONG_ERROR = `${'The assistant could not finish this reply because the provider stopped answering. '.repeat(8)}`.slice(0, 600);
    const SHORT_ERROR = 'The assistant is unavailable right now. Try again shortly.';
    const SEND = /\/api\/agent\/conversations\/\d+\/$/;
    const jsonRoute = (page: Page, url: string, body: unknown, status = 200) => page.route(url, (route) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)}));
    // A real reply takes longer than the scroll events of the reader's own send; answering at once under load races them.
    const replyWith = (page: Page, extra: Record<string, unknown>, content = 'I can save these as your priorities.') => page.route(SEND, async (route) => {
        if (route.request().method() !== 'POST') return route.fallback();
        await new Promise((resolve) => setTimeout(resolve, 400));
        return route.fulfill({status: 201, contentType: 'application/json', body: JSON.stringify({
            message: {id: 9000 + Math.floor(Math.random() * 90000), role: 'assistant', content, preferences_changed: false, created: null, results: null},
            preferences_changed: false, ...extra,
        })});
    });
    const failWith = (page: Page, message: string, status = 503, type = 'assistant_unavailable') => page.route(SEND, (route) => (route.request().method() === 'POST'
        ? route.fulfill({status, contentType: 'application/json', body: JSON.stringify({error: {type, message}})})
        : route.fallback()));
    const send = async (page: Page, text: string) => {
        await page.locator('textarea[aria-label="Message"]').fill(text);
        await page.getByRole('button', {name: 'Send message'}).click();
    };
    const statusOn = (page: Page) => page.route('**/api/agent/assistant-status/', (route) => route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({state: 'temporarily_unavailable', actions: ['retry'], checked_at: '2026-10-13T00:00:00Z'}),
    }));
    const settleLayout = async (page: Page) => {
        await page.evaluate(() => document.fonts.ready.then(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))));
        await page.waitForTimeout(150);
    };
    const fromEnd = (page: Page) => page.getByRole('log').evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop);
    // What the reader can see of the notice in the transcript, with no scrolling of ours.
    const arrival = (page: Page) => page.evaluate(() => {
        const log = document.querySelector('[role="log"]') as HTMLElement;
        const blocks = document.querySelectorAll('[data-testid="transcript-notice"]');
        // The newest notice is the last block.
        const notice = blocks[blocks.length - 1] as HTMLElement;
        const box = log.getBoundingClientRect();
        const inside = (el: Element) => { const r = el.getBoundingClientRect(); return r.top >= box.top - 0.5 && r.bottom <= box.bottom + 0.5; };
        const title = notice.querySelector('.pref-change-summary') as HTMLElement;
        const rows = Array.from(notice.querySelectorAll('.pref-change-item'));
        const apply = notice.querySelector('[data-testid="preference-apply-button"]') as HTMLElement | null;
        return {
            noticeTop: notice.getBoundingClientRect().top - box.top, titleInside: inside(title), rows: rows.length, rowsInside: rows.filter(inside).length,
            applyInside: apply ? inside(apply) : null, fromEnd: log.scrollHeight - log.clientHeight - log.scrollTop, logHeight: log.clientHeight, noticeHeight: notice.getBoundingClientRect().height,
        };
    });

    for (const size of SIZES) {
        test(`a proposal arrives with its title on screen, then Apply is reachable, and a failed Apply stays at the end, ${size.width}x${size.height}`, async ({page}) => {
            test.setTimeout(120_000);
            await page.setViewportSize(size);
            await loadPanel(page, 'priorities=57');
            // The reader is following the transcript: the loaded history has been scrolled to its end.
            await expect.poll(() => fromEnd(page), {message: 'the loaded history is at its end'}).toBeLessThanOrEqual(2);
            for (const count of [2, 5, 10]) {
                await page.unroute(SEND).catch(() => undefined);
                await replyWith(page, {preference_proposal: proposalOf(count)});
                await send(page, `Please set ${count} things.`);
                await expect(page.getByTestId('preference-proposal-notice')).toBeVisible();
                await settleLayout(page);
                // Read again until two readings 250ms apart agree: a smooth scroll that was already under way has ended.
                let first = await arrival(page);
                for (let i = 0; i < 20; i += 1) {
                    await page.waitForTimeout(250);
                    const again = await arrival(page);
                    const same = JSON.stringify(again) === JSON.stringify(first);
                    first = again;
                    if (same) break;
                }
                expect(first.titleInside, `${count} fields: the title is fully on screen on arrival ${JSON.stringify(first)}`).toBe(true);
                // Two complete rows where the transcript has the room they need (this host's font: 281px at 320x640; CI's wider fallback font
                // leaves 264px, where one row and the title fit), one row at least anywhere.
                expect(first.rowsInside, `${count} fields: complete rows on screen ${JSON.stringify(first)}`).toBeGreaterThanOrEqual(first.logHeight >= 280 ? Math.min(count, 2) : 1);
                if (first.fromEnd > 2) expect(first.noticeTop, `${count} fields: the notice's top is just below the transcript's top`).toBeGreaterThanOrEqual(-1);
                // Wheel to the end with the pointer over the transcript: Apply is on screen and takes a real click.
                const box = (await page.getByRole('log').boundingBox())!;
                await page.mouse.move(box.x + box.width / 2, box.y + 40);
                for (let i = 0; i < 60 && (await fromEnd(page)) > 1; i += 1) {
                    await page.mouse.wheel(0, 120);
                    await page.waitForTimeout(40);
                }
                await settleLayout(page);
                expect(await fromEnd(page)).toBeLessThanOrEqual(2);
                expect((await arrival(page)).applyInside, `${count} fields: Apply is on screen at the end`).toBe(true);
                await page.getByTestId('preference-proposal-dismiss-button').click();
                await expect(page.getByTestId('transcript-notice')).toHaveCount(0);
            }
            // A failed Apply: the reader stays at the end with the error and Apply on screen.
            await page.unroute(SEND).catch(() => undefined);
            await replyWith(page, {preference_proposal: proposalOf(5)});
            await send(page, 'Once more, five things.');
            await expect(page.getByTestId('preference-proposal-notice')).toBeVisible();
            await settleLayout(page);
            await page.getByRole('log').evaluate((el) => { el.scrollTop = el.scrollHeight; });
            await page.route('**/api/agent/preferences/apply/', (route) => route.fulfill({
                status: 500, contentType: 'application/json', body: JSON.stringify({error: {type: 'server_error', message: 'The proposal could not be applied. Please try again.'}}),
            }));
            await page.getByTestId('preference-apply-button').click();
            await expect(page.getByTestId('preference-proposal-error')).toBeVisible();
            await settleLayout(page);
            const failed = await arrival(page);
            expect(failed.fromEnd, 'a failed Apply keeps the reader at the end').toBeLessThanOrEqual(2);
            expect(failed.applyInside, 'Apply is still on screen').toBe(true);
        });

        test(`the jump pill never covers or takes the press of a control of a notice, ${size.width}x${size.height}`, async ({page}) => {
            test.setTimeout(150_000);
            await page.setViewportSize(size);
            await loadPanel(page, 'priorities=57');
            await expect.poll(() => fromEnd(page), {message: 'the loaded history is at its end'}).toBeLessThanOrEqual(2);
            await replyWith(page, {preference_proposal: proposalOf(5)});
            await send(page, 'Please set five things.');
            await expect(page.getByTestId('preference-proposal-notice')).toBeVisible();
            await page.unroute(SEND);
            // A failed Apply adds the error message to the notice; two more turns put it in mid-history.
            await page.route('**/api/agent/preferences/apply/', (route) => route.fulfill({
                status: 500, contentType: 'application/json', body: JSON.stringify({error: {type: 'server_error', message: 'The proposal could not be applied. Please try again.'}}),
            }));
            await page.getByTestId('preference-apply-button').click();
            await expect(page.getByTestId('preference-proposal-error')).toBeVisible();
            await page.unroute('**/api/agent/preferences/apply/');
            for (const text of ['A question after it.', 'And another one.']) {
                await replyWith(page, {}, 'Noted, here is some more detail so the history grows past the notice.');
                await send(page, text);
                await expect(page.getByRole('log').getByText('here is some more detail').last()).toBeVisible();
                await page.unroute(SEND);
            }
            await settleLayout(page);
            // Every control of the notice at every scroll position (4px steps): none is covered by the pill.
            const sweep = await page.evaluate(async () => {
                const log = document.querySelector('[role="log"]') as HTMLElement;
                const notice = log.querySelector('[data-testid="transcript-notice"]') as HTMLElement;
                const controls = Array.from(notice.querySelectorAll('button, a[href]'));
                let worst = 0; let shownNoticeOff = 0; let shownNoticeOn = 0; let positions = 0;
                for (let y = 0; y <= log.scrollHeight - log.clientHeight; y += 4) {
                    log.scrollTo({top: y, behavior: 'instant' as ScrollBehavior});
                    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
                    positions += 1;
                    const pill = document.querySelector('#job-search-chat .chat-jump-row .btn');
                    if (!pill) continue;
                    const pr = pill.getBoundingClientRect(); const lr = log.getBoundingClientRect(); const nr = notice.getBoundingClientRect();
                    if (nr.bottom > lr.top && nr.top < lr.bottom) shownNoticeOn += 1; else shownNoticeOff += 1;
                    for (const c of controls) {
                        const r = c.getBoundingClientRect();
                        const top = Math.max(r.top, lr.top); const bottom = Math.min(r.bottom, lr.bottom);
                        if (bottom - top < 1) continue;
                        const w = Math.min(r.right, pr.right) - Math.max(r.left, pr.left); const h = Math.min(bottom, pr.bottom) - Math.max(top, pr.top);
                        if (w > 0 && h > 0) worst = Math.max(worst, (w * h) / (r.width * r.height));
                    }
                }
                return {worst, shownNoticeOff, shownNoticeOn, positions, controls: controls.length};
            });
            // eslint-disable-next-line no-console
            console.log(`pill sweep ${size.width}x${size.height}: ${JSON.stringify(sweep)}`);
            expect(sweep.controls).toBeGreaterThanOrEqual(5);
            expect(sweep.worst, 'the largest share of any control of the notice that the pill covers').toBe(0);
            expect(sweep.shownNoticeOff, 'the pill still shows with the notice off screen').toBeGreaterThan(0);
            // A real press where the pill used to be: Apply, centred 40px above the transcript's bottom edge, applies.
            await jsonRoute(page, '**/api/agent/preferences/apply/', {applied: true, scope: 'account', revision: 3, changes: FIELDS.slice(0, 5), undo: {expected_revision: 3, document: {}}});
            await page.getByRole('log').evaluate((log) => {
                const apply = log.querySelector('[data-testid="preference-apply-button"]') as HTMLElement;
                const r = apply.getBoundingClientRect(); const lr = log.getBoundingClientRect();
                log.scrollTop += (r.top + r.height / 2) - (lr.bottom - 40);
            });
            await settleLayout(page);
            const point = await page.getByTestId('preference-apply-button').evaluate((el) => {
                const r = el.getBoundingClientRect(); const x = r.left + r.width / 2; const y = r.top + r.height / 2;
                const top = document.elementFromPoint(x, y);
                return {x, y, own: top === el || el.contains(top)};
            });
            expect(point.own, 'the control, not the pill, is under the pointer').toBe(true);
            await page.mouse.click(point.x, point.y);
            await expect(page.getByTestId('preference-change-notice')).toBeVisible();
            // Keyboard: Tab through the applied notice's controls; none is under the pill.
            await page.getByRole('log').evaluate((el) => { el.scrollTop = 0; });
            await page.getByTestId('conversation-more').focus();
            const walked: string[] = [];
            for (let i = 0; i < 30 && walked.length < 2; i += 1) {
                await page.keyboard.press('Tab');
                await settleLayout(page);
                const read = await page.evaluate(() => {
                    const el = document.activeElement as HTMLElement; const notice = document.querySelector('[data-testid="transcript-notice"]');
                    if (!notice || !notice.contains(el)) return null;
                    const pill = document.querySelector('#job-search-chat .chat-jump-row .btn'); const r = el.getBoundingClientRect();
                    let covered = 0;
                    if (pill) { const p = pill.getBoundingClientRect(); const w = Math.min(r.right, p.right) - Math.max(r.left, p.left); const h = Math.min(r.bottom, p.bottom) - Math.max(r.top, p.top); if (w > 0 && h > 0) covered = (w * h) / (r.width * r.height); }
                    const lr = (document.querySelector('[role="log"]') as HTMLElement).getBoundingClientRect();
                    return {label: el.getAttribute('aria-label') || el.textContent, covered, inside: r.top >= lr.top - 0.5 && r.bottom <= lr.bottom + 0.5};
                });
                if (read) { walked.push(String(read.label)); expect(read.covered, `${read.label} is clear of the pill`).toBe(0); expect(read.inside, `${read.label} is inside the transcript`).toBe(true); }
            }
            expect(walked.length).toBe(2);
        });

        test(`a second proposal that replaces an open one arrives with its title on screen, ${size.width}x${size.height}`, async ({page}) => {
            test.setTimeout(120_000);
            await page.setViewportSize(size);
            await loadPanel(page, 'priorities=57');
            await expect.poll(() => fromEnd(page), {message: 'the loaded history is at its end'}).toBeLessThanOrEqual(2);
            await replyWith(page, {preference_proposal: proposalOf(10)}, 'The first proposal comes with this reply.');
            await send(page, 'Please set ten things.');
            await expect(page.getByTestId('preference-proposal-notice')).toBeVisible();
            await settleLayout(page);
            // The reader leaves it open and asks again; the next reply brings another proposal under itself.
            await page.unroute(SEND);
            await replyWith(page, {preference_proposal: {...proposalOf(10), id: 'prop-b'}}, 'The second proposal comes with this reply.');
            await send(page, 'Actually, think again.');
            await expect(page.getByRole('log').getByText('The second proposal comes with this reply.')).toBeVisible();
            await settleLayout(page);
            let reading = await arrival(page);
            for (let i = 0; i < 20; i += 1) {
                await page.waitForTimeout(250);
                const again = await arrival(page);
                const same = JSON.stringify(again) === JSON.stringify(reading);
                reading = again;
                if (same) break;
            }
            await expect(page.getByTestId('transcript-notice')).toHaveCount(1);
            await expect(page.getByTestId('transcript-notice').locator('xpath=preceding-sibling::*[1]')).toContainText('The second proposal comes with this reply.');
            expect(reading.titleInside, `the second proposal's title is on screen on arrival ${JSON.stringify(reading)}`).toBe(true);
            if (reading.fromEnd > 2) expect(reading.noticeTop, 'its top is just below the transcript\'s top').toBeGreaterThanOrEqual(-1);
        });

        test(`an applied notice stays under its own reply when a later reply brings a proposal, the pill shows for the newest notice off screen, and New conversation clears both, ${size.width}x${size.height}`, async ({page}) => {
            test.setTimeout(150_000);
            await page.setViewportSize(size);
            await loadPanel(page, 'priorities=57');
            await expect.poll(() => fromEnd(page), {message: 'the loaded history is at its end'}).toBeLessThanOrEqual(2);
            await replyWith(page, {preference_proposal: proposalOf(2)}, 'The first proposal comes with this reply.');
            await send(page, 'Please set two things.');
            await expect(page.getByTestId('preference-proposal-notice')).toBeVisible();
            await page.unroute(SEND);
            await jsonRoute(page, '**/api/agent/preferences/apply/', {applied: true, scope: 'account', revision: 3, changes: FIELDS.slice(0, 2), undo: {expected_revision: 3, document: {}}});
            await page.getByTestId('preference-apply-button').click();
            await expect(page.getByTestId('preference-change-notice')).toBeVisible();
            await page.unroute('**/api/agent/preferences/apply/');
            await replyWith(page, {preference_proposal: {...proposalOf(2), id: 'prop-b'}}, 'The second proposal comes with this reply.');
            await send(page, 'And two more.');
            await expect(page.getByTestId('preference-proposal-notice')).toBeVisible();
            await page.unroute(SEND);
            await settleLayout(page);
            const blocks = page.getByTestId('transcript-notice');
            await expect(blocks).toHaveCount(2);
            await expect(blocks.nth(0).getByTestId('preference-change-notice'), 'the applied notice and its Undo').toBeVisible();
            await expect(blocks.nth(0).locator('xpath=preceding-sibling::*[1]')).toContainText('The first proposal comes with this reply.');
            await expect(blocks.nth(1).getByTestId('preference-proposal-notice')).toBeAttached();
            await expect(blocks.nth(1).locator('xpath=preceding-sibling::*[1]')).toContainText('The second proposal comes with this reply.');
            // The reader scrolls to the very top with the wheel: both notices are off screen, so the pill is there to bring them back.
            const box = (await page.getByRole('log').boundingBox())!;
            await page.mouse.move(box.x + box.width / 2, box.y + 40);
            for (let i = 0; i < 200 && (await page.getByRole('log').evaluate((el) => el.scrollTop)) > 0; i += 1) {
                await page.mouse.wheel(0, -1200);
                await page.waitForTimeout(30);
            }
            await settleLayout(page);
            expect(await page.getByRole('log').evaluate((el) => el.scrollTop)).toBe(0);
            await expect(page.locator('#job-search-chat .chat-jump-row .btn'), 'the pill shows while the newest notice is off screen').toBeVisible();
            // New conversation with notices showing leaves none of them, and no anchor.
            await page.route('**/api/agent/conversations/*/reset/**', (route) => route.fulfill({
                status: 200, contentType: 'application/json',
                body: JSON.stringify({id: 43, active: true, created: null, modified: null, messages: [], preferences_changed: false}),
            }));
            await page.getByTestId('conversation-more').click();
            await page.getByTestId('conversation-new').click();
            await waitForConfirmArmed(page);
            await page.getByTestId('confirm-action').click();
            await expect(page.getByTestId('empty-history')).toBeVisible();
            await expect(page.getByTestId('transcript-notice')).toHaveCount(0);
            await expect(page.getByRole('log')).not.toHaveAttribute('data-notice-anchor', /.*/);
        });

        test(`the plain banner's close button is a 44px target and keeps its name, ${size.width}x${size.height}`, async ({page}) => {
            await page.setViewportSize(size);
            await loadPanel(page, 'priorities=57');
            await replyWith(page, {preferences_changed: true}, 'Noted, I will remember that.');
            await send(page, 'I also prefer smaller teams.');
            const close = page.getByRole('button', {name: 'Dismiss preference notice'});
            await expect(close).toBeVisible();
            await settleLayout(page);
            const box = (await close.boundingBox())!;
            expect(Math.round(box.width), 'the close button is 44px wide').toBeGreaterThanOrEqual(44);
            expect(Math.round(box.height), 'the close button is 44px high').toBeGreaterThanOrEqual(44);
            await expect(close).toHaveClass(/pref-change-dismiss/);
            await close.click();
            await expect(page.getByRole('status', {name: 'Preference update'})).toHaveCount(0);
        });

        test(`Details stays on screen and in the same place after it is pressed, by mouse, Enter and Space, ${size.width}x${size.height}`, async ({page}) => {
            test.setTimeout(180_000);
            await page.setViewportSize(size);
            await loadPanel(page, 'priorities=57');
            const scenarios = [
                {name: 'a status notice plus the 600-character error', status: true, error: LONG_ERROR},
                {name: 'the 600-character error alone', status: false, error: LONG_ERROR},
                {name: 'a status notice plus the 57-character error', status: true, error: SHORT_ERROR},
            ];
            let first = true;
            for (const scenario of scenarios) {
                await page.unroute(SEND).catch(() => undefined);
                await page.unroute('**/api/agent/assistant-status/').catch(() => undefined);
                if (scenario.status) await statusOn(page);
                if (!first) await page.reload();
                first = false;
                await expect(page.getByRole('log').locator('article').first()).toBeVisible();
                await failWith(page, scenario.error);
                await send(page, 'And which of them are hiring now?');
                await expect(page.getByTestId('chat-error')).toBeVisible();
                const details = page.getByTestId('chat-notices-details');
                await expect(details).toHaveText('Details');
                await settleLayout(page);
                const collapsedBox = (await details.boundingBox())!;
                const collapsedLog = await page.getByRole('log').evaluate((el) => el.clientHeight);
                const collapsedArea = (await page.getByTestId('chat-footer-notices').boundingBox())!.height;
                await details.evaluate((el) => { (el as unknown as {__same: boolean}).__same = true; });
                for (const how of ['mouse', 'Enter', 'Space'] as const) {
                    const label = `${scenario.name}, ${how}`;
                    if (how === 'mouse') await details.click();
                    else {
                        await page.locator('textarea[aria-label="Message"]').focus();
                        await page.keyboard.press('Shift+Tab');
                        await expect(details).toBeFocused();
                        await page.keyboard.press(how === 'Enter' ? 'Enter' : 'Space');
                    }
                    await expect(details).toHaveText('Hide details');
                    await settleLayout(page);
                    const read = await page.evaluate(() => {
                        const area = document.querySelector('[data-testid="chat-footer-notices"]') as HTMLElement;
                        const toggle = document.querySelector('[data-testid="chat-notices-details"]') as HTMLElement;
                        const body = area.querySelector('.chat-notices-body') as HTMLElement;
                        const text = area.querySelector('.chat-error-text') as HTMLElement;
                        const form = document.querySelector('#job-search-chat form') as HTMLElement;
                        const t = toggle.getBoundingClientRect(); const a = area.getBoundingClientRect(); const b = body.getBoundingClientRect(); const e = text.getBoundingClientRect();
                        const lineHeight = parseFloat(getComputedStyle(text).lineHeight);
                        const visible = Math.max(0, Math.min(e.bottom, b.bottom) - Math.max(e.top, b.top));
                        const inBody = Array.from(body.querySelectorAll('button')).map((el) => (el as HTMLElement).getBoundingClientRect().top);
                        const status = area.querySelector('[data-testid="assistant-status-notice"]');
                        return {
                            toggleInside: t.top >= -0.5 && t.bottom <= window.innerHeight + 0.5 && t.left >= -0.5 && t.right <= window.innerWidth + 0.5
                                && t.top >= a.top - 0.5 && t.bottom <= a.bottom + 0.5 && t.right <= a.right + 0.5,
                            toggleLast: area.lastElementChild === toggle, same: (toggle as unknown as {__same?: boolean}).__same === true,
                            right: t.right, top: t.top, errorLinesVisible: visible / lineHeight, errorWholeVisible: e.bottom <= b.bottom + 0.5,
                            areaHeight: a.height, tabOrderIsVisualOrder: inBody.every((top, i) => i === 0 || inBody[i - 1] <= top + 0.5) && t.top >= b.bottom - 0.5,
                            errorBeforeStatus: status ? !!(text.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING) : true,
                            composerInViewport: form.getBoundingClientRect().bottom <= window.innerHeight + 0.5 && form.getBoundingClientRect().top >= -0.5,
                            focusOnToggle: document.activeElement === toggle, focusRing: toggle.matches(':focus-visible'),
                        };
                    });
                    expect(read.toggleInside, `${label}: Hide details is inside the viewport and the notice area, with no scrolling`).toBe(true);
                    expect(read.toggleLast, `${label}: the toggle is the last row of the area`).toBe(true);
                    expect(read.same, `${label}: the toggle is the same element`).toBe(true);
                    expect(read.errorBeforeStatus, `${label}: the error comes before the status notice`).toBe(true);
                    expect(read.tabOrderIsVisualOrder, `${label}: the tab order is the visual order`).toBe(true);
                    expect(read.composerInViewport, `${label}: the composer is inside the viewport`).toBe(true);
                    if (scenario.error === LONG_ERROR) expect(read.errorLinesVisible, `${label}: two complete lines of the error are on screen at first`).toBeGreaterThanOrEqual(2);
                    else expect(read.errorWholeVisible, `${label}: the whole short error is on screen`).toBe(true);
                    expect(Math.abs(read.right - (collapsedBox.x + collapsedBox.width)), `${label}: the toggle's right edge does not move`).toBeLessThanOrEqual(1);
                    expect(Math.abs(read.top + 44 - (collapsedBox.y + collapsedBox.height)), `${label}: the toggle's bottom stays within 10px`).toBeLessThanOrEqual(10);
                    if (how !== 'mouse') {
                        expect(read.focusOnToggle, `${label}: the focus stays on the toggle`).toBe(true);
                        expect(read.focusRing, `${label}: the focus ring shows after a key press`).toBe(true);
                    }
                    // A second press without moving the pointer collapses, and the heights return.
                    if (how === 'mouse') await details.click();
                    else await page.keyboard.press(how === 'Enter' ? 'Enter' : 'Space');
                    await expect(details).toHaveText('Details');
                    await settleLayout(page);
                    expect(await page.getByRole('log').evaluate((el) => el.clientHeight), `${label}: the transcript returns to its height`).toBe(collapsedLog);
                    expect(Math.abs((await page.getByTestId('chat-footer-notices').boundingBox())!.height - collapsedArea), `${label}: the area returns to its height`).toBeLessThanOrEqual(1);
                }
            }
        });

        test(`the compact error row reads what fits and has Details only when it cuts, ${size.width}x${size.height}`, async ({page}) => {
            test.setTimeout(120_000);
            await page.setViewportSize(size);
            await loadPanel(page, 'priorities=57');
            const readRow = () => page.evaluate(() => {
                const area = document.querySelector('[data-testid="chat-footer-notices"]') as HTMLElement;
                const text = area.querySelector('.chat-error-text') as HTMLElement;
                return {
                    height: area.getBoundingClientRect().height, cut: text.scrollHeight > text.clientHeight + 1,
                    lines: Math.round(text.getBoundingClientRect().height / parseFloat(getComputedStyle(text).lineHeight)),
                    details: document.querySelectorAll('[data-testid="chat-notices-details"]').length,
                    transcript: (document.querySelector('[role="log"]') as HTMLElement).clientHeight,
                };
            });
            // The 57-character failed send reads whole wherever two lines hold it, and has Details only where they do not.
            await failWith(page, SHORT_ERROR);
            await send(page, 'And which of them are hiring now?');
            await expect(page.getByTestId('retry-button')).toBeVisible();
            await settleLayout(page);
            const short = await readRow();
            expect(short.height, 'the compact row is at most 56px').toBeLessThanOrEqual(56);
            expect(short.details, 'Details exists exactly when the text is cut').toBe(short.cut ? 1 : 0);
            expect(short.lines).toBeLessThanOrEqual(2);
            if (size.width >= 1024) expect(short.cut, 'the sentence fits in two lines').toBe(false);
            expect(await page.getByTestId('chat-error').textContent()).toContain(SHORT_ERROR);
            // 120 and 121 characters (no failed turn, so no Retry) give the same transcript at every size.
            const heights: number[] = [];
            for (const length of [120, 121]) {
                await page.unroute(SEND);
                await page.reload();
                await expect(page.getByRole('log').locator('article').first()).toBeVisible();
                await failWith(page, 'x'.repeat(length).replace(/x{9}/g, 'wordsword '), 429, 'rate_limited');
                await send(page, 'And which of them are hiring now?');
                await expect(page.getByTestId('chat-error')).toBeVisible();
                await settleLayout(page);
                heights.push((await readRow()).transcript);
            }
            expect(heights[0], '120 and 121 characters leave the same transcript').toBe(heights[1]);
            // The 600-character error is cut and has Details.
            await page.unroute(SEND);
            await page.reload();
            await expect(page.getByRole('log').locator('article').first()).toBeVisible();
            await failWith(page, LONG_ERROR);
            await send(page, 'And which of them are hiring now?');
            await expect(page.getByTestId('chat-error')).toBeVisible();
            await settleLayout(page);
            const long = await readRow();
            expect(long).toMatchObject({cut: true, details: 1});
            expect(long.height).toBeLessThanOrEqual(56);
        });
    }
});
