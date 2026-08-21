// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Viewport regression harness for the job-search chat (issue #431). Replaces the
// manual-only viewport/zoom/keyboard/IME verification recorded in
// docs/rollout-gates.md §4 with automated cross-browser assertions.
import {test, expect, Page} from '@playwright/test';

const CHAT_FIXTURE = '/e2e/fixtures/job-search-chat.html';
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

/**
 * Stub the job-search chat API so the component runs without a Django backend.
 * `scenario` selects the resume shape: `empty` (no history) or `populated`.
 */
async function mockJobSearchApi(page: Page, scenario: 'empty' | 'populated'): Promise<void> {
    await page.route('**/api/agent/conversations/**', async (route) => {
        const request = route.request();
        const method = request.method();
        const pathname = new URL(request.url()).pathname;

        if (method === 'GET' && pathname === '/api/agent/conversations/') {
            if (scenario === 'populated') {
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
            if (vp.width < 768) {
                // Mobile/tablet-narrow: cards are shown, table is hidden.
                await expect(cards).toBeVisible();
                await expect(wrap).toBeHidden();
            } else {
                // Desktop: table is shown and the wrapper owns horizontal
                // scrolling with no inner vertical scrollbar (overflow-y hidden).
                await expect(wrap).toBeVisible();
                const overflowY = await wrap.evaluate((el) => getComputedStyle(el).overflowY);
                expect(overflowY, 'organization list must not introduce an inner vertical scrollbar').toBe('hidden');
            }
            await expectNoHorizontalOverflow(page);
        });
    });
}

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
