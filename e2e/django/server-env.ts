// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Server settings shared by playwright.django.config.ts and by specs that run
// fixture commands against the same database (no Playwright import, so the
// config can load it).

/**
 * A second server on the same seeded database with stored match reads and
 * inline recompute on (issue #473). Only that configuration can publish a
 * generation and re-check it, which the "Evidence changed" refresh needs; the
 * main server keeps both off, like production's default.
 */
export const DJANGO_STORED_BASE_URL = 'http://local.crank.fyi:4175';

/** Environment shared by both servers and by fixture commands run from specs. */
export function djangoServerEnv(): Record<string, string> {
    return {
        ENV: 'dev',
        DJANGO_SETTINGS_MODULE: 'crank.settings',
        // Throwaway dev-only key for the local e2e server; never used in
        // any deployed environment. Override with SECRET_KEY if desired.
        SECRET_KEY: process.env.SECRET_KEY || 'e2e-throwaway-dev-key',
        REDIS_MASTER_URL: process.env.REDIS_MASTER_URL || 'redis://localhost:6379/0',
    };
}
