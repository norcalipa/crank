<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# E2E Validation Record — Django-backed Playwright tier (#491)

Date: 2026-09-15 (implementation run)
Scope: two-tier browser harness — the pre-existing fixture tier (untouched) plus a new
Django-backed tier exercising real pages, real sessions, real persistence, and
production-style webpack bundles.

> **Accessibility caveat (deliberate):** the a11y specs in this tier assert DOM
> semantics, keyboard behavior, and live regions only. Emulated browser checks and
> screenshots do **not** certify accessibility. Representative real screen-reader and
> real-device passes remain manual follow-ups; see "Manual evidence pending" below.

## Harness inventory

| Tier | Config | Server | Data |
| --- | --- | --- | --- |
| Fixture (existing, untouched) | `playwright.config.ts` + `webpack.e2e.config.js` | `python3 -m http.server 4173` | static fixtures |
| Django (new) | `playwright.django.config.ts` | `npx webpack && python3 manage.py migrate && python3 manage.py seed_e2e && python3 manage.py runserver 127.0.0.1:4174 --noreload` | deterministic `seed_e2e` dataset (synthetic, "E2E "-prefixed, dev-only) |

The Django tier is reached at `http://local.crank.fyi:4174` (Chromium
`--host-resolver-rules` maps it to 127.0.0.1 so dev settings' `.crank.fyi` cookies are
accepted without settings or hosts-file changes).

## Commands and results (this implementation run)

All commands executed locally from the repository root, `ENV=dev`, SQLite, Redis on
localhost:6379.

| Command | Result |
| --- | --- |
| `npx webpack` | compiled successfully (webpack 5.95.0, both `main` and `jobsearch`/`jobmatch` bundles) |
| `npx playwright test --config=playwright.django.config.ts --project=chromium --grep-invert '@outage'` | **32 passed, 12 skipped (40.3s)** — every skip carries an explicit named reason (pending #500/#496/#471/#464 merges, the eight AC7 ticket-owned skips, capture-pass-only) |
| `CRANK_E2E_PROVIDER_FAILURE=1 npx playwright test --config=playwright.django.config.ts --project=chromium --grep '@outage'` | **1 passed, 3 skipped (8.6s)** — the server-persistence assertion passes against the real outage server (stable `AssistantUnavailable` via the env-gated, **dev-only** hook); the cancel/direct-edit and retry-flow assertions stay skipped pending #496/#501 |
| `npx playwright test --project=chromium` (fixture tier, unchanged files) | **49 passed, 0 failed**; 48 skipped (all named-reason skips: Django-tier specs collected by the fixture config, including the 8 new AC7 specs). Full-run webkit `browserType.launch` errors remain host environment issues (missing system libraries for WebKit), unrelated to this branch |
| `npx jest` | **180 passed (7 suites)** |
| `ENV=dev DJANGO_SETTINGS_MODULE=crank.settings SECRET_KEY=… REDIS_MASTER_URL=redis://localhost:6379/0 python3 -m pytest -q` | **1891 passed, 36 subtests passed in 169.17s** (review-round delta: +5 — two seeder determinism tests, three provider-hook inertness tests) |
| `.github/workflows/playwright-django.yml` YAML lint | `pull_request` trigger mirrors `push` paths exactly (parsed + asserted via PyYAML `safe_load`) |
| `git diff --check` | clean |
| trailing-newline / license-header check on all new files | clean |

New backend tests introduced by this branch (subset of the pytest run):
`crank/tests/management/test_seed_e2e.py` (dev guard for prod/staging, bounded dataset
shape, idempotency, rerun-twice identical state, deliberate-drift repair, password
rotation, bounded secret-free summary) and `crank/tests/agents/test_providers.py`
#491 cases (hook default-off, exact-value arming, fires before provider selection,
non-`1` values inert, and **prod/staging inertness**: with the env var set, a fully
configured orchestrator still builds in prod/staging and the demo path raises the
ordinary non-dev disable message — the hook branch never runs outside dev).

## CI

`.github/workflows/playwright-django.yml` runs the same two passes on
`ubuntu-latest` (Node 20 / Python 3.12 / Redis service): normal pass with
`--grep-invert '@outage'`, then `CRANK_E2E_PROVIDER_FAILURE=1 --grep '@outage'`, then a
best-effort sanitized-capture pass. It is triggered both by pushes to `main`
and by **pull requests targeting `main`** (same path filter — exact parity
between the two triggers is asserted by `crank/tests/test_django_e2e_workflow.py`),
and the filter covers the system under test as well as the harness
(`static/js/**`, `static/css/**`, `templates/**`, `crank/views/**`, the webpack
configs, `package-lock.json`), so a UI-only regression cannot merge without
starting this workflow. The Django tier
is an actual PR merge gate — a regression in these files cannot merge before
the workflow executes. Artifacts: `playwright-report/` (on failure) and
`e2e/artifacts/captures/` (always).

## Skip ledger (named, greppable)

Every skip names its owning ticket. Tickets with an in-flight PR carry that PR's
number (review-disposition round: PRs #496, #497, #500, #501, #502, #503, #504);
#471 (shared request form) and #490 (comparison) are owning tickets whose PRs have
not been opened yet. **Unskip mechanism:** each skip is a single
`pendingTicketMerge(ticket, …)` call in the spec file listed below — delete that
call when the owning PR merges and the executable assertions turn on. The
implementation behind every skip is already written; nothing passes silently.

| Surface | Owning ticket / PR | Reason (greppable) | Where |
| --- | --- | --- | --- |
| #500 dialog z-order vs nav rail/toggle | #500 | `pending #500 merge: the dialog paints under the nav rail…` | `e2e/django/regression.spec.ts` |
| #496 failed-turn immediate-visibility + Retry | #496 | `pending #496 merge: the optimistic turn is rolled back on failure…` | `e2e/django/regression.spec.ts` |
| #471/#477 assistant sidebar open/closed | #471 | `pending #471 merge: the desktop assistant sidebar…` | `e2e/django/viewport-zoom.spec.ts` |
| #464 SuggestCompanyModal Escape hardening | #500 (fixes #464) | `pending #464 merge: SuggestCompanyModal Escape/focus-return hardening…` | `e2e/django/a11y-keyboard.spec.ts` |
| AC7: failed-turn cancel | #496 | `pending #496 merge: the persisted failed-turn delivery state…` (selectors verified at head fa8714d — Retry / "Edit as new message" / Stop; no `cancel-button`) | `e2e/django/ac7-surfaces.spec.ts` |
| AC7: independent direct action during outage | #501 | `pending #501 merge: the typed-action fail-closed and stale-patch/late-reply guards…` (head eec6baf ships no preference-editor UI; the test pins that an independent direct action never clobbers a failed turn) | `e2e/django/ac7-surfaces.spec.ts` |
| AC7: shared request form from the rankings Suggest action | #471 (PR pending) | `pending #471 merge: the shared company-request form…` (pins the rankings entry point; extend to the remaining Suggest actions when the form lands) | `e2e/django/ac7-surfaces.spec.ts` |
| AC7: company comparison vs saved priorities | #490 (PR pending) | `pending #490 merge: the 2–4 company comparison surface…` | `e2e/django/ac7-surfaces.spec.ts` |
| AC7: jobs availability states (refresh progress + honest result states) | #502 | `pending #502 merge: the distinct match-panel availability states…` (selectors verified at head c971eb9: job-match-refresh / refresh-notice / empty-state-*; the genuine-zero and partial-coverage variants need data-dependent fixtures) | `e2e/django/ac7-surfaces.spec.ts` |
| AC7: assistant availability disclosed before submission (healthy + outage) | #503 | `pending #503 merge: pre-submission availability disclosure…` (selectors verified at head 96390aa: assistant-status-notice / assistant-status-retry) | `e2e/django/ac7-surfaces.spec.ts` |
| AC7: ingestion outcome visibility (queued runs) | #497 | `pending #497 merge: single-owner ingestion with queued-run consumption…` (head ed4ee0e: job-match-refresh, job-reasons-<listing_id>) | `e2e/django/ac7-surfaces.spec.ts` |
| AC7: update-revision race (post-commit cache) | #504 | `pending #504 merge: post-commit cache invalidation…` — **explicitly non-executable from the browser harness**: no browser-reachable seam can create a score revision (head 2f130e8 has no Score-mutating view), so deleting the skip without wiring a real revision trigger fails loudly instead of passing vacuously | `e2e/django/ac7-surfaces.spec.ts` |
| Non-outage pass outage specs | — | `provider-outage pass only: run with CRANK_E2E_PROVIDER_FAILURE=1…` | `e2e/django/regression.spec.ts`, `e2e/django/ac7-surfaces.spec.ts` |
| Fixture-tier collection of Django specs | — | `Django tier only: requires the seeded Django server…` | `e2e/django/support.ts` (`requireDjangoTier`) |
| Capture pass in normal runs | — | `evidence capture pass only: set PW_CAPTURE_DIR…` | `e2e/django/captures.spec.ts` |

The zoom scenarios are no longer skipped for non-Chromium browsers: the 200%
text-zoom scenario uses root font-size scaling and the 400% browser-zoom
scenario uses a dedicated 320 CSS px / deviceScaleFactor 4 context — both are
cross-browser mechanisms (this tier's project matrix is Chromium regardless).

## Historical note

The fixture tier's own results remain dated to their original introduction (#431);
nothing in that tier was modified or re-baselined by #491.

## Manual evidence pending (does not block this PR)

- Real screen-reader pass (NVDA on Windows, VoiceOver on macOS/iOS) over the rankings,
  dialog, and chat surfaces — record browser/OS/build and concrete results here.
- Real-device pass on a small Android and iOS device for the 320/375/390 breakpoints.
- Both are manual by design; automation here pins the DOM/keyboard semantics those
  passes rely on, and sanitized captures (below) provide the visual baseline per
  breakpoint/state.

## Sanitized captures

`e2e/django/captures.spec.ts` produces per-breakpoint screenshots of the rankings,
details dialog, chat, match panel, and sign-in surfaces. Every page it shoots contains
only the synthetic "E2E …" seed data, so captures are sanitized by construction.
Local run: `PW_CAPTURE_DIR=<dir> npx playwright test --config=playwright.django.config.ts
--project=chromium e2e/django/captures.spec.ts`.
