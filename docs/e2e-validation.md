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
| `npx playwright test --config=playwright.django.config.ts --project=chromium` | **32 passed, 8 skipped (35.3s)** — every skip carries an explicit named reason (pending #500/#496/#471/#464 merges, outage-pass-only, capture-pass-only) |
| `CRANK_E2E_PROVIDER_FAILURE=1 npx playwright test --config=playwright.django.config.ts --project=chromium --grep '@outage'` | **1 passed, 1 skipped (9.5s)** — the server-persistence assertion passes against the real outage server (stable `AssistantUnavailable` via the env-gated hook); the retry-flow assertion stays skipped pending #496 |
| `npx playwright test` (fixture tier, unchanged) | chromium + firefox: **93 passed, 0 failed**; 130 skipped (all named-reason skips: Django-tier specs collected by the fixture config, outage-pass-only, pending-ticket, Chromium-only zoom). The 44 webkit "failures" in this local run are `browserType.launch` environment errors (host missing system libraries for WebKit); they are unrelated to this change — the fixture tier's files are untouched by this branch |
| `npx jest` | **180 passed (7 suites)** |
| `ENV=dev DJANGO_SETTINGS_MODULE=crank.settings SECRET_KEY=… REDIS_MASTER_URL=redis://localhost:6379/0 python3 -m pytest -q` | **1886 passed, 36 subtests passed in 164.84s** |
| `git diff --check` | clean |
| trailing-newline / license-header check on all new files | clean |

New backend tests introduced by this branch (subset of the pytest run):
`crank/tests/management/test_seed_e2e.py` (dev guard for prod/staging, bounded dataset
shape, idempotency, password rotation, bounded secret-free summary) and
`crank/tests/agents/test_providers.py` #491 cases (hook default-off, exact-value
arming, fires before provider selection, non-`1` values inert).

## CI

`.github/workflows/playwright-django.yml` runs the same two passes on
`ubuntu-latest` (Node 20 / Python 3.12 / Redis service): normal pass with
`--grep-invert '@outage'`, then `CRANK_E2E_PROVIDER_FAILURE=1 --grep '@outage'`, then a
best-effort sanitized-capture pass. Artifacts: `playwright-report/` (on failure) and
`e2e/artifacts/captures/` (always).

## Skip ledger (named, greppable)

| Surface | Reason | Where |
| --- | --- | --- |
| #500 dialog z-order vs nav rail/toggle | `pending #500 merge: the dialog paints under the nav rail…` | `e2e/django/regression.spec.ts` |
| #496 failed-turn immediate-visibility + Retry | `pending #496 merge: the optimistic turn is rolled back on failure…` | `e2e/django/regression.spec.ts` |
| #471/#477 assistant sidebar open/closed | `pending #471 merge: the desktop assistant sidebar…` | `e2e/django/viewport-zoom.spec.ts` |
| #464 SuggestCompanyModal Escape hardening | `pending #464 merge: SuggestCompanyModal Escape/focus-return hardening…` | `e2e/django/a11y-keyboard.spec.ts` |
| Non-outage pass outage specs | `provider-outage pass only: run with CRANK_E2E_PROVIDER_FAILURE=1…` | `e2e/django/regression.spec.ts` |
| Fixture-tier collection of Django specs | `Django tier only: requires the seeded Django server…` | `e2e/django/support.ts` (`requireDjangoTier`) |
| Non-Chromium zoom matrix | `CSS zoom emulation is Chromium-only` | `e2e/django/viewport-zoom.spec.ts` |
| Capture pass in normal runs | `evidence capture pass only: set PW_CAPTURE_DIR…` | `e2e/django/captures.spec.ts` |

Each skip text names its owning ticket so it can be deleted when that ticket merges.

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
