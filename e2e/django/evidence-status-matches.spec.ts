// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Evidence status on job-match cards and assistant organization cards over
// the seeded Django server (issue #473b). The seed gives E2E Alpha Corp a
// fresh prose RTO fact ("Remote-first") and E2E Beta Labs a stale one.
import {expect, Page, Route, test} from '@playwright/test';
import {expectNoHorizontalOverflow, login, requireDjangoTier} from './support';

requireDjangoTier();

const RANKED = '**/api/job-matches/ranked/**';

// route.fetch() runs in Node, which cannot resolve the browser-only
// local.crank.fyi mapping; the same server answers on the loopback address.
function fetchFromServer(route: Route) {
    return route.fetch({url: route.request().url().replace('//local.crank.fyi', '//127.0.0.1')});
}

async function openMatches(page: Page) {
    await page.goto('/chat/');
    await expect(page.getByTestId('assistant-panel')).toBeVisible();
    // On a phone the assistant opens as a sheet over the page; the matches
    // are read after going back to the results.
    const back = page.getByTestId('assistant-back-to-results');
    if (await back.isVisible()) await back.click();
    await expect(page.getByTestId('ranked-job-matches')).toBeVisible();
    return page.getByTestId('job-match-panel');
}

function orgCard(page: Page, name: string) {
    return page.getByTestId('ranked-org-matches').locator('.job-match-card').filter({hasText: name});
}

for (const [width, height] of [[1280, 800], [375, 800]] as const) {
    test.describe(`at ${width}px`, () => {
        test.beforeEach(async ({page}) => {
            await page.setViewportSize({width, height});
            await login(page);
        });

        test('requirement chips are qualified by the fact behind them', async ({page}) => {
            const panel = await openMatches(page);

            // Alpha's fact is fresh but prose, so the match is sourced, not verified.
            const jobChip = page.getByTestId('ranked-job-matches').getByTestId('requirement-work_location.modes').first();
            await expect(jobChip).toHaveAttribute('data-status', 'match');
            await expect(jobChip).toHaveAttribute('data-evidence-state', 'sourced');
            await expect(jobChip).toContainText('Work mode · Sourced, not confirmed');
            await expect(jobChip).toHaveClass(/job-match-chip--unverified/);
            await expect(jobChip.locator('.evidence-badge-verified')).toHaveCount(0);

            // Beta's fact is 400 days old and says Remote (the seed's displayed
            // policy since #484): the match stays, stale-qualified, never verified.
            const betaChip = orgCard(page, 'E2E Beta Labs').getByTestId('requirement-work_location.modes');
            await expect(betaChip).toHaveAttribute('data-status', 'match');
            await expect(betaChip).toHaveAttribute('data-evidence-state', 'stale');
            await expect(betaChip).toContainText(/Work mode · ! Stale \(last verified [A-Z][a-z]{2} \d{1,2}, \d{4}\)/);
            await expect(betaChip).toHaveClass(/job-match-chip--unverified/);
            await expect(betaChip.locator('.evidence-badge-verified')).toHaveCount(0);
            await expect(betaChip).toHaveAttribute('aria-label', /^Work mode: match, Stale, last verified /);

            // Gamma has no fact at all: a plain unknown chip.
            const gammaChip = orgCard(page, 'E2E Gamma Works').getByTestId('requirement-work_location.modes');
            await expect(gammaChip).toHaveAttribute('data-status', 'unknown');
            await expect(gammaChip.locator('.job-match-chip-qualifier')).toHaveCount(0);

            await expect(panel.locator('.job-match-figure-label').first()).toHaveText('Company score (preset)');
            await expect(panel.locator('.job-match-figure-label').nth(2)).toHaveText('Requirement coverage');
            await expect(page.getByTestId('evidence-changed-notice')).toHaveCount(0);
            await expectNoHorizontalOverflow(page);
        });

        test('replaced evidence reads "Evidence changed — refresh" until matches are refreshed', async ({page}) => {
            // The stored-match case: the response still cites a row that has
            // since been superseded.
            const supersede = async (route: Route) => {
                const response = await fetchFromServer(route);
                const body = await response.json();
                for (const match of [...body.job_matches, ...body.organization_matches]) {
                    for (const requirement of match.requirements) {
                        if (requirement.source_kind === 'evidence') {
                            requirement.evidence_status = {state: 'superseded', last_verified_at: null, source_domain: null};
                        }
                    }
                }
                await route.fulfill({response, json: body});
            };
            await page.route(RANKED, supersede);
            await openMatches(page);

            const jobChip = page.getByTestId('ranked-job-matches').getByTestId('requirement-work_location.modes').first();
            await expect(jobChip).toContainText('Work mode · Evidence changed — refresh');
            await expect(jobChip).toHaveClass(/job-match-chip--changed/);
            await expect(jobChip).not.toHaveClass(/job-match-chip--match/);
            const notice = page.getByTestId('evidence-changed-notice');
            await expect(notice).toHaveCount(1);
            await expectNoHorizontalOverflow(page);

            const refresh = notice.getByRole('button', {name: 'Refresh matches'});
            const box = await refresh.boundingBox();
            expect(box!.height).toBeGreaterThanOrEqual(44);
            await page.unroute(RANKED, supersede);
            await refresh.click();
            await expect(notice).toHaveCount(0);
            await expect(jobChip).toContainText('Sourced, not confirmed');
        });

        test('assistant organization cards show the summary, and old replies still render', async ({page}) => {
            const summary = {
                verified: 0, stale: 2, unknown: 5, total: 7, fact_coverage: 2,
                last_verified_at: '2024-08-08T12:00:00+00:00', pending_review: 1,
            };
            const withCards = async (route: Route) => {
                if (route.request().method() !== 'GET') return route.fallback();
                const response = await fetchFromServer(route);
                const body = await response.json();
                body.messages = [
                    {id: 900001, role: 'user', content: 'Which companies fit?', preferences_changed: false,
                     created: '2026-01-01T00:00:00Z', results: null, idempotency_key: 'e2e-473b', delivery_state: 'completed'},
                    {id: 900002, role: 'assistant', content: 'Two to look at.', preferences_changed: false,
                     created: '2026-01-01T00:00:01Z', results: {jobs: [], organizations: [
                         {id: 1, name: 'E2E Beta Labs', url: '', funding_round: 'X', rto_policy: 'O', evidence: summary},
                         {id: 2, name: 'E2E Old Reply Co', url: '', funding_round: 'O', rto_policy: 'H'},
                     ]}},
                ];
                await route.fulfill({response, json: body});
            };
            // The first visit creates the account's conversation; the stored
            // history is then served with two assistant organization results.
            await page.goto('/chat/');
            await expect(page.getByRole('button', {name: 'Send message'})).toBeVisible();
            await page.route('**/api/agent/conversations/', withCards);
            await page.reload();

            const beta = page.getByRole('article', {name: 'Organization: E2E Beta Labs'});
            await expect(beta).toBeVisible();
            await expect(beta).toContainText('Series G or Later · In-Office');
            await expect(beta.locator('.evidence-badge-stale')).toContainText('Stale');
            await expect(beta.getByTestId('evidence-facts')).toHaveText('0 verified · 2 stale · 5 unknown');
            await expect(beta.getByTestId('evidence-last-verified')).toHaveText('Last verified Aug 8, 2024');
            await expect(beta.getByTestId('evidence-pending')).toContainText('Pending review');
            await expect(beta.locator('.evidence-badge-verified')).toHaveCount(0);

            const old = page.getByRole('article', {name: 'Organization: E2E Old Reply Co'});
            await expect(old).toContainText('Other Private · Hybrid');
            await expect(old).toContainText('Fact status was not recorded for this reply.');
            await expect(old.locator('.evidence-badge')).toHaveCount(0);
            await expectNoHorizontalOverflow(page);
        });
    });
}
