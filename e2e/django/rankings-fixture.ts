// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Helpers for the specs that change a company's score behind an open rankings
// page (issue #485c): the fixture command, a page opened with a simulated
// clock and a test-driven visibility, and row measurements taken from the
// viewport rather than from scrollY.
import {execFileSync} from 'child_process';

import {expect, Page} from '@playwright/test';

import {djangoServerEnv} from './server-env';

export const POLL_INTERVAL_MS = 60_000;

/** The seeded order is Alpha 4.8, Beta 4.2, Gamma 3.5, Longname 2.9; raising
 * Gamma above Alpha is a visible reorder. */
export const RAISED_ORG = 'E2E Gamma Works';
export const RAISED_SCORE = '4.9';

type RankingsAction = 'set' | 'restore' | 'pad' | 'unpad';

/** Runs the dev-only fixture against the same database the servers use. */
export function rankingsFixture(action: RankingsAction, extra: Record<string, string> = {}): void {
    execFileSync(
        'python3',
        ['manage.py', 'shell', '-c', "exec(open('e2e/django/rankings_refresh_fixture.py').read())"],
        {env: {...process.env, ...djangoServerEnv(), ACTION: action, ...extra}, stdio: 'pipe', maxBuffer: 64 * 1024 * 1024},
    );
}

export const raiseGamma = () => rankingsFixture('set', {ORG: RAISED_ORG, VALUE: RAISED_SCORE});

export async function setTabVisibility(page: Page, state: 'visible' | 'hidden'): Promise<void> {
    await page.evaluate((next) => {
        (window as any).__visibility = next;
        document.dispatchEvent(new Event('visibilitychange'));
    }, state);
}

/** Counts the polls and the reads of the rows. */
export function countRankingsRequests(page: Page) {
    const seen = {revision: 0, revisionDone: 0, list: 0};
    page.on('request', (request) => {
        const path = new URL(request.url()).pathname;
        if (/^\/api\/rankings\/\d+\/revision\/$/.test(path)) seen.revision += 1;
        else if (/^\/api\/rankings\/\d+\/$/.test(path)) seen.list += 1;
    });
    page.on('requestfinished', (request) => {
        if (/^\/api\/rankings\/\d+\/revision\/$/.test(new URL(request.url()).pathname)) seen.revisionDone += 1;
    });
    return seen;
}

/** Opens the rankings with the clock installed before anything loads and the
 * visibility under the test's control; waits for the list to be on screen. */
export async function openRankings(page: Page, path = '/'): Promise<void> {
    await page.addInitScript(() => {
        (window as any).__visibility = 'visible';
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => (window as any).__visibility});
    });
    await page.clock.install({time: new Date()});
    await page.goto(path);
    await expect(page.getByTestId('organization-results-region')).toBeVisible();
    await expect(page.locator('.organization-row, .organization-card').filter({visible: true}).first()).toBeVisible();
}

/** Company names in list order (the table and the cards list the same rows). */
export const rowNames = (page: Page) => page.evaluate(() =>
    Array.from(document.querySelectorAll('.organization-row .organization-name')).map((node) => node.textContent));

/** The row being read: the first visible row that starts below the sticky top
 * bar, and where it sits in the viewport. */
export async function readingOrg(page: Page) {
    return page.evaluate(() => {
        const bar = document.querySelector<HTMLElement>('.app-mobile-topbar');
        const floor = bar ? Math.max(0, bar.getBoundingClientRect().bottom) : 0;
        const rows = Array.from(document.querySelectorAll<HTMLElement>(
            '.organization-row[data-organization-id], .organization-card[data-organization-id]'));
        const row = rows.find((candidate) => {
            const rect = candidate.getBoundingClientRect();
            return rect.height > 0 && rect.top >= floor && rect.top < window.innerHeight;
        });
        return row ? {id: row.dataset.organizationId!, top: row.getBoundingClientRect().top} : null;
    });
}

export const orgTop = (page: Page, id: string) => page.evaluate((organizationId) => {
    const row = Array.from(document.querySelectorAll<HTMLElement>(`[data-organization-id="${organizationId}"]`))
        .find((candidate) => candidate.getBoundingClientRect().height > 0);
    return row ? row.getBoundingClientRect().top : null;
}, id);

/** Where every displayed row sits in the viewport, by organization id. */
export const rowTops = (page: Page) => page.evaluate(() => Object.fromEntries(Array.from(document.querySelectorAll<HTMLElement>(
    '.organization-row[data-organization-id], .organization-card[data-organization-id]'))
    .filter((row) => row.getBoundingClientRect().height > 0)
    .map((row) => [row.dataset.organizationId!, row.getBoundingClientRect().top])) as Record<string, number>);

export type ReaderPosition = 'top' | 'heading-at-top' | 'mid-list';

/** Scrolls so the reader is at the top, with the page heading at the top of
 * the viewport, or with the second row near the top. */
export async function placeRankingsReader(page: Page, position: ReaderPosition): Promise<void> {
    await page.evaluate((where) => {
        const heading = document.getElementById('rankings-heading')!;
        const second = document.querySelectorAll<HTMLElement>('.organization-row, .organization-card');
        const visibleSecond = Array.from(second).filter((row) => row.getBoundingClientRect().height > 0)[1];
        let top = 0;
        if (where === 'heading-at-top') top = window.scrollY + heading.getBoundingClientRect().top - 72;
        if (where === 'mid-list') top = window.scrollY + visibleSecond.getBoundingClientRect().top - 140;
        window.scrollTo({top: Math.max(0, top), left: 0, behavior: 'instant'});
    }, position);
}
