// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// "Updated results available" against a server that really stores matches
// (issue #485, 485b). Nothing is stubbed: a generation is published behind
// the open page (the fixture's `settle`), the page's poller finds it, and the
// button applies it. Time is simulated with `page.clock` (installed before
// navigation) and visibility is driven from the test; no spec waits for a
// real interval. The fixture's throwaway account is armed before each test
// and always disarmed after it.
import {execFileSync} from 'child_process';

import {expect, Page, test} from '@playwright/test';

import {DJANGO_STORED_BASE_URL, djangoServerEnv} from './server-env';
import {
    countRankingsRequests,
    openRankings,
    orgTop,
    placeRankingsReader,
    POLL_INTERVAL_MS,
    raiseGamma,
    ReaderPosition,
    readingOrg,
    rankingsFixture,
    rowNames,
    setTabVisibility,
} from './rankings-fixture';
import {E2E_PASSWORD, expectNoHorizontalOverflow, login, requireDjangoTier} from './support';

requireDjangoTier();

const USERNAME = 'e2e_refresh_user';
const INTERVAL_MS = 60_000;

function fixture(action: 'arm' | 'settle' | 'disarm'): void {
    execFileSync(
        'python3',
        ['manage.py', 'shell', '-c', "exec(open('e2e/django/match_refresh_fixture.py').read())"],
        {env: {...process.env, ...djangoServerEnv(), ACTION: action, E2E_PASSWORD}, stdio: 'pipe'},
    );
}

async function setVisibility(page: Page, state: 'visible' | 'hidden'): Promise<void> {
    await page.evaluate((next) => {
        (window as any).__visibility = next;
        document.dispatchEvent(new Event('visibilitychange'));
    }, state);
}

// Counts requests to the revision read; the status read must never be polled.
function countRequests(page: Page) {
    const seen = {revision: 0, revisionDone: 0, status: 0};
    page.on('request', (request) => {
        if (request.url().includes('/api/job-matches/revision/')) seen.revision += 1;
        if (request.url().includes('/api/job-matches/status/')) seen.status += 1;
    });
    page.on('requestfinished', (request) => {
        if (request.url().includes('/api/job-matches/revision/')) seen.revisionDone += 1;
    });
    return seen;
}

async function open(page: Page) {
    // Visibility is the test's to drive; the clock is installed before anything loads.
    await page.addInitScript(() => {
        (window as any).__visibility = 'visible';
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => (window as any).__visibility});
    });
    await page.clock.install({time: new Date()});
    await page.goto(`${DJANGO_STORED_BASE_URL}/accounts/login/`);
    await page.locator('input[name="login"]').fill(USERNAME);
    await page.locator('input[name="password"]').fill(E2E_PASSWORD);
    await page.getByRole('button', {name: 'Sign In'}).click();
    await page.waitForURL((url) => url.pathname === '/');
    await page.goto(`${DJANGO_STORED_BASE_URL}/chat/`);
    await expect(page.getByTestId('assistant-panel')).toBeVisible();
    await expect(page.getByTestId('chat-loading')).toHaveCount(0);
    const back = page.getByTestId('assistant-back-to-results');
    if (await back.isVisible()) await back.click();
    await expect(page.getByTestId('ranked-job-matches')).toBeVisible();
}

// The generation each of the three reads of one load carries (in-page fetch:
// it shares the session cookie).
async function generations(page: Page) {
    return page.evaluate(async () => {
        const read = async (url: string) => (await fetch(url)).json();
        const [status, matches, ranked] = await Promise.all([
            read('/api/job-matches/status/'),
            read('/api/job-matches/?page=1&page_size=1'),
            read('/api/job-matches/ranked/?limit=10'),
        ]);
        return {
            status: status.result_generation as number | null,
            matches: matches.results?.[0]?.revision?.result_generation as number | null,
            ranked: ranked.job_matches?.[0]?.revision?.result_generation as number | null,
        };
    });
}

// The result card being read: the first one on screen, and where it sits in
// the viewport. The notice and the stale box come and go above the list, so
// "nothing moved" is measured on the row, not on scrollY (scroll anchoring
// hides a push from scrollY in Chrome and does not exist in Safari).
async function readingRow(page: Page) {
    return page.evaluate(() => {
        const cards = Array.from(document.querySelectorAll<HTMLElement>(
            '[data-testid^="ranked-job-"]:not([data-testid="ranked-job-matches"])'));
        const card = cards.find((c) => {
            const rect = c.getBoundingClientRect();
            return rect.bottom > 0 && rect.top < window.innerHeight;
        });
        return card ? {id: card.dataset.testid!, top: card.getBoundingClientRect().top} : null;
    });
}
const rowTop = (page: Page, id: string) =>
    page.evaluate((testid) => document.querySelector(`[data-testid="${testid}"]`)?.getBoundingClientRect().top ?? null, id);

type Position = 'heading-at-top' | 'mid-list';
async function placeReader(page: Page, position: Position) {
    await page.evaluate((where) => {
        const heading = document.getElementById('job-match-panel-title')!;
        const max = document.documentElement.scrollHeight - window.innerHeight;
        const top = where === 'heading-at-top'
            // 72 px: below the sticky top bar, so no control is scrolled into view by the test's own press.
            ? window.scrollY + heading.getBoundingClientRect().top - 72
            : Math.min(max, window.scrollY + heading.getBoundingClientRect().top + 220);
        window.scrollTo({top, left: 0, behavior: 'instant'});
    }, position);
}

const chip = (page: Page) =>
    page.getByTestId('ranked-job-matches').getByTestId('requirement-work_location.modes').first();

test.describe('Updated results available (stored results)', () => {
    test.beforeEach(() => fixture('arm'));
    test.afterEach(() => fixture('disarm'));

    for (const [width, height] of [[1280, 800], [375, 800]] as const) {
        test(`a generation published behind the page shows a notice, changes nothing, and Refresh results applies it at ${width}px`, async ({page}) => {
            await page.setViewportSize({width, height});
            const seen = countRequests(page);
            await open(page);
            const before = await generations(page);
            expect(before.status).not.toBeNull();
            expect(new Set([before.status, before.matches, before.ranked]).size).toBe(1);
            await expect(chip(page)).toHaveAttribute('data-evidence-state', 'missing');
            await page.evaluate(() => {
                (document.querySelector('[data-testid^="ranked-job-"]:not([data-testid="ranked-job-matches"])') as any).__marker = 'same-node';
            });
            await placeReader(page, 'heading-at-top');
            const reading = (await readingRow(page))!;
            expect(reading).not.toBeNull();
            const statusBefore = seen.status;

            fixture('settle');
            await page.clock.runFor(INTERVAL_MS + 1000);
            const notice = page.getByTestId('updated-results-notice');
            await expect(notice).toBeVisible();
            await expect(notice).toHaveAttribute('role', 'status');
            await expect(notice).toContainText('Updated results available.');
            expect(seen.revision).toBe(1);
            // Nothing else moved: the row being read is where it was (the
            // notice's arrival is held), same node, old content, no status read.
            expect(Math.abs((await rowTop(page, reading.id))! - reading.top)).toBeLessThanOrEqual(2);
            expect(await page.evaluate(() =>
                (document.querySelector('[data-testid^="ranked-job-"]:not([data-testid="ranked-job-matches"])') as any).__marker)).toBe('same-node');
            await expect(chip(page)).toHaveAttribute('data-evidence-state', 'missing');
            expect(seen.status).toBe(statusBefore);
            const button = notice.getByRole('button', {name: 'Refresh results'});
            expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(44);
            await expectNoHorizontalOverflow(page);

            await button.focus();
            await page.keyboard.press('Enter');
            await expect(notice).toHaveCount(0);
            await expect.poll(async () => Math.abs((await rowTop(page, reading.id))! - reading.top)).toBeLessThanOrEqual(2);
            await expect(chip(page)).toHaveAttribute('data-evidence-state', 'sourced');
            await expect(page.getByRole('heading', {name: 'Your Job Matches'})).toBeFocused();
            const after = await generations(page);
            expect(after.status).toBeGreaterThan(before.status!);
            expect(new Set([after.status, after.matches, after.ranked]).size).toBe(1);
            expect(await page.evaluate(() =>
                (document.querySelector('[data-testid^="ranked-job-"]:not([data-testid="ranked-job-matches"])') as any).__marker)).toBe('same-node');
            await expectNoHorizontalOverflow(page);
        });
    }

    for (const [width, height] of [[1280, 800], [375, 800]] as const) {
        for (const anchoring of ['on', 'off'] as const) {
            for (const position of ['heading-at-top', 'mid-list'] as const) {
                test(`the row being read does not move when the notice arrives or is applied: ${position}, scroll anchoring ${anchoring}, ${width}px`, async ({page}) => {
                    await page.setViewportSize({width, height});
                    await open(page);
                    if (anchoring === 'off') {
                        // Safari has no scroll anchoring: stand in for it.
                        await page.addStyleTag({content: '*, *::before, *::after { overflow-anchor: none !important; }'});
                    }
                    await placeReader(page, position);
                    const reading = (await readingRow(page))!;
                    expect(reading).not.toBeNull();

                    fixture('settle');
                    await page.clock.runFor(INTERVAL_MS + 1000);
                    const notice = page.getByTestId('updated-results-notice');
                    await expect(notice).toBeVisible();
                    expect(Math.abs((await rowTop(page, reading.id))! - reading.top)).toBeLessThanOrEqual(2);

                    // Pressed from off screen (browse-mode activation): the page does not jump to the heading.
                    await notice.getByRole('button', {name: 'Refresh results'}).dispatchEvent('click');
                    await expect(notice).toHaveCount(0);
                    await expect.poll(async () => Math.abs((await rowTop(page, reading.id))! - reading.top)).toBeLessThanOrEqual(2);
                    await expectNoHorizontalOverflow(page);
                });
            }
        }

        test(`the header refresh with the notice up holds the row, and the evidence box offers no second refresh, ${width}px`, async ({page}) => {
            await page.setViewportSize({width, height});
            await open(page);
            await placeReader(page, 'heading-at-top');
            const reading = (await readingRow(page))!;
            fixture('settle');
            await page.clock.runFor(INTERVAL_MS + 1000);
            await expect(page.getByTestId('updated-results-notice')).toBeVisible();
            expect(Math.abs((await rowTop(page, reading.id))! - reading.top)).toBeLessThanOrEqual(2);
            // One message, one button: "Refresh matches" waits until the newer results are in.
            await expect(page.getByTestId('evidence-changed-notice')).toBeVisible();
            await expect(page.getByTestId('evidence-changed-refresh')).toHaveCount(0);
            await expect(page.getByTestId('stale-notice')).toHaveCount(0);
            // dispatchEvent: a real press would scroll the control into view first, which moves the row on its own.
            await page.getByTestId('job-match-refresh').dispatchEvent('click');
            await expect(page.getByTestId('updated-results-notice')).toHaveCount(0);
            await expect.poll(async () => Math.abs((await rowTop(page, reading.id))! - reading.top)).toBeLessThanOrEqual(2);
        });
    }

    test('a refresh that cannot be applied says so in the notice and keeps the old set', async ({page}) => {
        await open(page);
        fixture('settle');
        await page.clock.runFor(INTERVAL_MS + 1000);
        const notice = page.getByTestId('updated-results-notice');
        await expect(notice).toBeVisible();
        // The three reads of the press disagree: the status read reports another generation.
        // In the page (the server's host name only resolves in the browser).
        await page.evaluate(() => {
            const original = window.fetch.bind(window);
            (window as any).__realFetch = original;
            window.fetch = async (input, init) => {
                const response = await original(input, init);
                const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
                if (!url.includes('/api/job-matches/status/')) return response;
                const body = await response.clone().json();
                return new Response(JSON.stringify({...body, result_generation: (body.result_generation ?? 0) + 7}),
                    {status: 200, headers: {'Content-Type': 'application/json'}});
            };
        });
        await notice.getByRole('button', {name: 'Refresh results'}).click();
        await expect(page.getByTestId('notice-folded')).toContainText('Still showing the results you had');
        // Announced once: by the notice's own live region, not by the panel's announcer as well.
        await expect(page.getByTestId('read-announcement')).not.toContainText('weren’t ready');
        await expect(notice).toHaveAttribute('role', 'status');
        await expect(notice).toBeVisible();
        await expect(notice.getByRole('button', {name: 'Refresh results'})).not.toHaveAttribute('aria-disabled', 'true');
        await expect(chip(page)).toHaveAttribute('data-evidence-state', 'missing');
        await page.evaluate(() => { window.fetch = (window as any).__realFetch; });
        await notice.getByRole('button', {name: 'Refresh results'}).click();
        await expect(notice).toHaveCount(0);
        await expect(chip(page)).toHaveAttribute('data-evidence-state', 'sourced');
    });

    for (const [width, height] of [[1280, 800], [375, 667]] as const) {
        test(`the kept sentence and every later press hold the row at ${width}px`, async ({page}) => {
            await page.setViewportSize({width, height});
            await open(page);
            fixture('settle');
            await page.clock.runFor(INTERVAL_MS + 1000);
            const notice = page.getByTestId('updated-results-notice');
            await expect(notice).toBeVisible();
            await placeReader(page, 'heading-at-top');
            const reading = (await readingRow(page))!;
            await page.evaluate(() => {
                const original = window.fetch.bind(window);
                (window as any).__realFetch = original;
                window.fetch = async (input, init) => {
                    const response = await original(input, init);
                    const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
                    if (!url.includes('/api/job-matches/status/')) return response;
                    const body = await response.clone().json();
                    return new Response(JSON.stringify({...body, result_generation: (body.result_generation ?? 0) + 7}),
                        {status: 200, headers: {'Content-Type': 'application/json'}});
                };
            });
            const held = () => expect.poll(async () => Math.abs((await rowTop(page, reading.id))! - reading.top)).toBeLessThanOrEqual(2);
            // dispatchEvent: a real press would scroll the control into view first.
            await notice.getByRole('button', {name: 'Refresh results'}).dispatchEvent('click');
            await expect(page.getByTestId('notice-folded')).toContainText('Still showing the results you had');
            await held();
            // The next press takes the sentence away and brings it back: the row stays put through both.
            await notice.getByRole('button', {name: 'Refresh results'}).dispatchEvent('click');
            await held();
            await expect(page.getByTestId('notice-folded')).toContainText('Still showing the results you had');
            await held();
            // The reason is read before the button.
            expect(await page.evaluate(() => {
                const folded = document.querySelector('[data-testid="notice-folded"]')!;
                const button = document.querySelector('[data-testid="updated-results-refresh"]')!;
                return !!(folded.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING);
            })).toBe(true);
            await page.evaluate(() => { window.fetch = (window as any).__realFetch; });
            await notice.getByRole('button', {name: 'Refresh results'}).dispatchEvent('click');
            await expect(notice).toHaveCount(0);
            await held();
        });
    }

    test('a hidden tab sends no revision request; the return sends one', async ({page}) => {
        const seen = countRequests(page);
        await open(page);
        await setVisibility(page, 'hidden');
        await page.clock.runFor(10 * 60_000);
        expect(seen.revision).toBe(0);
        await setVisibility(page, 'visible');
        await expect.poll(() => seen.revision).toBe(1);
        // Visible again: at most one a minute.
        await page.clock.runFor(10 * 60_000);
        expect(seen.revision).toBeLessThanOrEqual(11);
    });

    test('found on the first check after a return, at the top with nothing focused, it is applied without asking', async ({page}) => {
        await open(page);
        await expect(chip(page)).toHaveAttribute('data-evidence-state', 'missing');
        await page.evaluate(() => {
            window.scrollTo({top: 0, left: 0, behavior: 'instant'});
            (document.activeElement as HTMLElement | null)?.blur();
        });
        await setVisibility(page, 'hidden');
        await page.clock.runFor(5 * 60_000);
        fixture('settle');
        await setVisibility(page, 'visible');
        await expect(chip(page)).toHaveAttribute('data-evidence-state', 'sourced');
        await expect(page.getByTestId('updated-results-notice')).toHaveCount(0);
        // The list changed under a reader who was away: it says so until they act.
        await expect(page.getByTestId('results-updated-badge')).toBeVisible();
        await expect(page.getByTestId('read-announcement')).toHaveText('Showing updated results.');
        await page.mouse.wheel(0, 40);
        await expect(page.getByTestId('results-updated-badge')).toHaveCount(0);
    });

    test('typing in the composer does not clear the mark of an idle auto-apply', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 800});
        await open(page);
        await expect(chip(page)).toHaveAttribute('data-evidence-state', 'missing');
        await page.evaluate(() => window.scrollTo({top: 0, left: 0, behavior: 'instant'}));
        const composer = page.getByTestId('assistant-composer');
        await composer.focus();
        await composer.pressSequentially('which of these');
        await setVisibility(page, 'hidden');
        await page.clock.runFor(5 * 60_000);
        fixture('settle');
        await setVisibility(page, 'visible');
        await expect(chip(page)).toHaveAttribute('data-evidence-state', 'sourced');
        await expect(page.getByTestId('results-updated-badge')).toBeVisible();
        // The reader goes on typing: that is not about the list.
        await composer.pressSequentially(' is best');
        await expect(composer).toHaveValue('which of these is best');
        await expect(page.getByTestId('results-updated-badge')).toBeVisible();
        await page.mouse.wheel(0, 40);
        await expect(page.getByTestId('results-updated-badge')).toHaveCount(0);
    });

    test('a reader who keeps switching tabs sends at most one early check per interval (AC 9 as amended 2026-10-17)', async ({page}) => {
        const seen = countRequests(page);
        await open(page);
        const start = seen.revision;
        // Away 1 s, back 10 s, for 10 simulated minutes.
        for (let elapsed = 0; elapsed < 10 * 60_000; elapsed += 11_000) {
            await setVisibility(page, 'hidden');
            await page.clock.runFor(1_000);
            await setVisibility(page, 'visible');
            await page.clock.runFor(10_000);
            // Let the answer land (the clock is simulated, the network is not):
            // a request still out would hide the rate the poller allows.
            await expect.poll(() => seen.revisionDone).toBe(seen.revision);
        }
        // One timer check and at most one early check per 60 s interval.
        expect(seen.revision - start).toBeLessThanOrEqual(20);
    });

    // The mark wraps the timestamp line on phones: it must be painted, and its
    // box must stay when it goes, or the first tap lands on the row below.
    test.describe('on phones', () => {
        test.use({hasTouch: true});
        for (const [width, height] of [[375, 667], [390, 844]] as const) {
            test(`the idle auto-apply's mark is seen and the first tap on a job title opens the listing at ${width}x${height}`, async ({page}) => {
                await page.setViewportSize({width, height});
                await open(page);
                await page.evaluate(() => {
                    window.scrollTo({top: 0, left: 0, behavior: 'instant'});
                    (document.activeElement as HTMLElement | null)?.blur();
                });
                await setVisibility(page, 'hidden');
                await page.clock.runFor(5 * 60_000);
                fixture('settle');
                await setVisibility(page, 'visible');
                await expect(chip(page)).toHaveAttribute('data-evidence-state', 'sourced');
                // Still there after the panel's own hold has scrolled the page.
                await expect(page.getByTestId('results-updated-badge')).toBeVisible();
                const title = page.getByTestId('ranked-job-matches').locator('a[target="_blank"]').first();
                // Scrolled into reach only when it is not already (a scroll event is the old clearing path:
                // the first input must be the tap). Centred, not under the sticky top bar.
                const reach = (await title.boundingBox())!;
                if (reach.y < 60 || reach.y + reach.height > height - 60) {
                    await title.evaluate((el) => el.scrollIntoView({block: 'center', behavior: 'instant'}));
                }
                // The tap is the reader's first input: the mark leaves, its box does not.
                const before = (await title.boundingBox())!;
                const popup = page.waitForEvent('popup', {timeout: 5000});
                await page.touchscreen.tap(before.x + before.width / 2, before.y + before.height / 2);
                await popup;
                await expect(page.getByTestId('results-updated-badge')).toHaveCount(0);
                expect(Math.abs((await title.boundingBox())!.y - before.y)).toBeLessThanOrEqual(1);
            });
        }
    });

    test('after a return, with focus inside the panel, only the notice appears', async ({page}) => {
        await open(page);
        await page.getByTestId('job-match-refresh').focus();
        await setVisibility(page, 'hidden');
        await page.clock.runFor(5 * 60_000);
        fixture('settle');
        await setVisibility(page, 'visible');
        await expect(page.getByTestId('updated-results-notice')).toBeVisible();
        await expect(chip(page)).toHaveAttribute('data-evidence-state', 'missing');
    });

    test('a quick return does not make the next scheduled check an idle apply, even with the reader typing', async ({page}) => {
        const seen = countRequests(page);
        await open(page);
        await page.clock.runFor(INTERVAL_MS + 1000);
        expect(seen.revision).toBe(1);
        await page.evaluate(() => {
            window.scrollTo({top: 0, left: 0, behavior: 'instant'});
            (document.activeElement as HTMLElement | null)?.blur();
        });
        // Away for 5 s: the return waits for the scheduled check.
        await page.clock.runFor(2000);
        await setVisibility(page, 'hidden');
        await page.clock.runFor(5000);
        await setVisibility(page, 'visible');
        await page.clock.runFor(100);
        expect(seen.revision).toBe(1);
        const composer = page.getByTestId('assistant-composer');
        await composer.fill('half a sentence');
        fixture('settle');
        await page.clock.runFor(INTERVAL_MS);
        await expect(page.getByTestId('updated-results-notice')).toBeVisible();
        await expect(chip(page)).toHaveAttribute('data-evidence-state', 'missing');
        await expect(page.getByTestId('results-updated-badge')).toHaveCount(0);
        await expect(composer).toBeFocused();
        await expect(composer).toHaveValue('half a sentence');
    });

    test('a draft in the composer survives the notice and Refresh results', async ({page}) => {
        await open(page);
        const composer = page.getByTestId('assistant-composer');
        await composer.fill('a half-written question');
        fixture('settle');
        await page.clock.runFor(INTERVAL_MS + 1000);
        const notice = page.getByTestId('updated-results-notice');
        await expect(notice).toBeVisible();
        await expect(composer).toHaveValue('a half-written question');
        await notice.getByRole('button', {name: 'Refresh results'}).click();
        await expect(notice).toHaveCount(0);
        await expect(composer).toHaveValue('a half-written question');
    });
});

// The company rankings against the main server (issue #485c). A score is
// raised through the real write path behind the open page; the page's poller
// finds it within one simulated interval, offers it, and changes nothing until
// the reader presses Refresh results (or the idle rule applies it).
const SEEDED_ORDER = ['E2E Alpha Corp', 'E2E Beta Labs', 'E2E Gamma Works'];
const RAISED_ORDER = ['E2E Gamma Works', 'E2E Alpha Corp', 'E2E Beta Labs'];

test.describe('Updated rankings available (company rankings)', () => {
    test.afterEach(() => {
        rankingsFixture('unpad');
        rankingsFixture('restore');
    });

    const notice = (page: Page) => page.getByTestId('rankings-updated-notice');
    const firstThree = async (page: Page) => (await rowNames(page)).slice(0, 3);

    for (const [width, height] of [[1280, 800], [375, 800]] as const) {
        test(`a score raised behind the page shows a notice, changes nothing, and Refresh results applies it at ${width}px`, async ({page}) => {
            await page.setViewportSize({width, height});
            const seen = countRankingsRequests(page);
            await openRankings(page);
            expect(await firstThree(page)).toEqual(SEEDED_ORDER);
            await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();

            raiseGamma();
            await page.clock.runFor(POLL_INTERVAL_MS + 1000);
            await expect(notice(page)).toBeVisible();
            await expect(notice(page)).toContainText('Updated results available.');
            expect(seen.revision).toBe(1);
            expect(seen.list).toBe(0);
            // Nothing else changed: same order, same count, one polite region.
            expect(await firstThree(page)).toEqual(SEEDED_ORDER);
            await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
            await expect(page.getByTestId('rankings-announcement')).toHaveText('Updated results available.');
            expect(await page.locator('[aria-live="polite"]:has([data-testid="rankings-announcement"])').count()).toBe(1);
            const button = notice(page).getByRole('button', {name: 'Refresh results'});
            expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(44);
            await expectNoHorizontalOverflow(page);

            await button.focus();
            await page.keyboard.press('Enter');
            await expect(notice(page)).toHaveCount(0);
            expect(seen.list).toBe(1);
            await expect.poll(() => firstThree(page)).toEqual(RAISED_ORDER);
            await expect(page.locator('#rankings-heading')).toBeFocused();
            await expect(page.getByTestId('rankings-announcement')).toHaveText('Showing updated rankings.');
            await expectNoHorizontalOverflow(page);
            // The page a reader would reload to shows the same order.
            await page.reload();
            expect(await firstThree(page)).toEqual(RAISED_ORDER);
        });
    }

    test('a signed-in reader gets the same notice, and the same endpoints answer', async ({page}) => {
        await login(page);
        const seen = countRankingsRequests(page);
        await openRankings(page);
        raiseGamma();
        await page.clock.runFor(POLL_INTERVAL_MS + 1000);
        await expect(notice(page)).toBeVisible();
        expect(seen.revision).toBe(1);
        await notice(page).getByRole('button', {name: 'Refresh results'}).click();
        await expect.poll(() => firstThree(page)).toEqual(RAISED_ORDER);
        // The in-page answers carry no account and are never stored.
        const answer = await page.evaluate(async () => {
            const id = document.getElementById('organization-list-config')!.dataset.currentAlgorithmId;
            const res = await fetch(`/api/rankings/${id}/revision/`);
            return {status: res.status, cache: res.headers.get('cache-control'), body: await res.json()};
        });
        expect(answer.status).toBe(200);
        expect(answer.cache).toContain('no-store');
        expect(Object.keys(answer.body).sort()).toEqual(['poll_after_seconds', 'revision']);
    });

    for (const [width, height] of [[1280, 420], [375, 667]] as const) {
        for (const anchoring of ['on', 'off'] as const) {
            for (const position of ['top', 'heading-at-top', 'mid-list'] as ReaderPosition[]) {
                test(`the row being read does not move when the notice arrives or is applied: ${position}, scroll anchoring ${anchoring}, ${width}px`, async ({page}) => {
                    await page.setViewportSize({width, height});
                    await openRankings(page);
                    if (anchoring === 'off') {
                        // Safari has no scroll anchoring: stand in for it.
                        await page.addStyleTag({content: '*, *::before, *::after { overflow-anchor: none !important; }'});
                    }
                    await placeRankingsReader(page, position);
                    const reading = (await readingOrg(page))!;
                    expect(reading).not.toBeNull();

                    raiseGamma();
                    await page.clock.runFor(POLL_INTERVAL_MS + 1000);
                    await expect(notice(page)).toBeVisible();
                    await expect.poll(async () => Math.abs((await orgTop(page, reading.id))! - reading.top)).toBeLessThanOrEqual(2);

                    // Pressed from off screen (browse-mode activation): the page does not jump to the heading.
                    await notice(page).getByRole('button', {name: 'Refresh results'}).dispatchEvent('click');
                    await expect(notice(page)).toHaveCount(0);
                    await expect.poll(() => firstThree(page)).toEqual(RAISED_ORDER);
                    await expect.poll(async () => Math.abs((await orgTop(page, reading.id))! - reading.top)).toBeLessThanOrEqual(2);
                    await expectNoHorizontalOverflow(page);
                });
            }
        }
    }

    test('a hidden tab sends nothing, and a return finds the change', async ({page}) => {
        const seen = countRankingsRequests(page);
        await openRankings(page);
        await setTabVisibility(page, 'hidden');
        await page.clock.runFor(10 * 60_000);
        expect(seen.revision).toBe(0);
        raiseGamma();
        await page.evaluate(() => {
            window.scrollTo({top: 200, left: 0, behavior: 'instant'});
        });
        await setTabVisibility(page, 'visible');
        await expect(notice(page)).toBeVisible();
        expect(seen.revision).toBe(1);
        // Scrolled, so the idle rule does not apply it.
        expect(await firstThree(page)).toEqual(SEEDED_ORDER);
    });

    test('over 10 simulated visible minutes the page sends at most 10 polls', async ({page}) => {
        const seen = countRankingsRequests(page);
        await openRankings(page);
        await page.clock.runFor(10 * 60_000);
        expect(seen.revision).toBeLessThanOrEqual(10);
        expect(seen.revision).toBeGreaterThanOrEqual(9);
        await expect(notice(page)).toHaveCount(0);
    });

    test('idle at the top after a return: the rows are swapped without a press and an "Updated" mark follows the reader\u2019s input', async ({page}) => {
        await openRankings(page);
        await page.evaluate(() => {
            window.scrollTo({top: 0, left: 0, behavior: 'instant'});
            (document.activeElement as HTMLElement | null)?.blur();
        });
        await setTabVisibility(page, 'hidden');
        await page.clock.runFor(5 * 60_000);
        raiseGamma();
        await setTabVisibility(page, 'visible');
        await expect.poll(() => firstThree(page)).toEqual(RAISED_ORDER);
        await expect(notice(page)).toHaveCount(0);
        await expect(page.getByTestId('rankings-updated-badge')).toBeVisible();
        const count = (await page.locator('.organization-results-count').boundingBox())!;
        await page.mouse.wheel(0, 40);
        await expect(page.getByTestId('rankings-updated-badge')).toHaveCount(0);
        // Its box stays, so the count line does not reflow.
        expect(Math.abs((await page.locator('.organization-results-count').boundingBox())!.height - count.height)).toBeLessThanOrEqual(1);
    });

    test('a list on page 2 that shrinks to one page lands on page 1 and the URL says so', async ({page}) => {
        rankingsFixture('pad');
        await openRankings(page, '/?page=2');
        await expect(page.getByText('Showing 16-16 of 16 organizations')).toBeVisible();
        rankingsFixture('unpad');
        await page.clock.runFor(POLL_INTERVAL_MS + 1000);
        await expect(notice(page)).toBeVisible();
        await notice(page).getByRole('button', {name: 'Refresh results'}).click();
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        expect(new URL(page.url()).searchParams.get('page')).toBe('1');
        // A stale ?page=2 would be a 404 on reload.
        const reloaded = await page.reload();
        expect(reloaded!.status()).toBe(200);
    });

    test('with a company open nothing is applied; the notice is waiting after Back to results', async ({page}) => {
        await openRankings(page);
        await page.locator('.organization-row, .organization-card').filter({visible: true}).first().click();
        await expect(page.getByTestId('company-view')).toBeVisible();
        raiseGamma();
        await page.clock.runFor(POLL_INTERVAL_MS + 1000);
        await expect(notice(page)).toBeHidden();
        await page.getByRole('button', {name: 'Back to results'}).click();
        await expect(notice(page)).toBeVisible();
        expect(await firstThree(page)).toEqual(SEEDED_ORDER);
    });
});
