// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Django-backed Playwright tier (issue #491).
//
// Second E2E harness alongside the fast fixture tier (playwright.config.ts +
// webpack.e2e.config.js + http.server 4173), which stays untouched. This tier
// builds the production webpack bundles, migrates a dev/SQLite database,
// seeds deterministic synthetic data (`manage.py seed_e2e`), and boots the
// real Django dev server so specs exercise real pages, real sessions, and
// real persisted state.
//
// The browser is pointed at http://local.crank.fyi:4174 (resolved to
// 127.0.0.1 via --host-resolver-rules) because the dev settings pin the
// CSRF/session cookies to the .crank.fyi domain; a subdomain of the real
// cookie domain keeps the browser accepting those cookies without any
// settings changes or /etc/hosts edits.
import {defineConfig, devices} from '@playwright/test';

// Marks this tier for the specs: the fixture-tier config scans all of ./e2e,
// so the Django specs skip with a named reason when it collects them.
process.env.PW_DJANGO_TIER = '1';

// Deterministic provider-outage pass (issue #491): when armed, the seeded
// Django server fails every assistant turn with a stable 503. A failure pass
// must never reuse an already-running healthy server.
const providerFailure = process.env.CRANK_E2E_PROVIDER_FAILURE === '1';

export default defineConfig({
    testDir: './e2e/django',
    timeout: 60_000,
    fullyParallel: false,
    workers: 1,
    forbidOnly: !!process.env.CI,
    retries: process.env.CI ? 2 : 0,
    reporter: process.env.CI ? [['list'], ['html', {open: 'never'}]] : 'list',
    use: {
        baseURL: 'http://local.crank.fyi:4174',
        trace: 'on-first-retry',
    },
    webServer: {
        command: [
            'npx webpack',
            'python3 manage.py migrate --noinput',
            'python3 manage.py seed_e2e',
            'python3 manage.py runserver 127.0.0.1:4174 --noreload',
        ].join(' && '),
        url: 'http://127.0.0.1:4174/',
        reuseExistingServer: !process.env.CI && !providerFailure,
        timeout: 300_000,
        env: {
            ENV: 'dev',
            DJANGO_SETTINGS_MODULE: 'crank.settings',
            // Throwaway dev-only key for the local e2e server; never used in
            // any deployed environment. Override with SECRET_KEY if desired.
            SECRET_KEY: process.env.SECRET_KEY || 'e2e-throwaway-dev-key',
            REDIS_MASTER_URL: process.env.REDIS_MASTER_URL || 'redis://localhost:6379/0',
            // The seeded tier exercises the real assistant send path (demo
            // provider in dev): the interactive-agent feature flag must be on
            // or the advisory status endpoint classifies replies_disabled and
            // the composer is gated (issue #491 tier repair for #465). The
            // provider-outage pass still overrides the provider itself via
            // CRANK_E2E_PROVIDER_FAILURE, so the outage contract is unchanged.
            INTERACTIVE_AGENT_ENABLED: process.env.INTERACTIVE_AGENT_ENABLED || '1',
            CRANK_E2E_PROVIDER_FAILURE: providerFailure ? '1' : '0',
        },
    },
    projects: [
        {
            name: 'chromium',
            use: {
                ...devices['Desktop Chrome'],
                launchOptions: {
                    args: ['--host-resolver-rules=MAP local.crank.fyi 127.0.0.1'],
                },
            },
        },
    ],
});
