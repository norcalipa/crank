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

                    // Wait for deterministic layout: the measured card height is
                    // applied and web fonts have finished loading.
                    await page.waitForFunction(() => {
                        const card = document.getElementById('job-search-chat')?.firstElementChild as HTMLElement | null;
                        return !!card && card.style.height !== '' && document.fonts.status === 'loaded';
                    });
                    await page.evaluate(() => new Promise<void>((resolve) => {
                        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
                    }));

                    const layout = await page.evaluate(() => {
                        const cardBody = document.querySelector('#job-search-chat .card-body') as HTMLElement;
                        const history = document.querySelector('[role="log"][aria-label="Message history"]') as HTMLElement;
                        const noticeEl = document.querySelector('[data-testid="assistant-status-notice"]') as HTMLElement;
                        const composer = document.querySelector('textarea[aria-label="Message"]') as HTMLElement;
                        const viewportHeight = window.innerHeight;
                        const withinViewport = (el: HTMLElement) => {
                            const r = el.getBoundingClientRect();
                            return r.top >= -1 && r.bottom <= viewportHeight + 1;
                        };
                        return {
                            cardBodyScrolls: cardBody.scrollHeight > cardBody.clientHeight + 1,
                            cardBodyOverflowY: getComputedStyle(cardBody).overflowY,
                            historyOverflowY: getComputedStyle(history).overflowY,
                            noticeWithinViewport: withinViewport(noticeEl),
                            composerWithinViewport: withinViewport(composer),
                        };
                    });

                    expect(layout.cardBodyOverflowY, 'card body must not become a scroll container').toBe('visible');
                    expect(layout.cardBodyScrolls, 'card body must not scroll; history yields instead').toBe(false);
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

const expectPanelAtLatest = async (page: Page) => {
    await expect.poll(() => page.evaluate(() => {
        const panel = document.querySelector('.assistant-panel-body') as HTMLElement;
        return panel.scrollHeight - panel.clientHeight - panel.scrollTop;
    }), {message: 'the panel opens at the latest message'}).toBeLessThanOrEqual(2);
    await expect(page.getByTestId('jump-to-latest')).toHaveCount(0);
};

test.describe('scroll position across a viewport resize (issue #483)', () => {
    test.skip(({browserName}) => browserName !== 'chromium', 'resize anchoring is verified in Chromium');

    test('the article the reader was on stays in view when the viewport shrinks', async ({page}) => {
        await page.setViewportSize({width: 375, height: 700});
        await mockJobSearchApi(page, 'long');
        await page.goto(CHAT_FIXTURE);
        const log = page.getByRole('log');
        await expect(log.locator('article').first()).toBeVisible();
        const box = (await log.boundingBox())!;
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
        await page.mouse.wheel(0, -300);
        await page.waitForTimeout(300);
        const readerArticle = await page.evaluate(() => {
            const top = document.querySelector('[role="log"]')!.getBoundingClientRect().top;
            const articles = Array.from(document.querySelectorAll('[role="log"] article'));
            const first = articles.find((a) => a.getBoundingClientRect().bottom > top + 1)!;
            return articles.indexOf(first);
        });
        await page.setViewportSize({width: 375, height: 380});
        await page.waitForTimeout(400);
        const inView = await page.evaluate((index) => {
            const article = document.querySelectorAll('[role="log"] article')[index] as HTMLElement;
            const rect = article.getBoundingClientRect();
            return rect.bottom > 0 && rect.top < window.innerHeight;
        }, readerArticle);
        expect(inView).toBe(true);
    });

    for (const priorities of [120, 400]) {
        test(`transcript to panel flip keeps the reader's place with ${priorities}px above the card`, async ({page}) => {
            await page.setViewportSize({width: 375, height: 1100});
            await mockJobSearchApi(page, 'long');
            await page.goto(`${PANEL_CHAT_FIXTURE}?priorities=${priorities}`);
            const card = page.getByTestId('job-search-chat');
            const log = page.getByRole('log');
            await expect(log.locator('article').first()).toBeVisible();
            await expect(card).toHaveAttribute('data-scroll-owner', 'transcript');
            const box = (await log.boundingBox())!;
            await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
            await page.mouse.wheel(0, -700);
            await page.waitForTimeout(300);
            const measure = () => page.evaluate((index) => {
                const articles = Array.from(document.querySelectorAll('[role="log"] article'));
                const header = document.querySelector('.card-header') as HTMLElement;
                const owner = document.querySelector('[data-testid="job-search-chat"]')!.getAttribute('data-scroll-owner');
                const scroller = owner === 'panel' ? document.querySelector('.assistant-panel-body')! : document.querySelector('[role="log"]')!;
                const top = owner === 'panel'
                    ? scroller.getBoundingClientRect().top + header.offsetHeight
                    : scroller.getBoundingClientRect().top;
                const pick = index === null
                    ? articles.findIndex((a) => a.getBoundingClientRect().bottom > top + 1)
                    : index;
                return {index: pick, offset: articles[pick].getBoundingClientRect().top - top, owner};
            }, null as number | null);
            const before = await measure();
            await page.setViewportSize({width: 375, height: 420});
            await expect(card).toHaveAttribute('data-scroll-owner', 'panel');
            await page.waitForTimeout(400);
            const after = await page.evaluate(({index}) => {
                const articles = Array.from(document.querySelectorAll('[role="log"] article'));
                const header = document.querySelector('.card-header') as HTMLElement;
                const panel = document.querySelector('.assistant-panel-body')!;
                const top = panel.getBoundingClientRect().top + header.offsetHeight;
                return articles[index].getBoundingClientRect().top - top;
            }, {index: before.index});
            expect(before.owner).toBe('transcript');
            expect(Math.abs(after - before.offset)).toBeLessThanOrEqual(6);
        });
    }

    test('panel to transcript flip keeps the reader\'s place', async ({page}) => {
        await page.setViewportSize({width: 375, height: 420});
        await mockJobSearchApi(page, 'long');
        await page.goto(`${PANEL_CHAT_FIXTURE}?priorities=120`);
        const card = page.getByTestId('job-search-chat');
        await expect(card).toHaveAttribute('data-scroll-owner', 'panel');
        await expect(page.getByRole('log').locator('article').first()).toBeVisible();
        await expectPanelAtLatest(page);
        const read = () => page.evaluate(() => {
            const panel = document.querySelector('.assistant-panel-body') as HTMLElement;
            const header = document.querySelector('.card-header') as HTMLElement;
            const owner = document.querySelector('[data-testid="job-search-chat"]')!.getAttribute('data-scroll-owner');
            const scroller = owner === 'panel' ? panel : document.querySelector('[role="log"]')!;
            const top = owner === 'panel' ? Math.max(panel.getBoundingClientRect().top, header.getBoundingClientRect().bottom) : scroller.getBoundingClientRect().top;
            const articles = Array.from(document.querySelectorAll('[role="log"] article'));
            const index = articles.findIndex((a) => a.getBoundingClientRect().bottom > top + 1);
            return {index, offset: articles[index].getBoundingClientRect().top - top};
        });
        await page.mouse.move(150, 300);
        await page.mouse.wheel(0, -500);
        await page.waitForTimeout(300);
        const before = await read();
        expect(before.index).toBeGreaterThan(0);
        await page.setViewportSize({width: 375, height: 900});
        await expect(card).toHaveAttribute('data-scroll-owner', 'transcript');
        await page.waitForTimeout(400);
        const after = await page.evaluate((index) => {
            const log = document.querySelector('[role="log"]')!;
            return document.querySelectorAll('[role="log"] article')[index].getBoundingClientRect().top - log.getBoundingClientRect().top;
        }, before.index);
        expect(Math.abs(after - before.offset)).toBeLessThanOrEqual(6);
    });

    for (const size of [{width: 375, height: 420}, {width: 320, height: 420}]) {
        test(`panel mode loads at the latest message, ${size.width}x${size.height}`, async ({page}) => {
            await page.setViewportSize(size);
            await mockJobSearchApi(page, 'long');
            await page.goto(`${PANEL_CHAT_FIXTURE}?priorities=120`);
            await expect(page.getByTestId('job-search-chat')).toHaveAttribute('data-scroll-owner', 'panel');
            await expect(page.getByRole('log').locator('article').first()).toBeVisible();
            await expectPanelAtLatest(page);
        });
    }

    test('focusing into the pinned bands does not scroll the panel', async ({page}) => {
        await page.setViewportSize({width: 375, height: 420});
        await mockJobSearchApi(page, 'long');
        await page.goto(`${PANEL_CHAT_FIXTURE}?priorities=120`);
        await expect(page.getByTestId('job-search-chat')).toHaveAttribute('data-scroll-owner', 'panel');
        await expectPanelAtLatest(page);
        await page.mouse.move(150, 300);
        await page.mouse.wheel(0, -700);
        await page.waitForTimeout(300);
        const scrollTop = () => page.evaluate(() => (document.querySelector('.assistant-panel-body') as HTMLElement).scrollTop);
        const before = await scrollTop();
        expect(before).toBeGreaterThan(0);
        for (const focusTarget of ['conversation-more', 'jump-to-latest']) {
            await page.getByTestId(focusTarget).focus();
            await page.waitForTimeout(150);
            expect(Math.abs((await scrollTop()) - before), `focus on ${focusTarget}`).toBeLessThanOrEqual(2);
        }
        await page.locator('textarea[aria-label="Message"]').focus();
        await page.waitForTimeout(150);
        expect(Math.abs((await scrollTop()) - before)).toBeLessThanOrEqual(2);
    });

    for (const size of [{width: 320, height: 420}, {width: 320, height: 640}]) {
        for (const [trigger, label] of [['conversation-new', 'new'], ['conversation-delete', 'delete']]) {
            test(`the ${label} confirm shows its action and Cancel together on open, ${size.width}x${size.height}`, async ({page}) => {
                await page.setViewportSize(size);
                await mockJobSearchApi(page, 'long');
                await page.goto(`${PANEL_CHAT_FIXTURE}?priorities=120`);
                if (size.height < 500) await expect(page.getByTestId('job-search-chat')).toHaveAttribute('data-scroll-owner', 'panel');
                await expect(page.getByRole('log').locator('article').first()).toBeVisible();
                await page.getByTestId('conversation-more').click();
                await page.getByTestId(trigger).click();
                await expect(page.getByTestId('confirm-panel')).toBeVisible();
                await page.waitForTimeout(300);
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

    test('after a failed delete in panel mode the focused retry is topmost, not under the pinned Cancel row', async ({page}) => {
        await page.setViewportSize({width: 320, height: 420});
        await mockJobSearchApi(page, 'long');
        await page.route('**/api/agent/conversations/*/delete/**', (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
        await page.goto(`${PANEL_CHAT_FIXTURE}?priorities=120`);
        await expect(page.getByTestId('job-search-chat')).toHaveAttribute('data-scroll-owner', 'panel');
        await page.getByTestId('conversation-more').click();
        await page.getByTestId('conversation-delete').click();
        await page.getByTestId('confirm-action').click();
        const retry = page.getByTestId('confirm-action');
        await expect(page.getByRole('alert').filter({hasText: /delete/i})).toBeVisible();
        await expect(retry).toBeFocused();
        await page.waitForTimeout(300);
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
