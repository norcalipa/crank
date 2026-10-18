// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// AC7 (issue #491): every action from rankings, details, jobs, Help and the
// assistant has a visible, asserted outcome.
//
// The surfaces below ship on main, except the company comparison (#490) and
// the update-revision race (#504), whose tests stay skipped under a named
// ticket. The skip ledger in docs/e2e-validation.md lists every skip.
import {expect, test} from '@playwright/test';
import {expectNoHorizontalOverflow, login, pendingTicketMerge, requireDjangoTier} from './support';

requireDjangoTier();

const OUTAGE_SKIP_REASON =
    'provider-outage pass only: run with CRANK_E2E_PROVIDER_FAILURE=1 ' +
    '(see playwright.django.config.ts and docs/e2e-validation.md)';

// -- Failed-turn recovery: cancel (#496 / issue #458) -------------------------
test.describe('failed-turn cancel @outage', () => {
    test.skip(process.env.CRANK_E2E_PROVIDER_FAILURE !== '1', OUTAGE_SKIP_REASON);

    test('cancel abandons the failed turn with a visible outcome and no duplicate reply', async ({page}) => {
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();

        // Failed turns accumulate in e2e_user's conversation across retries and
        // re-runs, so every locator below is scoped to this run's own probe.
        const probe = `E2E cancel probe ${Date.now()}`;
        await page.locator('textarea[aria-label="Message"]').fill(probe);
        await page.getByLabel('Send message').click();

        const error = page.getByTestId('chat-error');
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');

        // The turn is persisted server-side as failed: the failed-turn panel
        // exposes the delivery-state recovery actions.
        const sent = page.locator('article[aria-label="Your message"]', {hasText: probe});
        await expect(sent).toHaveCount(1);
        await expect(sent.getByTestId('failed-turn')).toBeVisible();

        // Cancel has a visible outcome: "Edit as new message" abandons the
        // failed delivery and hands the content back to a focused composer,
        // while no assistant reply is persisted for the abandoned turn.
        await sent.getByTestId('edit-as-new-button').click();
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toHaveValue(probe);
        await expect(composer).toBeFocused();
        await expect(
            page.locator('article[aria-label="Assistant message"]', {hasText: probe}),
        ).toHaveCount(0);

        // The failed turn itself survives the reload (persisted delivery
        // state), still without any assistant reply.
        await page.reload();
        await expect(sent).toHaveCount(1);
        await expect(sent.getByTestId('failed-turn')).toBeVisible();
        await expect(
            page.locator('article[aria-label="Assistant message"]', {hasText: probe}),
        ).toHaveCount(0);
    });
});

// -- Independent direct actions during a provider outage (#501 / issue #487) --
test.describe('direct action during outage @outage', () => {
    test.skip(process.env.CRANK_E2E_PROVIDER_FAILURE !== '1', OUTAGE_SKIP_REASON);

    test('an independent direct action leaves the failed turn honest and unclobbered', async ({page}) => {
        await login(page);
        await page.goto('/chat/');

        // Open a turn that fails while the provider is down…
        const probe = `E2E direct-action outage probe ${Date.now()}`;
        await page.locator('textarea[aria-label="Message"]').fill(probe);
        await page.getByLabel('Send message').click();
        const error = page.getByTestId('chat-error');
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');
        const sent = page.locator('article[aria-label="Your message"]', {hasText: probe});
        await expect(sent.getByTestId('failed-turn')).toBeVisible();

        // …then perform an independent direct action in the sibling jobs
        // panel. The action is fail-closed: it must succeed on its own and
        // never clobber the failed turn's error or conjure a late reply.
        await page.getByTestId('job-match-refresh').click();
        await expect(page.getByTestId('ranked-job-matches')).toBeVisible();

        // The failed turn's error stays honest and no reply arrives late.
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');
        await expect(
            page.locator('article[aria-label="Assistant message"]', {hasText: probe}),
        ).toHaveCount(0);

        // The failed turn is still recoverable after the direct action. The
        // footer error is client state and is not restored on load: what a
        // reload shows is the persisted turn with an enabled Retry.
        await page.reload();
        await expect(sent).toHaveCount(1);
        await expect(sent.getByTestId('failed-turn')).toBeVisible();
        await expect(sent.getByTestId('retry-response-button')).toBeEnabled();
        await expect(page.getByTestId('chat-error')).toHaveCount(0);
        await expect(
            page.locator('article[aria-label="Assistant message"]', {hasText: probe}),
        ).toHaveCount(0);
    });
});

// -- Shared request form from every "Suggest a company" entry point (#471) --
test.describe('shared request form', () => {
    test('the rankings toolbar action opens the shared request form with a visible outcome', async ({page}) => {
        await login(page);
        await page.goto('/');

        // From the rankings toolbar…
        await page.getByTestId('suggest-company-btn').click();
        const modal = page.getByTestId('suggest-company-modal');
        await expect(modal).toBeVisible();

        // …submitting the shared form has a visible, asserted outcome. A
        // unique name avoids the real duplicate-detection response
        // (crank/views/company_requests.py) colliding across test runs
        // against a reused dev server.
        await modal.locator('#suggest-company-name').fill(`E2E Form Probe Co ${Date.now()}`);
        await modal.locator('#suggest-website-url').fill(`https://e2e-form-probe-${Date.now()}.example.com`);
        await modal.getByTestId('suggest-submit-btn').click();
        await expect(modal.getByTestId('suggest-success')).toBeVisible();
    });

    test('the rankings empty-state action opens the same shared form', async ({page}) => {
        await login(page);
        await page.goto('/?search=E2E+Nonexistent+Company+Probe');

        const emptyState = page.getByRole('alert').filter({hasText: 'No organizations found'});
        await expect(emptyState).toBeVisible();
        await emptyState.getByRole('button', {name: 'Suggest a company'}).click();
        await expect(page.getByTestId('suggest-company-modal')).toBeVisible();
    });

    test('the company details "Suggest a correction" action opens the correction form for that company over the company view', async ({page}) => {
        await login(page);
        await page.goto('/');

        await page.locator('tr[aria-label="View details for E2E Alpha Corp"]').click();
        const details = page.getByTestId('company-view');
        await expect(details).toBeVisible();

        await details.getByTestId('suggest-correction-link').click();
        const form = page.getByRole('dialog', {name: /Suggest a correction E2E Alpha Corp/});
        await expect(form).toBeVisible();
        await expect(details).toBeVisible(); // 486b: the view stays under the form
        await expect(form.getByTestId('correction-form')).toBeVisible();
    });
});

// -- Company comparison against saved priorities (#490) -----------------------
test.describe('company comparison', () => {
    test('comparing two seeded companies shows scoped requirements and scores', async ({page}) => {
        pendingTicketMerge(
            490,
            'the 2–4 company comparison surface (compared against the same saved ' +
            'priorities, with explicit zero-month-cliff requirements) ships with #490; ' +
            'delete this skip when its PR merges',
        );
        await login(page);
        await page.goto('/');
        await expect(page.locator('#organization-list')).toBeVisible();

        // Select two seeded organizations for comparison…
        await page.getByTestId('compare-select-E2E Alpha Corp').check();
        await page.getByTestId('compare-select-E2E Beta Labs').check();
        await page.getByTestId('compare-selected').click();

        // …the comparison view renders both companies against the saved
        // priorities with per-requirement outcomes, not just scores.
        const view = page.getByTestId('comparison-view');
        await expect(view).toBeVisible();
        await expect(view).toContainText('E2E Alpha Corp');
        await expect(view).toContainText('E2E Beta Labs');
        await expect(view.getByTestId(/comparison-requirement-/).first()).toBeVisible();
        await expectNoHorizontalOverflow(page);
    });
});

// -- Jobs availability states: refresh progress and honest result states (#502 / issue #476)
test.describe('jobs availability states', () => {
    test('refresh progress is a distinct visible state and result states stay honest', async ({page}) => {
        await login(page);
        await page.goto('/chat/');
        const panel = page.getByTestId('job-match-panel');
        await expect(panel).toBeVisible();

        // The seeded remote preference matches the seeded listing: results
        // render as ranked matches, not an empty state — an explicit empty
        // state must never masquerade as "no results" when matches exist.
        const matches = page.getByTestId('ranked-job-matches');
        await expect(matches).toBeVisible();
        await expect(page.getByTestId(/empty-state-/)).toHaveCount(0);

        // Hold the header refresh's ranked read at the network so the running
        // state can be observed; the notice for an operator crawl is a
        // different state and is not what this refresh shows.
        let release: () => void = () => undefined;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        let reached: () => void = () => undefined;
        const held = new Promise<void>((resolve) => { reached = resolve; });
        const pattern = /\/api\/job-matches\/ranked\//;
        await page.route(pattern, async (route) => {
            reached();
            await gate;
            await route.continue();
        });
        const refresh = page.getByTestId('job-match-refresh');
        await refresh.click();
        await held;

        // While the refresh runs the control is inert (aria-disabled keeps its
        // focus), the body is busy, and the current results stay up instead of
        // collapsing into an empty state.
        await expect(refresh).toHaveAttribute('aria-disabled', 'true');
        await expect(panel.locator('.card-body[aria-busy="true"]')).toHaveCount(1);
        await expect(matches).toBeVisible();
        await expect(page.getByTestId(/empty-state-/)).toHaveCount(0);

        // The refresh completes back into ranked results and says so in the
        // visually hidden live region.
        release();
        await expect(panel.locator('[aria-busy="true"]')).toHaveCount(0);
        await expect(refresh).not.toHaveAttribute('aria-disabled', 'true');
        await expect(page.getByTestId('read-announcement')).toHaveText('Finished checking your job matches.');
        await expect(matches).toBeVisible();
        await page.unroute(pattern);
    });
});

// -- Assistant availability disclosed before submission (#503 / issue #457) --
test.describe('availability before submission (healthy)', () => {
    test('the composer is usable and unblocked before submitting when the assistant is up', async ({page}) => {
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();

        // Healthy state discloses readiness by NOT blocking: the advisory
        // notice does not render for a ready assistant (the component returns
        // null), and the composer is usable before any text is entered.
        await expect(page.getByTestId('assistant-status-notice')).toHaveCount(0);
        const replies = page.locator('article[aria-label="Assistant message"]');
        const before = await replies.count();
        const composer = page.locator('textarea[aria-label="Message"]');
        await composer.fill('E2E availability probe');
        await expect(composer).toBeEnabled();
        await page.getByLabel('Send message').click();
        await expect(replies).toHaveCount(before + 1);
        await expect(replies.nth(before)).toBeVisible();
    });
});

test.describe('availability before submission @outage', () => {
    test.skip(process.env.CRANK_E2E_PROVIDER_FAILURE !== '1', OUTAGE_SKIP_REASON);

    test('degraded assistant availability is disclosed before a turn is submitted', async ({page}) => {
        await login(page);
        await page.goto('/chat/');

        // Before ANY text is entered, the advisory notice is visible as an
        // alert region (role=alert since the #472 visual review), so the user
        // knows the service state first.
        const notice = page.getByTestId('assistant-status-notice');
        await expect(notice).toBeVisible();
        await expect(notice).toHaveAttribute('role', 'alert');
        await expect(notice).toHaveAttribute('aria-label', 'Assistant availability');
        await expect(notice).toContainText(/unavailable/i);

        // …and it stays visible while composing, so a user never submits a
        // message into a dead assistant unknowingly.
        await page.locator('textarea[aria-label="Message"]').fill('E2E availability probe');
        await expect(notice).toBeVisible();
    });
});

// -- Update-revision race: post-commit visibility (#504 / issue #470) --------
test.describe('update-revision race', () => {
    test('a mid-session score update invalidates caches and appears only on refresh', async ({page}) => {
        // NON-EXECUTABLE from the browser harness (fix-verification round 2,
        // named per the skip ledger). #504's contract is server-side
        // publication and invalidation: the outbox clears the
        // algorithm_<id>_page full-page key and the algorithm result keys on
        // commit. No browser-reachable seam can create a score revision —
        // verified at the #504 head 2f130e8, where no view mutates Score —
        // so a browser-only flow cannot exercise the race. Deleting this
        // skip without wiring a real revision trigger fails loudly below
        // instead of passing vacuously on rectangle bookkeeping.
        pendingTicketMerge(
            504,
            'post-commit cache invalidation / update-revision visibility ships ' +
            'with #504 (issue #470, with #485\'s non-disruptive refresh); the ' +
            'browser harness cannot drive the server-side revision/publication ' +
            'path, so wire a real revision trigger (or move the contract to the ' +
            'Django unit tier) when deleting this skip',
        );
        throw new Error(
            'non-executable: the #504 update-revision race has no browser-reachable ' +
            'revision trigger; wire the real publication flow (or move the contract ' +
            'to the Django unit tier) before unskipping this test',
        );
    });
});
