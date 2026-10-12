// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Viewport regression harness for the job-search chat (issue #431). Replaces the
// manual-only viewport/zoom/keyboard/IME verification recorded in
// docs/rollout-gates.md §4 with automated cross-browser assertions.
import {test, expect, Page} from '@playwright/test';

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
    expect(reading.composerHit, `${label}: nothing covers the composer`).toBe(true);
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
        expect(reading.bodyScrollTop).toBeLessThan(40);
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
                await settledPanel(page);
                await setViewport(page, {inset: 250});
                await expect.poll(async () => (await readPanel(page)).logHeight).toBeLessThan(60);
                await scrollUpToPill(page);
                await pressCentre(page);
            });
        }
    });

    for (const size of [{width: 320, height: 420}, {width: 320, height: 640}]) {
        for (const [trigger, label] of [['conversation-new', 'new'], ['conversation-delete', 'delete']]) {
            test(`the ${label} confirm shows its action and Cancel together on open, ${size.width}x${size.height}`, async ({page}) => {
                await page.setViewportSize(size);
                await loadPanel(page, 'priorities=57');
                await settledPanel(page);
                await page.getByTestId('conversation-more').click();
                await page.getByTestId(trigger).click();
                await expect(page.getByTestId('confirm-panel')).toBeVisible();
                const reading = await settledPanel(page);
                expectOneScroller(reading, 'with the confirmation open');
                for (const id of ['confirm-action', 'confirm-cancel']) {
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

    test('after a failed delete at 320x420 the focused retry is topmost, not under the Cancel row', async ({page}) => {
        await page.setViewportSize({width: 320, height: 420});
        await loadPanel(page, 'priorities=57');
        // Registered after the catch-all mock so it wins for the delete call.
        await page.route('**/api/agent/conversations/*/delete/**', (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
        await page.getByTestId('conversation-more').click();
        await page.getByTestId('conversation-delete').click();
        await page.getByTestId('confirm-action').click();
        const retry = page.getByTestId('confirm-action');
        await expect(page.getByRole('alert').filter({hasText: /delete/i})).toBeVisible();
        await expect(retry).toBeFocused();
        const reading = await settledPanel(page);
        expectOneScroller(reading, 'after the failed delete');
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
