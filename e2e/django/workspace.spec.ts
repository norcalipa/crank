// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Shared responsive workspace (issue #472): one assistant per document,
// docked >=1280px, drawer 768-1279px, sheet <768px, lazy-loaded on first
// open, with no horizontal overflow at any supported width.
import {expect, test} from '@playwright/test';
import {
    expectNoHorizontalOverflow,
    login,
    rectOf,
    requireDjangoTier,
} from './support';

const OVERFLOW_WIDTHS = [320, 375, 390, 768, 1024, 1280, 1440];

test.describe('shared assistant workspace (issue #472)', () => {
    test.beforeEach(() => {
        requireDjangoTier();
    });

    for (const width of OVERFLOW_WIDTHS) {
        test(`no horizontal overflow at ${width}px with the panel open and closed`, async ({page}) => {
            await page.setViewportSize({width, height: 800});
            await login(page);
            for (const path of ['/', '/chat/']) {
                await page.goto(path);
                const launcher = page.locator('[data-testid="assistant-launcher"]');
                const panel = page.locator('[data-testid="assistant-panel"]');
                // /chat/ is pinned open; other pages open via the launcher.
                // While the panel is open the launcher leaves the DOM
                // (issue #469 re-critique): it renders only while closed, so
                // a pressed floating launcher can never sit beside the
                // already-open panel.
                if (await panel.count() === 0) {
                    await expect(launcher).toBeVisible();
                    await expectNoHorizontalOverflow(page);
                    await launcher.click();
                } else {
                    await expect(launcher).toHaveCount(0);
                }
                await expect(panel).toBeVisible();
                await expectNoHorizontalOverflow(page);
                await page.locator('[data-testid="assistant-close"]').click();
                await expect(page.locator('[data-testid="assistant-panel"]')).toHaveCount(0);
                // Closed again: the launcher returns as the single entry
                // point and the closed state stays overflow-free.
                await expect(launcher).toBeVisible();
                await expectNoHorizontalOverflow(page);
            }
        });
    }

    test('docked geometry: >=640px main region, 360-420px panel, collapsed rail at 1280', async ({page}) => {
        await login(page);
        for (const width of [1280, 1440]) {
            await page.setViewportSize({width, height: 900});
            await page.goto('/');
            await page.locator('[data-testid="assistant-launcher"]').click();
            await expect(page.locator('[data-testid="assistant-panel"]')).toBeVisible();
            const panel = await rectOf(page, '[data-testid="assistant-panel"]');
            expect(panel.width).toBeGreaterThanOrEqual(359);
            expect(panel.width).toBeLessThanOrEqual(421);
            const main = await page.locator('main.app-content').evaluate((el) => {
                const style = getComputedStyle(el);
                return el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
            });
            expect(main, `main content box at ${width}px`).toBeGreaterThanOrEqual(640);
            const rail = await rectOf(page, '.app-nav-rail');
            expect(rail.width).toBe(width < 1440 ? 72 : 240);
        }
    });

    test('drawer at 1024px leaves the background focusable and Escape closes', async ({page}) => {
        await page.setViewportSize({width: 1024, height: 800});
        await login(page);
        await page.goto('/');
        await page.locator('[data-testid="assistant-launcher"]').click();
        await expect(page.locator('[data-testid="assistant-panel"]')).toBeVisible();
        await expect(page.locator('[data-testid="assistant-panel"]')).not.toHaveAttribute('aria-modal');
        // Non-modal: a background control still takes focus and activates.
        const firstRow = page.locator('.organization-row, .organization-card').first();
        await firstRow.focus();
        await expect(firstRow).toBeFocused();
        await page.keyboard.press('Escape');
        await expect(page.locator('[data-testid="assistant-panel"]')).toHaveCount(0);
    });

    test('sheet at 375px: full-screen dialog, Back to results restores scroll', async ({page}) => {
        await page.setViewportSize({width: 375, height: 700});
        await login(page);
        await page.goto('/');
        await page.evaluate(() => window.scrollTo(0, 300));
        await page.locator('[data-testid="assistant-launcher"]').click();
        const panel = page.locator('[data-testid="assistant-panel"]');
        await expect(panel).toBeVisible();
        await expect(panel).toHaveAttribute('role', 'dialog');
        await expect(panel).toHaveAttribute('aria-modal', 'true');
        const rect = await rectOf(page, '[data-testid="assistant-panel"]');
        expect(rect.width).toBe(375);
        await expect(page.locator('[data-testid="assistant-back-to-results"]')).toBeVisible();
        await page.locator('[data-testid="assistant-back-to-results"]').click();
        await expect(panel).toHaveCount(0);
        const scrollY = await page.evaluate(() => window.scrollY);
        expect(scrollY).toBe(300);
    });

    test('lazy load: no assistant chunk before first open, exactly one after, none on reopen', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        const chunks: string[] = [];
        page.on('request', (req) => {
            if (/\.chunk\.js($|\?)/.test(req.url())) {
                chunks.push(req.url());
            }
        });
        await login(page);
        await page.goto('/');
        await expect(page.locator('[data-testid="assistant-launcher"]')).toBeVisible();
        expect(chunks).toHaveLength(0);
        await page.locator('[data-testid="assistant-launcher"]').click();
        await expect(page.locator('[data-testid="job-search-chat"]')).toBeVisible();
        expect(chunks).toHaveLength(1);
        await page.locator('[data-testid="assistant-close"]').click();
        await page.locator('[data-testid="assistant-launcher"]').click();
        await expect(page.locator('[data-testid="job-search-chat"]')).toBeVisible();
        expect(chunks).toHaveLength(1);
    });

    test('opening the assistant creates no conversation and submits no prompt', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await login(page);
        await page.goto('/');
        // Deterministic start state: the seeded account may hold a
        // conversation from an earlier spec (the dev DB persists), so delete
        // it — the assertions below require the resume GET to 404. In-page
        // fetch shares the session cookie and can send the CSRF header.
        await page.evaluate(async () => {
            const res = await fetch('/api/agent/conversations/');
            if (res.ok) {
                const body = await res.json() as {id: number};
                const csrf = /csrftoken=([^;]+)/.exec(document.cookie)?.[1] ?? '';
                await fetch(`/api/agent/conversations/${body.id}/delete/`, {
                    method: 'POST',
                    headers: {'X-CSRFToken': csrf},
                });
            }
        });
        await page.goto('/');
        const conversationPosts: string[] = [];
        const turnRequests: string[] = [];
        page.on('request', (req) => {
            const url = req.url();
            if (req.method() === 'POST' && /\/api\/agent\/conversations\/$/.test(url)) {
                conversationPosts.push(url);
            }
            if (/\/api\/agent\/conversations\/\d+\//.test(url)) {
                turnRequests.push(url);
            }
        });
        await page.locator('[data-testid="assistant-launcher"]').click();
        await expect(page.locator('[data-testid="job-search-chat"]')).toBeVisible();
        expect(conversationPosts).toHaveLength(0);
        expect(turnRequests).toHaveLength(0);
        // The first actual send creates the conversation exactly once.
        await page.locator('[aria-label="Message"]').fill('hello from e2e');
        await page.locator('[aria-label="Send message"]').click();
        await expect.poll(() => conversationPosts.length).toBe(1);
    });

    test('/chat/ renders exactly one job-search-chat section', async ({page}) => {
        await login(page);
        await page.goto('/chat/');
        await expect(page.locator('[data-testid="job-search-chat"]')).toHaveCount(1);
        await expect(page.locator('#job-search-chat')).toHaveCount(1);
    });

    test('long unbroken content does not overflow at 320px', async ({page}) => {
        await page.setViewportSize({width: 320, height: 700});
        await login(page);
        await page.goto('/');
        await page.evaluate(() => {
            const el = document.createElement('p');
            el.textContent = 'x'.repeat(5000);
            document.querySelector('main.app-content')?.appendChild(el);
        });
        await expectNoHorizontalOverflow(page);
        await page.locator('[data-testid="assistant-launcher"]').click();
        await expect(page.locator('[data-testid="assistant-panel"]')).toBeVisible();
        await expectNoHorizontalOverflow(page);
    });

    test('one blocking surface at a time: a details dialog over the sheet closes it', async ({page}) => {
        await page.setViewportSize({width: 375, height: 700});
        await login(page);
        await page.goto('/');
        await page.locator('[data-testid="assistant-launcher"]').click();
        await expect(page.locator('[data-testid="assistant-panel"]')).toBeVisible();
        // Trigger the company details dialog programmatically: the sheet
        // inerts the rankings rows, so a real pointer click cannot reach
        // them — the dialog opens over the sheet (e.g. from a recovered
        // state) and the sheet must yield.
        await page.evaluate(() => {
            document.querySelector<HTMLElement>('tr[aria-label="View details for E2E Alpha Corp"]')?.click();
        });
        await expect(page.getByRole('dialog').first()).toBeVisible();
        await expect(page.locator('[data-testid="assistant-panel"]')).toHaveCount(0);
        const inertCount = await page.evaluate(
            () => document.querySelectorAll('.app-shell[inert], main.app-content[inert]').length,
        );
        expect(inertCount).toBeGreaterThan(0);
    });

    test('admin and login pages render no workspace anchor', async ({page}) => {
        await page.goto('/accounts/login/');
        await expect(page.locator('#assistant-workspace')).toHaveCount(0);
        await login(page);
        await page.goto('/admin/');
        await expect(page.locator('#assistant-workspace')).toHaveCount(0);
    });
});
