// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// "Refresh matches" against a server that really stores and recomputes
// matches (issue #473). Nothing here is stubbed: the notice comes from a
// stored generation that cites a removed fact, and the button's outcome is
// whatever POST /api/job-matches/refresh/ answered.
import {execFileSync} from 'child_process';

import {expect, Page, test} from '@playwright/test';

import {DJANGO_STORED_BASE_URL, djangoServerEnv} from './server-env';
import {E2E_PASSWORD, expectNoHorizontalOverflow, requireDjangoTier} from './support';

requireDjangoTier();

const USERNAME = 'e2e_refresh_user';
const REFRESH = '/api/job-matches/refresh/';

function fixture(action: 'arm' | 'pause' | 'settle' | 'disarm'): void {
    execFileSync(
        'python3',
        ['manage.py', 'shell', '-c', "exec(open('e2e/django/match_refresh_fixture.py').read())"],
        {env: {...process.env, ...djangoServerEnv(), ACTION: action, E2E_PASSWORD}, stdio: 'pipe'},
    );
}

async function openStoredMatches(page: Page) {
    await page.goto(`${DJANGO_STORED_BASE_URL}/accounts/login/`);
    await page.locator('input[name="login"]').fill(USERNAME);
    await page.locator('input[name="password"]').fill(E2E_PASSWORD);
    await page.getByRole('button', {name: 'Sign In'}).click();
    await page.waitForURL((url) => url.pathname === '/');
    await page.goto(`${DJANGO_STORED_BASE_URL}/chat/`);
    // The assistant takes focus once when it opens with the page; wait for
    // that to be over so it cannot land after the press under test.
    await expect(page.getByTestId('assistant-panel')).toBeVisible();
    await expect(page.getByTestId('chat-loading')).toHaveCount(0);
    const back = page.getByTestId('assistant-back-to-results');
    if (await back.isVisible()) await back.click();
    await page.waitForLoadState('networkidle');
    await expect(page.getByTestId('ranked-job-matches')).toBeVisible();
    return page.getByTestId('job-match-panel');
}

const refreshPosts = (page: Page): string[] => {
    const seen: string[] = [];
    page.on('response', async (response) => {
        if (response.url().includes(REFRESH) && response.request().method() === 'POST') {
            seen.push(`${response.status()} ${(await response.json()).status}`);
        }
    });
    return seen;
};

test.describe('Refresh matches on stored results', () => {
    test.beforeEach(() => fixture('arm'));
    test.afterEach(() => fixture('disarm'));

    for (const [width, height] of [[1280, 800], [375, 800]] as const) {
        test(`a refresh re-checks, clears the notice and says so at ${width}px`, async ({page}) => {
            await page.setViewportSize({width, height});
            const posts = refreshPosts(page);
            const panel = await openStoredMatches(page);
            const notice = page.getByTestId('evidence-changed-notice');
            await expect(notice).toHaveCount(1);
            await expect(notice).toContainText('Refresh to re-check them.');
            const chip = page.getByTestId('ranked-job-matches').getByTestId('requirement-work_location.modes').first();
            await expect(chip).toHaveAttribute('data-evidence-state', 'missing');
            await expect(chip).toContainText('Evidence changed — refresh');
            const heading = page.getByRole('heading', {name: 'Your Job Matches'});
            await expect(heading).toHaveAttribute('aria-describedby', 'job-match-notice');

            const refresh = notice.getByRole('button', {name: 'Refresh matches'});
            expect((await refresh.boundingBox())!.height).toBeGreaterThanOrEqual(44);
            await refresh.focus();
            // A double press is one request.
            await page.keyboard.press('Enter');
            await page.keyboard.press('Enter');

            await expect(notice).toHaveCount(0);
            expect(posts).toEqual(['200 published']);
            await expect(chip).toHaveAttribute('data-evidence-state', 'sourced');
            await expect(chip).toContainText('Sourced, not confirmed');
            // The button went with the notice: focus is on the heading, and
            // the outcome is announced.
            await expect(heading).toBeFocused();
            await expect(page.getByTestId('recheck-announcement')).toHaveText('Matches re-checked.');
            await expect(panel.getByRole('button', {name: 'Refresh matches'})).toHaveCount(0);
            await expectNoHorizontalOverflow(page);

            // Resolved: the stored generation survives a reload.
            await page.reload();
            await expect(page.getByTestId('ranked-job-matches')).toBeAttached();
            await expect(notice).toHaveCount(0);
        });

        test(`paused by the operator: the notice says so and offers no button at ${width}px`, async ({page}) => {
            fixture('pause');
            await page.setViewportSize({width, height});
            const posts = refreshPosts(page);
            await openStoredMatches(page);
            const notice = page.getByTestId('evidence-changed-notice');
            const refresh = notice.getByRole('button', {name: 'Refresh matches'});
            await refresh.focus();
            await page.keyboard.press('Enter');

            await expect(notice).toHaveAttribute('data-recheck', 'paused');
            expect(posts).toEqual(['200 disabled']);
            await expect(notice).toContainText('Re-checking is paused right now');
            await expect(notice).not.toContainText(/try again/i);
            await expect(notice.getByRole('button')).toHaveCount(0);
            await expect(page.getByTestId('evidence-changed-notice')).toHaveCount(1);
            await expect(page.getByRole('heading', {name: 'Your Job Matches'})).toBeFocused();
            // The results are still on screen, still marked, without a call
            // to refresh that nothing on the page can answer.
            const chip = page.getByTestId('ranked-job-matches').getByTestId('requirement-work_location.modes').first();
            await expect(chip).toContainText('Work mode · Evidence changed');
            await expect(chip).not.toContainText('refresh');
            // The header's refresh asks again and re-reads the lists.
            const reread = page.waitForResponse((response) => response.url().includes('/api/job-matches/ranked/'));
            await page.getByTestId('job-match-refresh').click();
            expect((await reread).status()).toBe(200);
            await expect.poll(() => posts).toEqual(['200 disabled', '200 disabled']);
            await expect(notice).toHaveAttribute('data-recheck', 'paused');
            await expectNoHorizontalOverflow(page);
        });

        test(`nothing left to re-check: the notice clears and says the matches were up to date at ${width}px`, async ({page}) => {
            await page.setViewportSize({width, height});
            const posts = refreshPosts(page);
            await openStoredMatches(page);
            const notice = page.getByTestId('evidence-changed-notice');
            await expect(notice).toHaveCount(1);
            // The matches are published again behind the open page, so the
            // generation on the server no longer cites the removed fact.
            fixture('settle');
            const refresh = notice.getByRole('button', {name: 'Refresh matches'});
            await refresh.focus();
            await page.keyboard.press('Enter');

            await expect(notice).toHaveCount(0);
            expect(posts).toEqual(['200 not_needed']);
            const chip = page.getByTestId('ranked-job-matches').getByTestId('requirement-work_location.modes').first();
            await expect(chip).toHaveAttribute('data-evidence-state', 'sourced');
            await expect(page.getByRole('heading', {name: 'Your Job Matches'})).toBeFocused();
            await expect(page.getByTestId('recheck-announcement')).toHaveText('Your matches were already up to date.');
            await expectNoHorizontalOverflow(page);
        });
    }
});
