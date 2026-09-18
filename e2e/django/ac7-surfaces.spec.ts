// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// AC7 (issue #491): every action from rankings, details, jobs, Help and the
// assistant has a visible, asserted outcome.
//
// The surfaces below are NOT yet on main — each is implemented here and
// gated behind an explicit, named skip whose reason names the owning ticket.
// Every selector below is aligned with the owning PR's ACTUAL head (verified
// against the head trees: #496 fa8714d, #497 ed4ee0e, #501 eec6baf,
// #502 c971eb9, #503 96390aa, #504 2f130e8), so deleting a skip after the
// owning PR merges yields the intended contract, not deterministic failures
// against selectors that never existed. The ticket → PR mapping is
// documented in docs/e2e-validation.md (skip ledger): #496, #497, #501,
// #502, #503 and #504 have in-flight PRs of the same number; #471 (shared
// request form) and #490 (comparison) are owning tickets whose PRs have not
// been opened yet. When a PR merges, delete its `pendingTicketMerge` call to
// turn the executable assertions on; a merged surface with a leftover skip
// is caught by the ledger review in docs/e2e-validation.md.
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
        pendingTicketMerge(
            496,
            'the persisted failed-turn delivery state (failed-turn panel with ' +
            'Retry / "Edit as new message" — the cancel affordance; the in-flight ' +
            'variant is Stop) ships with #496; delete this skip when #496 merges. ' +
            'Selectors verified at the #496 head fa8714d (no cancel-button exists ' +
            'there: delivery-state Retry/Edit/Stop)',
        );
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();

        const probe = `E2E cancel probe ${Date.now()}`;
        await page.locator('textarea[aria-label="Message"]').fill(probe);
        await page.getByLabel('Send message').click();

        const error = page.getByTestId('chat-error');
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');

        // The turn is persisted server-side as failed: the failed-turn panel
        // exposes the delivery-state recovery actions.
        await expect(page.getByTestId('failed-turn')).toBeVisible();

        // Cancel has a visible outcome: "Edit as new message" abandons the
        // failed delivery and hands the content back to a focused composer,
        // while no assistant reply is persisted for the abandoned turn.
        await page.getByTestId('edit-as-new-button').click();
        const composer = page.locator('textarea[aria-label="Message"]');
        await expect(composer).toHaveValue(probe);
        await expect(composer).toBeFocused();
        await expect(
            page.locator('article[aria-label="Assistant message"]', {hasText: probe}),
        ).toHaveCount(0);

        // The failed turn itself survives the reload (persisted delivery
        // state), still without any assistant reply.
        await page.reload();
        await expect(page.getByTestId('failed-turn')).toBeVisible();
        await expect(
            page.locator('article[aria-label="Assistant message"]', {hasText: probe}),
        ).toHaveCount(0);
    });
});

// -- Independent direct actions during a provider outage (#501 / issue #487) --
test.describe('direct action during outage @outage', () => {
    test.skip(process.env.CRANK_E2E_PROVIDER_FAILURE !== '1', OUTAGE_SKIP_REASON);

    test('an independent direct action leaves the failed turn honest and unclobbered', async ({page}) => {
        pendingTicketMerge(
            501,
            'the typed-action fail-closed and stale-patch/late-reply guards ship ' +
            'with #501 (issue #487); delete this skip when #501 merges. Selectors ' +
            'verified at the #501 head eec6baf: that PR ships no direct ' +
            'preference-editor UI (no preference-editor/chip/review/apply test ' +
            'IDs), so the browser-observable contract pinned here is that an ' +
            'independent direct action (the jobs panel refresh) never clobbers, ' +
            'clears, or answers a failed turn',
        );
        await login(page);
        await page.goto('/chat/');

        // Open a turn that fails while the provider is down…
        await page.locator('textarea[aria-label="Message"]').fill('E2E direct-action outage probe');
        await page.getByLabel('Send message').click();
        const error = page.getByTestId('chat-error');
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');

        // …then perform an independent direct action in the sibling jobs
        // panel. The action is fail-closed: it must succeed on its own and
        // never clobber the failed turn's error or conjure a late reply.
        await page.getByTestId('job-match-refresh').click();
        await expect(page.getByTestId('ranked-job-matches')).toBeVisible();

        // The failed turn's error stays honest and no reply arrives late.
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');
        await expect(
            page.locator('article[aria-label="Assistant message"]', {hasText: 'direct-action outage probe'}),
        ).toHaveCount(0);

        // The failed turn is still recoverable after the direct action.
        await page.reload();
        await expect(page.getByTestId('chat-error')).toBeVisible();
        await expect(page.getByTestId('chat-error')).toHaveAttribute(
            'data-error-type',
            'assistant_unavailable',
        );
        await expect(
            page.locator('article[aria-label="Assistant message"]', {hasText: 'direct-action outage probe'}),
        ).toHaveCount(0);
    });
});

// -- Shared request form from the rankings "Suggest a company" action (#471) --
test.describe('shared request form', () => {
    test('the rankings "Suggest a company" action opens the shared request form with a visible outcome', async ({page}) => {
        pendingTicketMerge(
            471,
            'the shared company-request form ships with #471; this test pins the ' +
            'rankings entry point only — extend it to the remaining Suggest ' +
            'actions (chat correction link, help surface) when the form lands. ' +
            'delete this skip when its PR merges',
        );
        await login(page);
        await page.goto('/');

        // From the rankings surface…
        await page.getByTestId('suggest-company-btn').click();
        const form = page.getByTestId('company-request-form');
        await expect(form).toBeVisible();

        // …submitting the shared form has a visible, asserted outcome.
        await form.locator('input[name="company_name"]').fill('E2E Form Probe Co');
        await form.getByRole('button', {name: /submit/i}).click();
        await expect(page.getByTestId('company-request-confirmation')).toBeVisible();
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
        pendingTicketMerge(
            502,
            'the distinct match-panel availability states (refresh-notice, ' +
            'coverage-notice for partial source coverage, inventory-facts, and ' +
            'the empty-state-* variants for genuine zero) ship with #502 ' +
            '(issue #476); delete this skip when #502 merges. Selectors verified ' +
            'at the #502 head c971eb9 (no edit-preferences/preference-editor or ' +
            'zero-matches/inventory-unavailable IDs exist there; the genuine-zero ' +
            'and partial-coverage variants additionally need data-dependent ' +
            'fixtures — a disabled source or non-matching preferences — so extend ' +
            'them when driving fixtures land)',
        );
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-match-panel')).toBeVisible();

        // The seeded remote preference matches the seeded listing: results
        // render as ranked matches, not an empty state — an explicit empty
        // state must never masquerade as "no results" when matches exist.
        const matches = page.getByTestId('ranked-job-matches');
        await expect(matches).toBeVisible();
        await expect(page.getByTestId(/empty-state-/)).toHaveCount(0);

        // Refresh progress is a visible, distinct state (role=status,
        // aria-live=polite) while the refresh runs; the current results stay
        // up instead of collapsing into an empty state.
        await page.getByTestId('job-match-refresh').click();
        const notice = page.getByTestId('refresh-notice');
        await expect(notice).toBeVisible();
        await expect(notice).toHaveAttribute('role', 'status');
        await expect(notice).toHaveAttribute('aria-live', 'polite');
        await expect(matches).toBeVisible();

        // The refresh completes back into ranked results.
        await expect(notice).toBeHidden();
        await expect(matches).toBeVisible();
    });
});

// -- Assistant availability disclosed before submission (#503 / issue #457) --
test.describe('availability before submission (healthy)', () => {
    test('the composer is usable and unblocked before submitting when the assistant is up', async ({page}) => {
        pendingTicketMerge(
            503,
            'pre-submission availability disclosure ships with #503 (issue #457); ' +
            'delete this skip when #503 merges. Selectors verified at the #503 ' +
            'head 96390aa (assistant-status-notice / assistant-status-retry; no ' +
            'assistant-availability or inventory-availability IDs exist there)',
        );
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();

        // Healthy state discloses readiness by NOT blocking: the advisory
        // notice does not render for a ready assistant (the component returns
        // null), and the composer is usable before any text is entered.
        await expect(page.getByTestId('assistant-status-notice')).toHaveCount(0);
        const composer = page.locator('textarea[aria-label="Message"]');
        await composer.fill('E2E availability probe');
        await expect(composer).toBeEnabled();
        await page.getByLabel('Send message').click();
        await expect(page.locator('article[aria-label="Assistant message"]').last()).toBeVisible();
    });
});

test.describe('availability before submission @outage', () => {
    test.skip(process.env.CRANK_E2E_PROVIDER_FAILURE !== '1', OUTAGE_SKIP_REASON);

    test('degraded assistant availability is disclosed before a turn is submitted', async ({page}) => {
        pendingTicketMerge(
            503,
            'pre-submission availability disclosure ships with #503 (issue #457); ' +
            'delete this skip when #503 merges. Selectors verified at the #503 ' +
            'head 96390aa (assistant-status-notice / assistant-status-retry)',
        );
        await login(page);
        await page.goto('/chat/');

        // Before ANY text is entered, the advisory notice is visible as a
        // polite status region, so the user knows the service state first.
        const notice = page.getByTestId('assistant-status-notice');
        await expect(notice).toBeVisible();
        await expect(notice).toHaveAttribute('role', 'status');
        await expect(notice).toHaveAttribute('aria-label', 'Assistant availability');
        await expect(notice).toContainText(/unavailable/i);

        // …and it stays visible while composing, so a user never submits a
        // message into a dead assistant unknowingly.
        await page.locator('textarea[aria-label="Message"]').fill('E2E availability probe');
        await expect(notice).toBeVisible();
    });
});

// -- Ingestion visibility: consumed queued runs appear as job cards (#497 / issue #462)
test.describe('ingestion outcome visibility', () => {
    test('a consumed queued ingestion run has a visible job-card outcome', async ({page}) => {
        pendingTicketMerge(
            497,
            'single-owner ingestion with queued-run consumption ships with #497 ' +
            '(issue #462); delete this skip when #497 merges. Selectors verified ' +
            'at the #497 head ed4ee0e: the refresh affordance is job-match-refresh ' +
            '(not refresh-matches) and per-match reasons are job-reasons-<listing_id>',
        );
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-match-panel')).toBeVisible();

        // A queued run that has been consumed by the single owner surfaces as
        // a fresh, attributable job card (never silently dropped).
        await page.getByTestId('job-match-refresh').click();
        const matches = page.getByTestId('ranked-job-matches');
        await expect(matches).toBeVisible();
        const card = matches.locator('a', {hasText: 'E2E Seed Software Engineer'});
        await expect(card).toBeVisible();
        await expect(card).toHaveAttribute('href', /usajobs\.gov/);
        await expect(page.getByTestId(/job-reasons-\d+/).first()).toBeVisible();
        await expectNoHorizontalOverflow(page);
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
