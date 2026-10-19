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

## Commands and results (last updated by #492c)

All commands executed locally from the repository root, `ENV=dev`, SQLite (a fresh
file per run), Redis private to the run. The Django rows are re-measured by #492c
(`CI=1`, so retries are on and no server is reused); the other rows are from the
implementation run and are re-run by CI.

| Command | Result |
| --- | --- |
| `npx webpack` | compiled successfully (webpack 5.95.0, both `main` and `jobsearch`/`jobmatch` bundles) |
| `npx playwright test --config=playwright.django.config.ts --project=chromium --grep-invert '@outage'` | **PASSED_PLACEHOLDER** — the skips are the #490 test (named below) and the three capture-pass-only tests; the flaky test (`company-view.spec.ts` "Ask keeps the view…") passed on its retry |
| `CRANK_E2E_PROVIDER_FAILURE=1 npx playwright test --config=playwright.django.config.ts --project=chromium --grep '@outage'` | **7 passed, 0 skipped (29.5s)**; with `--repeat-each=2` on one database: 14 passed (the failed-turn tests scope every locator to their own probe, so a second run passes) |
| `npx playwright test --project=chromium --project=firefox` (fixture tier) | **402 passed, 971 skipped (Django-tier specs collected by the fixture config, named-reason skips), 1 flaky** (Firefox, `job-search-chat-viewport.spec.ts` double press on Delete; passed on retry). WebKit cannot launch on the local host; CI runs it |
| `npx jest` | **2285 passed (50 suites)** |
| `coverage run -m pytest -q` (`ENV=dev`, private Redis) | **4979 passed, 18 skipped** |
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

Every skip names why it is skipped. Ticket-owned skips are a single
`pendingTicketMerge(ticket, …)` call in the spec file listed below; only tickets
that are still open appear here. A ticket-owned skip whose PR has merged is
re-enabled (re-targeted where the product moved) or deleted in favour of a named
successor, never left in place. Skips that depend on the run, not on a ticket,
are listed after them.

| Surface | Owning ticket / PR | Reason (greppable) | Where |
| --- | --- | --- | --- |
| AC7: company comparison vs saved priorities | #490 (open) | `pending #490 merge: the 2–4 company comparison surface…` | `e2e/django/ac7-surfaces.spec.ts` |
| Outage-only specs, in the normal pass | — | `provider-outage pass only: run with CRANK_E2E_PROVIDER_FAILURE=1…` | `e2e/django/regression.spec.ts`, `e2e/django/ac7-surfaces.spec.ts`, `e2e/django/priorities.spec.ts` (the outage describe at the end of the file) |
| Specs that need assistant replies, in the outage pass | — | `needs assistant replies: not part of the provider-outage pass` | `e2e/django/priorities.spec.ts`, `e2e/django/conversation-controls.spec.ts`, `e2e/django/chat-layout.spec.ts` (three describes) |
| Touch input outside Chromium | — | `touch is emulated in Chromium only` | `e2e/django/priorities.spec.ts` (two loops) |
| Fixture-tier collection of Django specs | — | `Django tier only: requires the seeded Django server…` | `e2e/django/support.ts` (`requireDjangoTier`) |
| Capture pass in normal runs | — | `evidence capture pass only: set PW_CAPTURE_DIR…` | `e2e/django/captures.spec.ts` |

Skips removed for merged tickets (#471, #496, #497, #500, #501, #502, #503):

- Re-enabled as written: the two rankings "Suggest a company" tests (#471).
- Re-enabled and re-targeted where the product moved: the composer test of the
  healthy pass (#503), which waits for the history to load before it counts
  replies; failed-turn cancel (#496)
  and the independent direct action (#501), which now check their own probe's
  turn and, after a reload, its enabled Retry (the footer error is client state);
  the jobs refresh test (#502), which holds the ranked read and checks the busy
  state and the hidden announcement; the outage notice test (#503), which expects
  `role="alert"`; and the failed-turn reload test (#496), which also presses Retry
  after the reload. A new outage test presses Retry until the server reports
  the retry cap and recovers with "Edit as new message".
- Deleted, with the test that already covers it: the dialog and nav rail test
  (#500; the dialog is gone) is covered by `e2e/organization-popup.spec.ts` and
  `e2e/django/company-view.spec.ts`; the queued-ingestion card test (#497) by
  `e2e/django/journey.spec.ts::match panel shows the ranked seeded listing with reasons`;
  the sidebar open/closed test (#471) by `e2e/django/workspace.spec.ts::no horizontal overflow at ${width}px with the panel open and closed`.
  The deleted #500 test also checked, at 375 px, that the mobile nav toggle does
  not overlap the company heading. That rectangle-intersection check is dropped:
  `e2e/navigation.spec.ts::topbar band pushes content below it without overlap`
  (the toggle sits inside the reserved band above the content) and
  `e2e/organization-popup.spec.ts::heading and Back to results are fully visible in the first screen and at least 44px tall`
  (the heading is whole in the first screen) cover its neighbours, not the intersection itself.

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
