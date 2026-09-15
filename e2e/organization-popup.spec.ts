// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// E2E tests for the company details dialog layering and keyboard focus above
// the fixed navigation rail (issue #464). Asserts visible bounds and hit
// targets: the dialog title and Close button must not intersect the nav rail
// at desktop width, and the dialog must be reachable and closable by keyboard
// at both desktop and mobile widths.
import {test, expect, Page} from '@playwright/test';

const POPUP_FIXTURE = '/e2e/fixtures/organization-popup.html';

async function openDetailsDialog(page: Page): Promise<void> {
    // The rankings table is hidden at narrow widths; the mobile layout shows
    // cards instead. Pick whichever opener the current viewport renders.
    const row = page.locator('tr.organization-row').first();
    const card = page.locator('.organization-card').first();
    const opener = (await row.isVisible()) ? row : card;
    await expect(opener).toBeVisible();
    // Enter/Space on a company row opens the dialog (issue #464 validation).
    await opener.focus();
    await opener.press('Enter');
    await expect(page.getByRole('dialog')).toBeVisible();
}

test.describe('company details dialog layering (issue #464) — desktop', () => {
    test.use({viewport: {width: 1280, height: 800}});

    test('dialog title and Close button are hit targets above the nav rail', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openDetailsDialog(page);

        const dialog = page.getByRole('dialog');
        const rail = page.locator('[data-nav-rail]');
        await expect(rail).toBeVisible();

        // A centered dialog necessarily overlaps the fixed rail's bounding
        // box; the issue's requirement is that the dialog renders ABOVE it and
        // its title and Close control remain real hit targets (visible
        // bounds). Assert the topmost element at each control's center belongs
        // to the dialog overlay (never the rail) and both controls sit inside
        // the viewport.
        const viewport = page.viewportSize()!;
        const targets = [
            {name: 'dialog title', locator: page.locator('#organization-details-title')},
            {name: 'Close button', locator: dialog.getByRole('button', {name: 'Close'})},
        ] as const;
        for (const {name, locator} of targets) {
            const box = await locator.boundingBox();
            expect(box, `${name} must be visible`).not.toBeNull();
            expect(box!.x + box!.width / 2, `${name} center x inside viewport`).toBeGreaterThanOrEqual(0);
            expect(box!.x + box!.width / 2, `${name} center x inside viewport`).toBeLessThanOrEqual(viewport.width);
            expect(box!.y + box!.height / 2, `${name} center y inside viewport`).toBeGreaterThanOrEqual(0);
            expect(box!.y + box!.height / 2, `${name} center y inside viewport`).toBeLessThanOrEqual(viewport.height);

            const onTop = await page.evaluate(([x, y]) => {
                const el = document.elementFromPoint(x, y);
                return Boolean(el && el.closest('.popup-overlay'));
            }, [box!.x + box!.width / 2, box!.y + box!.height / 2]);
            expect(onTop,
                `${name} must render above the nav rail (hit target resolves inside the dialog overlay)`)
                .toBe(true);
        }
    });

    test('blocking dialog renders above the navigation rail', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openDetailsDialog(page);

        const overlayZ = await page.locator('.popup-overlay').evaluate(
            (el) => parseInt(getComputedStyle(el).zIndex, 10)
        );
        const railZ = await page.locator('[data-nav-rail]').evaluate(
            (el) => parseInt(getComputedStyle(el).zIndex, 10)
        );

        expect(overlayZ, 'popup overlay must stack above the nav rail').toBeGreaterThan(railZ);
    });

    test('large content scrolls inside the card body while the header stays pinned', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openDetailsDialog(page);

        const dialog = page.getByRole('dialog');
        const body = dialog.locator('.card-body');
        const header = dialog.locator('.card-header');

        // The checked-in fixture's live content may not overflow, which made
        // the original test vacuous (review r1): force deterministic overflow
        // so the substantive assertions always run.
        await body.evaluate((el) => {
            const filler = document.createElement('div');
            filler.setAttribute('data-testid', 'scroll-filler');
            filler.setAttribute('aria-hidden', 'true');
            filler.style.cssText = 'height: 2400px;';
            el.appendChild(filler);
        });
        const scrollInfo = await body.evaluate((el) => ({
            overflowY: getComputedStyle(el).overflowY,
            scrollHeight: el.scrollHeight,
            clientHeight: el.clientHeight,
        }));
        expect(scrollInfo.overflowY).toBe('auto');
        expect(scrollInfo.scrollHeight, 'card body must actually overflow').toBeGreaterThan(scrollInfo.clientHeight);

        // Record the pinned header's position, then scroll the body to its
        // maximum and prove the scroll actually advanced.
        const headerBoxBefore = await header.boundingBox();
        await body.evaluate((el) => {
            el.scrollTop = el.scrollHeight;
        });
        const scrollTop = await body.evaluate((el) => el.scrollTop);
        expect(scrollTop, 'card body scrolls to its maximum').toBeGreaterThan(0);

        // The header keeps its exact position while the body scrolls beneath it.
        const headerBoxAfter = await header.boundingBox();
        expect(headerBoxAfter!.y).toBe(headerBoxBefore!.y);
        const dialogBox = await dialog.boundingBox();
        expect(headerBoxAfter!.y).toBeGreaterThanOrEqual(dialogBox!.y - 1);
        expect(headerBoxAfter!.y + headerBoxAfter!.height)
            .toBeLessThanOrEqual(dialogBox!.y + dialogBox!.height + 1);
        await expect(header).toBeVisible();
    });

    test('background content is unreachable and document scrolling is locked while the dialog is open', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        const overflowBefore = await page.evaluate(() => getComputedStyle(document.body).overflow);
        await openDetailsDialog(page);

        const dialog = page.getByRole('dialog');

        // Background isolation (issue #464 "prevent background interaction"):
        // the app shell and page content behind the dialog are inert and
        // hidden from the accessibility tree while it is open.
        const backgroundState = await page.evaluate(() => {
            const roots = Array.from(document.querySelectorAll<HTMLElement>('.app-shell, main.app-content'));
            return roots.map((el) => ({
                inert: el.hasAttribute('inert'),
                ariaHidden: el.getAttribute('aria-hidden'),
            }));
        });
        expect(backgroundState.length).toBeGreaterThan(0);
        for (const state of backgroundState) {
            expect(state.inert).toBe(true);
            expect(state.ariaHidden).toBe('true');
        }

        // Document scroll lock: the body cannot scroll while the dialog is open.
        const overflowWhileOpen = await page.evaluate(() => getComputedStyle(document.body).overflow);
        expect(overflowWhileOpen).toBe('hidden');
        await page.mouse.wheel(0, 600);
        await page.waitForTimeout(100);
        const scrollYWhileOpen = await page.evaluate(() => window.scrollY);
        expect(scrollYWhileOpen, 'wheel scrolling over the backdrop must not move the document').toBe(0);

        // Programmatic focus on background elements is a no-op while inert.
        const focusStayedInDialog = await page.evaluate(() => {
            const navLink = document.getElementById('nav-rankings');
            navLink?.focus();
            const dialogEl = document.querySelector('[role="dialog"]');
            return Boolean(dialogEl && dialogEl.contains(document.activeElement));
        });
        expect(focusStayedInDialog, 'focus must stay inside the dialog while the background is inert').toBe(true);

        // Keyboard walk: Tab never leaves the dialog for the page behind it.
        for (let i = 0; i < 8; i++) {
            await page.keyboard.press('Tab');
            const inside = await dialog.evaluate((el) => el.contains(document.activeElement));
            expect(inside, `Tab #${i + 1} must stay inside the dialog`).toBe(true);
        }

        // Closing the dialog releases the isolation.
        await page.keyboard.press('Escape');
        await expect(dialog).toHaveCount(0);
        const overflowAfter = await page.evaluate(() => getComputedStyle(document.body).overflow);
        expect(overflowAfter).toBe(overflowBefore);
        const stillInert = await page.evaluate(() =>
            Array.from(document.querySelectorAll('.app-shell, main.app-content'))
                .some((el) => el.hasAttribute('inert')));
        expect(stillInert).toBe(false);
    });

    test('Tab cycles inside the dialog and Escape restores focus to the opener row', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openDetailsDialog(page);

        const dialog = page.getByRole('dialog');
        await expect(dialog.getByRole('button', {name: 'Close'})).toBeFocused();

        // Tab cycles among the dialog's own focusable elements and never
        // lands on the navigation rail behind the dialog.
        for (let i = 0; i < 6; i++) {
            await page.keyboard.press('Tab');
            const focusedInDialog = await dialog.evaluate((el) => el.contains(document.activeElement));
            expect(focusedInDialog, `Tab #${i + 1} must stay inside the dialog`).toBe(true);
        }

        await page.keyboard.press('Escape');
        await expect(page.getByRole('dialog')).toHaveCount(0);
        await expect(page.locator('tr.organization-row').first()).toBeFocused();
    });

    test('overlay click closes and returns focus to the opener row', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openDetailsDialog(page);

        // Click the overlay padding outside the dialog card.
        await page.locator('.popup-overlay').click({position: {x: 10, y: 10}});
        await expect(page.getByRole('dialog')).toHaveCount(0);
        await expect(page.locator('tr.organization-row').first()).toBeFocused();
    });

    test('only one blocking dialog is active at a time', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openDetailsDialog(page);

        // While the details dialog blocks the page, the suggest modal is not
        // open (mutual exclusion enforced by OrganizationList state).
        await expect(page.getByTestId('suggest-company-modal')).toHaveCount(0);

        // Escape, then open the suggest modal: the details dialog stays closed.
        await page.keyboard.press('Escape');
        await expect(page.getByRole('dialog')).toHaveCount(0);
        await page.getByTestId('suggest-company-btn').click();
        await expect(page.getByTestId('suggest-company-modal')).toBeVisible();
        await expect(page.getByRole('dialog', {name: /Acme Robotics/})).toHaveCount(0);

        // Escape again, then reopen the details dialog: the suggest modal
        // stays closed — at most one blocking dialog is ever active.
        await page.keyboard.press('Escape');
        await expect(page.getByTestId('suggest-company-modal')).toHaveCount(0);
        await page.locator('tr.organization-row').first().click();
        await expect(page.getByRole('dialog', {name: /Acme Robotics/})).toBeVisible();
        await expect(page.getByTestId('suggest-company-modal')).toHaveCount(0);
    });
});

test.describe('company details dialog layering (issue #464) — mobile', () => {
    test.use({viewport: {width: 375, height: 667}, isMobile: true, hasTouch: true});

    test('dialog title and Close button are fully visible at narrow width', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        await openDetailsDialog(page);

        const viewport = page.viewportSize()!;
        const dialog = page.getByRole('dialog');
        const titleBox = await page.locator('#organization-details-title').boundingBox();
        const closeButtonBox = await dialog.getByRole('button', {name: 'Close'}).boundingBox();

        for (const [name, box] of [['title', titleBox], ['Close button', closeButtonBox]] as const) {
            expect(box, `${name} must be visible`).not.toBeNull();
            expect(box!.x, `${name} must start inside the viewport`).toBeGreaterThanOrEqual(0);
            expect(box!.x + box!.width, `${name} must end inside the viewport`).toBeLessThanOrEqual(viewport.width);
            expect(box!.y, `${name} must start inside the viewport`).toBeGreaterThanOrEqual(0);
            expect(box!.y + box!.height, `${name} must end inside the viewport`).toBeLessThanOrEqual(viewport.height);
        }
    });

    test('dialog opens via keyboard and Escape restores focus to the opener card', async ({page}) => {
        await page.goto(POPUP_FIXTURE);

        // Mobile view renders cards instead of the table.
        const card = page.locator('.organization-card').first();
        await expect(card).toBeVisible();
        await card.focus();
        await card.press('Enter');
        await expect(page.getByRole('dialog')).toBeVisible();

        await page.keyboard.press('Escape');
        await expect(page.getByRole('dialog')).toHaveCount(0);
        await expect(page.locator('.organization-card').first()).toBeFocused();
    });

    test('background is inert and document scrolling is locked at mobile width', async ({page}) => {
        await page.goto(POPUP_FIXTURE);
        const overflowBefore = await page.evaluate(() => getComputedStyle(document.body).overflow);
        await openDetailsDialog(page);

        // Same isolation contract as desktop, asserted at the narrow width.
        const state = await page.evaluate(() => {
            const roots = Array.from(document.querySelectorAll<HTMLElement>('.app-shell, main.app-content'));
            return {
                inert: roots.map((el) => el.hasAttribute('inert')),
                overflow: getComputedStyle(document.body).overflow,
            };
        });
        expect(state.inert.length).toBeGreaterThan(0);
        expect(state.inert).not.toContain(false);
        expect(state.overflow).toBe('hidden');

        await page.keyboard.press('Escape');
        await expect(page.getByRole('dialog')).toHaveCount(0);
        const released = await page.evaluate(() => ({
            inert: Array.from(document.querySelectorAll('.app-shell, main.app-content'))
                .some((el) => el.hasAttribute('inert')),
            overflow: getComputedStyle(document.body).overflow,
        }));
        expect(released.inert).toBe(false);
        expect(released.overflow).toBe(overflowBefore);
    });
});
