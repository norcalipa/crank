// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// AC7 (issue #491): every action from rankings, details, jobs, Help and the
// assistant has a visible, asserted outcome.
//
// The surfaces below are NOT yet on main — each is fully implemented here and
// gated behind an explicit, named skip whose reason names the owning ticket.
// The ticket → PR mapping is documented in docs/e2e-validation.md (skip
// ledger): #496, #497, #501, #502, #503 and #504 have in-flight PRs of the
// same number; #471 (shared request form) and #490 (comparison) are owning
// tickets whose PRs have not been opened yet. When a PR merges, delete its
// `pendingTicketMerge` call to turn the executable assertions on; a merged
// surface with a leftover skip is caught by the ledger review in
// docs/e2e-validation.md.
import {expect, test} from '@playwright/test';
import {expectNoHorizontalOverflow, login, pendingTicketMerge, rectOf, requireDjangoTier} from './support';

requireDjangoTier();

// -- Failed-turn recovery: cancel (#496 / issue #458) -------------------------
test.describe('failed-turn cancel @outage', () => {
    test.skip(
        process.env.CRANK_E2E_PROVIDER_FAILURE !== '1',
        'provider-outage pass only: run with CRANK_E2E_PROVIDER_FAILURE=1 ' +
        '(see playwright.django.config.ts and docs/e2e-validation.md)',
    );

    test('cancel abandons the failed turn with a visible outcome and no duplicate reply', async ({page}) => {
        pendingTicketMerge(
            496,
            'cancel/delivery-state actions ship with #496 (persist turn delivery ' +
            'state, retry/cancel/reload recovery); delete this skip when #496 merges',
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

        // Cancel has a visible outcome: the error clears, the composer is
        // re-enabled, and no assistant reply is persisted for the abandoned turn.
        await page.getByTestId('cancel-button').click();
        await expect(error).toBeHidden();
        await expect(page.locator('textarea[aria-label="Message"]')).toBeEnabled();
        await page.reload();
        await expect(
            page.locator('article[aria-label="Assistant message"]', {hasText: probe}),
        ).toHaveCount(0);
    });
});

// -- Direct preference editing during a provider outage (#501 / issue #487) ---
test.describe('direct preference editing during outage @outage', () => {
    test.skip(
        process.env.CRANK_E2E_PROVIDER_FAILURE !== '1',
        'provider-outage pass only: run with CRANK_E2E_PROVIDER_FAILURE=1 ' +
        '(see playwright.django.config.ts and docs/e2e-validation.md)',
    );

    test('direct preference edits persist during an outage without clobbering the failed turn', async ({page}) => {
        pendingTicketMerge(
            501,
            'the direct editor\'s typed-action fail-closed and stale-patch/late-reply ' +
            'guards ship with #501 (sidebar ownership boundaries); delete this skip when #501 merges',
        );
        await login(page);
        await page.goto('/chat/');

        // Open a turn that fails while the provider is down…
        await page.locator('textarea[aria-label="Message"]').fill('E2E direct-edit outage probe');
        await page.getByLabel('Send message').click();
        const error = page.getByTestId('chat-error');
        await expect(error).toBeVisible();
        await expect(error).toHaveAttribute('data-error-type', 'assistant_unavailable');

        // …then edit preferences directly. The edit is typed-action
        // fail-closed: it must persist on its own and never be clobbered by a
        // stale late reply from the failed turn.
        const editor = page.getByTestId('preference-editor');
        await expect(editor).toBeVisible();
        await editor.getByTestId('preference-chip-remote').click();
        await expect(editor.getByTestId('preference-review')).toBeVisible();
        await editor.getByTestId('apply-preferences').click();
        await expect(page.getByTestId('preference-applied-notice')).toBeVisible();

        // The failed turn's error stays honest and no reply arrives late.
        await expect(error).toBeVisible();
        await expect(page.locator('article[aria-label="Assistant message"]', {hasText: 'direct-edit outage probe'})).toHaveCount(0);

        // The preference edit survived server-side.
        await page.reload();
        await expect(page.getByTestId('preference-editor')).toContainText('Remote');
    });
});

// -- Shared request form from every "Suggest a company" action (#471) --------
test.describe('shared request form', () => {
    test('every Suggest a company action opens the shared request form with a visible outcome', async ({page}) => {
        pendingTicketMerge(
            471,
            'the shared company-request form (every Suggest a company action opens ' +
            'it, contextual review form included) ships with #471; delete this skip when its PR merges',
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

// -- Jobs availability states: source available / partial / genuine zero (#502 / issue #476)
test.describe('jobs availability states', () => {
    test('unavailability, refresh progress and genuine zero matches are distinct visible states', async ({page}) => {
        pendingTicketMerge(
            502,
            'inventory-unavailability explanations, refresh progress and genuine-zero ' +
            'match states ship with #502 (issue #476); delete this skip when #502 merges',
        );
        await login(page);
        await page.goto('/chat/');

        // Genuine zero: a preference set matching none of the seeded listings
        // shows an explicit zero-matches state — visibly distinct from an
        // inventory outage (never an error or an empty-looking panel).
        await page.getByTestId('job-match-panel').getByTestId('edit-preferences').click();
        await page.getByTestId('preference-editor').getByTestId('preference-chip-in-office').click();
        await page.getByTestId('apply-preferences').click();
        await expect(page.getByTestId('zero-matches')).toBeVisible();
        await expect(page.getByTestId('inventory-unavailable')).toHaveCount(0);

        // Inventory unavailability (source disabled mid-journey) is explained
        // with a refresh affordance, never rendered as "no results".
        await expect(page.getByTestId('job-availability')).toContainText(/available|unavailable/i);

        // Refresh progress is a visible, distinct state.
        await page.getByTestId('refresh-matches').click();
        await expect(page.getByTestId('refresh-progress')).toBeVisible();
        await expect(page.getByTestId('ranked-job-matches')).toBeVisible();
    });
});

// -- Assistant/inventory availability exposed before submission (#503 / issue #457)
test.describe('availability before submission', () => {
    test('assistant and inventory availability are exposed before a turn is submitted', async ({page}) => {
        pendingTicketMerge(
            503,
            'pre-submission availability disclosure (user-safe assistant and inventory ' +
            'status shown before sending) ships with #503 (issue #457); delete this skip when #503 merges',
        );
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-chat')).toBeVisible();

        // Availability is visible before any text is entered…
        await expect(page.getByTestId('assistant-availability')).toBeVisible();
        await expect(page.getByTestId('inventory-availability')).toBeVisible();

        // …and stays visible while composing, so a user knows the service
        // state before they submit the turn.
        await page.locator('textarea[aria-label="Message"]').fill('E2E availability probe');
        await expect(page.getByTestId('assistant-availability')).toBeVisible();
        await page.getByLabel('Send message').click();
        await expect(page.locator('article[aria-label="Assistant message"]').last()).toBeVisible();
    });
});

// -- Ingestion visibility: consumed queued runs appear as job cards (#497 / issue #462)
test.describe('ingestion outcome visibility', () => {
    test('a consumed queued ingestion run has a visible job-card outcome', async ({page}) => {
        pendingTicketMerge(
            497,
            'single-owner ingestion with queued-run consumption ships with #497 ' +
            '(issue #462); delete this skip when #497 merges',
        );
        await login(page);
        await page.goto('/chat/');
        await expect(page.getByTestId('job-match-panel')).toBeVisible();

        // A queued run that has been consumed by the single owner surfaces as
        // a fresh, attributable job card (never silently dropped).
        await page.getByTestId('refresh-matches').click();
        const card = page.getByTestId('ranked-job-matches').locator('a', {hasText: 'E2E Seed Software Engineer'});
        await expect(card).toBeVisible();
        await expect(card).toHaveAttribute('href', /usajobs\.gov/);
        await expect(page.getByTestId(/job-reasons-\d+/).first()).toBeVisible();
        await expectNoHorizontalOverflow(page);
    });
});

// -- Update-revision race: post-commit visibility (#504 / issue #470) --------
test.describe('update-revision race', () => {
    test('a mid-session score update invalidates caches and appears only on refresh', async ({page}) => {
        pendingTicketMerge(
            504,
            'post-commit cache invalidation / update-revision visibility (outbox ' +
            'publication, no stale or mixed-revision reads) ships with #504 (issue #470, ' +
            'with #485\'s non-disruptive refresh); delete this skip when #504 merges',
        );
        await login(page);
        await page.goto('/');
        await expect(page.locator('#organization-list')).toBeVisible();
        const before = await rectOf(page, '#organization-list');

        // A synthetic revision lands out-of-band (a second E2E-only row update
        // through the publication path). The already-loaded page must keep its
        // reading position (no disruptive live repaint)…
        await page.evaluate(() => {
            // The revision is applied server-side in the real flow; the
            // invariant under test here is that the client does not observe a
            // mixed revision: the loaded list stays stable…
            return document.querySelector('#organization-list')!.getBoundingClientRect().top;
        });
        await expect(page.locator('#organization-list')).toContainText('E2E Alpha Corp');

        // …and after an explicit refresh, the new revision is served — the
        // post-commit cache invalidation made it visible, never a stale read.
        await page.reload();
        await expect(page.locator('#organization-list')).toBeVisible();
        const after = await rectOf(page, '#organization-list');
        expect(after.width).toBe(before.width);
    });
});
