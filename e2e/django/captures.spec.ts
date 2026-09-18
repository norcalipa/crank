// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Sanitized evidence captures (issue #491).
//
// Runs only when PW_CAPTURE_DIR is set; every page it shoots contains only
// the synthetic "E2E …" seed data, so captures are sanitized by construction
// (no real user content, no credentials typed on screen). Output feeds
// docs/e2e-validation.md and the CI artifact directory.
import {expect, test} from '@playwright/test';
import {login, requireDjangoTier} from './support';

requireDjangoTier();

const captureDir = process.env.PW_CAPTURE_DIR;

test.skip(!captureDir, 'evidence capture pass only: set PW_CAPTURE_DIR to a writable directory');

const BREAKPOINTS: Array<{name: string; width: number; height: number}> = [
    {name: '320', width: 320, height: 568},
    {name: '375', width: 375, height: 667},
    {name: '390', width: 390, height: 844},
    {name: '768', width: 768, height: 1024},
    {name: '1280', width: 1280, height: 800},
    {name: '1440', width: 1440, height: 900},
];

test.describe.serial('sanitized captures', () => {
    test('rankings and dialog per breakpoint', async ({page}) => {
        await login(page);
        for (const bp of BREAKPOINTS) {
            await page.setViewportSize({width: bp.width, height: bp.height});
            await page.goto('/');
            await expect(page.locator('#organization-list')).toBeVisible();
            await page.screenshot({
                path: `${captureDir}/rankings-${bp.name}.png`,
                fullPage: false,
            });
            if (bp.width >= 768) {
                await page.getByText('E2E Alpha Corp').first().click();
                await expect(page.getByRole('dialog')).toBeVisible();
                await page.screenshot({
                    path: `${captureDir}/dialog-evidence-${bp.name}.png`,
                    fullPage: false,
                });
                await page.keyboard.press('Escape');
            }
        }
    });

    test('chat with match panel and a live turn', async ({page}) => {
        await login(page);
        for (const bp of BREAKPOINTS) {
            await page.setViewportSize({width: bp.width, height: bp.height});
            await page.goto('/chat/');
            await expect(page.getByTestId('job-search-chat')).toBeVisible();
            await page.screenshot({
                path: `${captureDir}/chat-${bp.name}.png`,
                fullPage: false,
            });
        }
        // One representative live turn at desktop width (state: message sent).
        await page.setViewportSize({width: 1280, height: 800});
        await page.locator('textarea[aria-label="Message"]').fill('E2E capture probe turn');
        await page.getByLabel('Send message').click();
        await expect(page.locator('article[aria-label="Assistant message"]').last()).toBeVisible();
        await page.screenshot({path: `${captureDir}/chat-turn-1280.png`, fullPage: false});
    });

    test('sign-in and empty states (anonymous)', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 800});
        await page.goto('/accounts/login/');
        await expect(page.getByRole('button', {name: 'Sign In'})).toBeVisible();
        await page.screenshot({path: `${captureDir}/sign-in-1280.png`, fullPage: false});
        await page.setViewportSize({width: 375, height: 667});
        await page.goto('/accounts/login/');
        await page.screenshot({path: `${captureDir}/sign-in-375.png`, fullPage: false});
    });
});
