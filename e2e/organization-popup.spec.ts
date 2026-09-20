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

// Review r3: Bootstrap sets `:root { scroll-behavior: smooth }`, so
// window.scrollTo animates and an immediate exact read of window.scrollY
// catches a mid-animation offset — the tall-document sanity assertions
// failed in Firefox and WebKit (CI observed 0, 2, and 32 instead of 300
// across retries) before the dialog was even opened. Disable smooth
// scrolling on the test page — a page.evaluate scrollTo with instant
// behavior — and wait for the scroll to settle before reading it, so the
// setup is deterministic in chromium, firefox, and webkit.
async function scrollWindowTo(page: Page, top: number, message?: string): Promise<void> {
    await page.evaluate(() => {
        document.documentElement.style.scrollBehavior = 'auto';
    });
    await page.evaluate((y) => window.scrollTo(0, y), top);
    await expect.poll(
        () => page.evaluate(() => window.scrollY),
        {message: message ?? `window scroll must settle at ${top}`},
    ).toBe(top);
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

        // The pinned-header contract (review r2): the header is a flex SIBLING
        // of the .card-body scroll container, so sibling geometry alone cannot
        // detect a `position: sticky` regression — removing sticky leaves the
        // header in place while the body scrolls beneath it. Pin the contract
        // itself, then prove the header is a real on-screen hit target.
        const headerPosition = await header.evaluate((el) => getComputedStyle(el).position);
        expect(headerPosition, 'the pinned header must keep position: sticky').toBe('sticky');

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
        expect(headerBoxAfter!.x).toBe(headerBoxBefore!.x);
        const dialogBox = await dialog.boundingBox();
        expect(headerBoxAfter!.y).toBeGreaterThanOrEqual(dialogBox!.y - 1);
        expect(headerBoxAfter!.y + headerBoxAfter!.height)
            .toBeLessThanOrEqual(dialogBox!.y + dialogBox!.height + 1);

        // Viewport-relative bounds and hit target (review r2): the pinned
        // header must be a real, visible on-screen target after the scroll —
        // Playwright `toBeVisible()` does not require viewport intersection,
        // and dialog-relative bounds alone would pass an off-screen header.
        const viewport = page.viewportSize()!;
        expect(headerBoxAfter!.x).toBeGreaterThanOrEqual(0);
        expect(headerBoxAfter!.y).toBeGreaterThanOrEqual(0);
        expect(headerBoxAfter!.x + headerBoxAfter!.width).toBeLessThanOrEqual(viewport.width);
        expect(headerBoxAfter!.y + headerBoxAfter!.height).toBeLessThanOrEqual(viewport.height);
        const headerOnTop = await page.evaluate((box) => {
            const el = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
            return Boolean(el && el.closest('.card-header'));
        }, headerBoxAfter!);
        expect(headerOnTop, 'the pinned header must remain the topmost hit target at its center').toBe(true);
        await expect(header).toBeVisible();
    });

    test('background content is unreachable and document scrolling is locked while the dialog is open', async ({page}) => {
        await page.goto(POPUP_FIXTURE);

        // Review r2: the checked-in fixture is exactly viewport-high, so the
        // old wheel probe passed vacuously. Make the document provably tall
        // first and prove it actually scrolls before the dialog opens.
        await page.evaluate(() => {
            const filler = document.createElement('div');
            filler.setAttribute('data-testid', 'document-filler');
            filler.setAttribute('aria-hidden', 'true');
            filler.style.cssText = 'height: 2400px;';
            document.querySelector('main.app-content')!.appendChild(filler);
        });
        await scrollWindowTo(page, 300, 'sanity: the tall document scrolls before the dialog opens');
        await scrollWindowTo(page, 0);

        const overflowBefore = await page.evaluate(() => ({
            body: getComputedStyle(document.body).overflow,
            root: getComputedStyle(document.documentElement).overflow,
        }));
        await openDetailsDialog(page);

        const dialog = page.getByRole('dialog');

        // Background isolation (issue #464 "prevent background interaction"):
        // everything outside the dialog — app shell, page content and the
        // modal-external skip link (review r2) — is inert and hidden from
        // the accessibility tree while it is open.
        const backgroundState = await page.evaluate(() => {
            const roots = Array.from(document.querySelectorAll<HTMLElement>('.app-shell, main.app-content, .skip-to-content'));
            return roots.map((el) => ({
                cls: el.className,
                inert: el.hasAttribute('inert'),
                ariaHidden: el.getAttribute('aria-hidden'),
            }));
        });
        expect(backgroundState.length).toBeGreaterThanOrEqual(3);
        for (const state of backgroundState) {
            expect(state.inert, `${state.cls} must be inert while the dialog is open`).toBe(true);
            expect(state.ariaHidden, `${state.cls} must be aria-hidden while the dialog is open`).toBe('true');
        }

        // Document scroll lock (review r2): under the stylesheet's
        // `html, body { overflow-x: hidden }` rule the ROOT element carries
        // the viewport overflow, so locking body alone left the actual
        // viewport scroller free. Both the root element and body must be
        // locked while the dialog is open.
        const overflowWhileOpen = await page.evaluate(() => ({
            body: getComputedStyle(document.body).overflow,
            root: getComputedStyle(document.documentElement).overflow,
        }));
        expect(overflowWhileOpen.body).toBe('hidden');
        expect(overflowWhileOpen.root).toBe('hidden');

        // Wheel input over the backdrop cannot move the (provably tall)
        // document behind the blocking dialog.
        await page.mouse.wheel(0, 600);
        await page.waitForTimeout(100);
        const scrollYWhileOpen = await page.evaluate(() => window.scrollY);
        expect(scrollYWhileOpen, 'wheel scrolling over the backdrop must not move the document').toBe(0);

        // Programmatic focus on background elements is a no-op while inert —
        // including the modal-external skip link, which used to remain a
        // reachable page-level focus target (review r2).
        const focusProbes = await page.evaluate(() => {
            const dialogEl = document.querySelector('[role="dialog"]');
            const results: Record<string, boolean> = {};
            for (const id of ['nav-rankings']) {
                document.getElementById(id)?.focus();
                results[id] = Boolean(dialogEl && dialogEl.contains(document.activeElement));
            }
            (document.querySelector('.skip-to-content') as HTMLElement | null)?.focus();
            results['skip-to-content'] = Boolean(dialogEl && dialogEl.contains(document.activeElement));
            return results;
        });
        expect(focusProbes['nav-rankings'], 'nav focus probe must stay inside the dialog').toBe(true);
        expect(focusProbes['skip-to-content'], 'skip-link focus probe must stay inside the dialog').toBe(true);

        // Keyboard walk: Tab never leaves the dialog for the page behind it —
        // not for the nav rail and not for the skip link.
        for (let i = 0; i < 8; i++) {
            await page.keyboard.press('Tab');
            const inside = await dialog.evaluate((el) => el.contains(document.activeElement));
            expect(inside, `Tab #${i + 1} must stay inside the dialog`).toBe(true);
        }

        // Closing the dialog releases the isolation and the document
        // scrolls again.
        await page.keyboard.press('Escape');
        await expect(dialog).toHaveCount(0);
        const overflowAfter = await page.evaluate(() => ({
            body: getComputedStyle(document.body).overflow,
            root: getComputedStyle(document.documentElement).overflow,
        }));
        expect(overflowAfter).toEqual(overflowBefore);
        const stillInert = await page.evaluate(() =>
            Array.from(document.querySelectorAll('.app-shell, main.app-content, .skip-to-content'))
                .some((el) => el.hasAttribute('inert')));
        expect(stillInert).toBe(false);
        await scrollWindowTo(page, 150, 'sanity: the document scrolls again after the close');
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

        // The suggest modal is the topmost (and only) blocking dialog: the
        // background stays fully isolated while it is open — including the
        // modal-external skip link and the actual document scroller (r2).
        const suggestIsolation = await page.evaluate(() => ({
            anyNotInert: Array.from(document.querySelectorAll<HTMLElement>('.app-shell, main.app-content, .skip-to-content'))
                .some((el) => !el.hasAttribute('inert')),
            rootOverflow: getComputedStyle(document.documentElement).overflow,
        }));
        expect(suggestIsolation.anyNotInert).toBe(false);
        expect(suggestIsolation.rootOverflow).toBe('hidden');

        // Escape again, then reopen the details dialog: the suggest modal
        // stays closed — at most one blocking dialog is ever active.
        await page.keyboard.press('Escape');
        await expect(page.getByTestId('suggest-company-modal')).toHaveCount(0);
        await page.locator('tr.organization-row').first().click();
        await expect(page.getByRole('dialog', {name: /Acme Robotics/})).toBeVisible();
        await expect(page.getByTestId('suggest-company-modal')).toHaveCount(0);
    });

    test('suggest modal Escape returns keyboard focus to the toolbar button that opened it', async ({page}) => {
        // Real-browser assertion of the #464 focus-restore contract on the
        // suggest modal: the opener must be captured BEFORE lockBackground()
        // inerts the shell, because inerting blurs the trigger and resets
        // activeElement to <body>.
        await page.goto(POPUP_FIXTURE);
        await page.getByTestId('suggest-company-btn').click();
        await expect(page.getByTestId('suggest-company-modal')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.getByTestId('suggest-company-modal')).toHaveCount(0);
        await expect(page.getByTestId('suggest-company-btn')).toBeFocused();
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

    test('background is inert and document scrolling is locked at mobile width', async ({page, browserName}) => {
        await page.goto(POPUP_FIXTURE);

        // Review r2: the mobile test previously checked only the computed
        // overflow string and never wheeled. Make the document provably tall,
        // prove it scrolls, and wheel over the backdrop at this width too.
        await page.evaluate(() => {
            const filler = document.createElement('div');
            filler.setAttribute('data-testid', 'document-filler');
            filler.setAttribute('aria-hidden', 'true');
            filler.style.cssText = 'height: 2400px;';
            document.querySelector('main.app-content')!.appendChild(filler);
        });
        await scrollWindowTo(page, 300, 'sanity: the tall document scrolls before the dialog opens');
        await scrollWindowTo(page, 0);

        const overflowBefore = await page.evaluate(() => ({
            body: getComputedStyle(document.body).overflow,
            root: getComputedStyle(document.documentElement).overflow,
        }));
        await openDetailsDialog(page);

        // Same isolation contract as desktop, asserted at the narrow width:
        // every non-dialog focus target — shell, content, skip link — is
        // inert, and the actual document scroller (root element) is locked.
        const state = await page.evaluate(() => {
            const roots = Array.from(document.querySelectorAll<HTMLElement>('.app-shell, main.app-content, .skip-to-content'));
            return {
                count: roots.length,
                inert: roots.map((el) => el.hasAttribute('inert')),
                ariaHidden: roots.map((el) => el.getAttribute('aria-hidden')),
                overflow: {
                    body: getComputedStyle(document.body).overflow,
                    root: getComputedStyle(document.documentElement).overflow,
                },
            };
        });
        expect(state.count).toBeGreaterThanOrEqual(3);
        expect(state.inert).not.toContain(false);
        expect(state.ariaHidden).not.toContain(null);
        expect(state.ariaHidden).not.toContain('false');
        expect(state.overflow.body).toBe('hidden');
        expect(state.overflow.root).toBe('hidden');

        // Review r3 follow-up: Playwright cannot dispatch a synthetic mouse
        // wheel in mobile WebKit, so the wheel probe is Chromium/Firefox-only
        // at this width — desktop WebKit still exercises the same
        // wheel-over-backdrop scroll-chaining contract in the desktop test
        // above. The lock itself stays asserted in every engine via the
        // computed root/body overflow and the inert state above.
        if (browserName !== 'webkit') {
            await page.mouse.wheel(0, 600);
            await page.waitForTimeout(100);
            expect(await page.evaluate(() => window.scrollY), 'wheel over the backdrop must not move the document at mobile width').toBe(0);
        }

        await page.keyboard.press('Escape');
        await expect(page.getByRole('dialog')).toHaveCount(0);
        const released = await page.evaluate(() => ({
            inert: Array.from(document.querySelectorAll('.app-shell, main.app-content, .skip-to-content'))
                .some((el) => el.hasAttribute('inert')),
            overflow: {
                body: getComputedStyle(document.body).overflow,
                root: getComputedStyle(document.documentElement).overflow,
            },
        }));
        expect(released.inert).toBe(false);
        expect(released.overflow).toEqual(overflowBefore);
        await scrollWindowTo(page, 150, 'sanity: the document scrolls again after the close');
    });
});
