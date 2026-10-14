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
import {E2E_PASSWORD, expectNoHorizontalOverflow, requireDjangoTier} from './support';

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
    const seen = {revision: 0, status: 0};
    page.on('request', (request) => {
        if (request.url().includes('/api/job-matches/revision/')) seen.revision += 1;
        if (request.url().includes('/api/job-matches/status/')) seen.status += 1;
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
                window.scrollTo({top: 80, left: 0, behavior: 'instant'});
            });
            const scrollBefore = await page.evaluate(() => window.scrollY);
            const statusBefore = seen.status;

            fixture('settle');
            await page.clock.runFor(INTERVAL_MS + 1000);
            const notice = page.getByTestId('updated-results-notice');
            await expect(notice).toBeVisible();
            await expect(notice).toHaveAttribute('role', 'status');
            await expect(notice).toContainText('Updated results available.');
            expect(seen.revision).toBe(1);
            // Nothing else moved: same node, same scroll, old content, no status read.
            expect(await page.evaluate(() => window.scrollY)).toBe(scrollBefore);
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
