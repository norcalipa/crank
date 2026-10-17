// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// The conversation controls in a real browser (issue #483, AC 14): the More menu with the keyboard, New conversation,
// Export and Delete with their inline confirmations, copy, success, failure and retry, where focus goes after each
// step, saved priorities left alone, items disabled while a turn is pending, and 44px targets on a phone.
import {expect, Locator, Page, test} from '@playwright/test';
import {E2E_PASSWORD, E2E_USERNAME, login, requireDjangoTier, waitForConfirmArmed} from './support';

const SIZES = [
    {name: 'docked', width: 1280, height: 900, sheet: false},
    {name: 'sheet', width: 375, height: 667, sheet: true},
];
const NEW_COPY = 'Your current history will be archived. Your saved priorities are not changed.';
const DELETE_COPY = 'Its messages are removed and can’t be recovered. Your saved priorities and job matches are not changed.';
const TURN_URL = /\/api\/agent\/conversations\/\d+\/$/;

async function openAssistant(page: Page): Promise<void> {
    await page.goto('/');
    const panel = page.getByTestId('assistant-panel');
    const opener = page.locator('[data-testid="assistant-launcher"], [data-testid="assistant-restore"]');
    await expect(panel.or(opener).first()).toBeVisible();
    if (!(await panel.isVisible())) await opener.click({timeout: 3000}).catch(async () => { await expect(panel).toBeVisible(); });
    await expect(page.getByTestId('assistant-composer')).toBeVisible();
    await expect(page.getByTestId('chat-loading')).toHaveCount(0);
    await page.waitForLoadState('networkidle');
}

/** The test's own turns: the seeded demo provider answers each. */
async function ensureTurns(page: Page, turns: number): Promise<void> {
    const replies = page.locator('article[aria-label="Assistant message"]');
    for (let have = await replies.count(); have < turns; have += 1) {
        await page.getByTestId('assistant-composer').fill(`Controls spec question ${have + 1}: how are scores worked out?`);
        await page.getByRole('button', {name: 'Send message'}).click();
        await expect(replies.nth(have)).toBeVisible({timeout: 30_000});
    }
}

/** The saved priorities as the server answers them: whether the row exists, its revision and its document.
 * The seeded account's row is at revision 0, so a revision alone cannot tell a deleted row (also revision 0) from a kept one. */
async function savedPriorities(page: Page): Promise<{exists: boolean; revision: number; preferences: unknown}> {
    return page.evaluate(async () => {
        const {exists, revision, preferences} = await (await fetch('/api/agent/preferences/')).json();
        return {exists, revision, preferences};
    });
}

const more = (page: Page) => page.getByTestId('conversation-more');
const menu = (page: Page) => page.getByTestId('conversation-menu');

async function chooseItem(page: Page, testId: string): Promise<void> {
    await more(page).click();
    await expect(menu(page)).toBeVisible();
    await page.getByTestId(testId).click();
}

for (const size of SIZES) {
    test.describe(`conversation controls, ${size.name} ${size.width}x${size.height}`, () => {
        test.skip(process.env.CRANK_E2E_PROVIDER_FAILURE === '1', 'needs assistant replies: not part of the provider-outage pass');

        let startPriorities: Awaited<ReturnType<typeof savedPriorities>>;
        test.beforeEach(async ({page}) => {
            requireDjangoTier();
            await page.setViewportSize({width: size.width, height: size.height});
            await login(page, E2E_USERNAME, E2E_PASSWORD);
            await openAssistant(page);
            await ensureTurns(page, 2);
            startPriorities = await savedPriorities(page);
            expect(startPriorities.exists, 'the seeded account has a saved row to compare against').toBe(true);
        });

        test.afterEach(async ({page}, testInfo) => {
            if (testInfo.status === 'skipped') return;
            await page.unrouteAll({behavior: 'ignoreErrors'});
            await page.goto('/');
            expect(await savedPriorities(page), 'saved priorities are unchanged').toEqual(startPriorities);
        });

        /** Where focus belongs after New or Delete: the composer when docked, the empty state's call to action in the sheet. */
        const expectFocusAfterHistoryAction = async (page: Page) => {
            await expect(page.getByTestId('empty-history')).toBeVisible();
            if (size.sheet) await expect(page.getByTestId('empty-history-cta')).toBeFocused();
            else await expect(page.getByTestId('assistant-composer')).toBeFocused();
        };

        test('More opens and closes with the keyboard and moves between its items', async ({page}) => {
            await more(page).focus();
            await expect(more(page)).toHaveAttribute('aria-expanded', 'false');
            await page.keyboard.press('ArrowDown');
            await expect(menu(page)).toBeVisible();
            await expect(more(page)).toHaveAttribute('aria-expanded', 'true');
            const order = ['conversation-new', 'conversation-export', 'conversation-about-history', 'conversation-delete'];
            await expect(page.getByTestId(order[0])).toBeFocused();
            for (const next of order.slice(1)) {
                await page.keyboard.press('ArrowDown');
                await expect(page.getByTestId(next)).toBeFocused();
            }
            await page.keyboard.press('ArrowDown');
            await expect(page.getByTestId(order[0]), 'ArrowDown wraps').toBeFocused();
            await page.keyboard.press('ArrowUp');
            await expect(page.getByTestId(order[3]), 'ArrowUp wraps').toBeFocused();
            await page.keyboard.press('Home');
            await expect(page.getByTestId(order[0])).toBeFocused();
            await page.keyboard.press('End');
            await expect(page.getByTestId(order[3])).toBeFocused();
            // In the drawer and the sheet the workspace shell also closes the whole panel on Escape, so the
            // key is pressed where it only closes the menu.
            if (!size.sheet) {
                await page.keyboard.press('Escape');
                await expect(menu(page)).toHaveCount(0);
                await expect(more(page), 'Escape returns focus to More').toBeFocused();
            } else {
                await page.keyboard.press('Tab');
                await expect(menu(page)).toHaveCount(0);
                await more(page).focus();
            }
            // ArrowUp from the button opens the menu on its last item; Tab leaves and closes it.
            await page.keyboard.press('ArrowUp');
            await expect(page.getByTestId(order[3])).toBeFocused();
            await page.keyboard.press('Tab');
            await expect(menu(page)).toHaveCount(0);
            // A second press on More closes the menu it opened.
            await more(page).click();
            await expect(menu(page)).toBeVisible();
            await more(page).click();
            await expect(menu(page)).toHaveCount(0);
            // About saved history: its heading takes focus, Close gives it back to More.
            await chooseItem(page, 'conversation-about-history');
            const about = page.getByTestId('about-history-panel');
            await expect(about.getByRole('heading', {name: 'About saved history'})).toBeFocused();
            await page.getByTestId('about-history-close').click();
            await expect(about).toHaveCount(0);
            await expect(more(page)).toBeFocused();
        });

        test('New conversation: confirmation copy and focus, Cancel, a failure with its retry, then success', async ({page}) => {
            await chooseItem(page, 'conversation-new');
            const heading = page.getByRole('heading', {name: 'Start a new conversation?'});
            await expect(heading).toBeFocused();
            await expect(page.getByTestId('confirm-panel')).toContainText(NEW_COPY);
            await page.getByTestId('confirm-cancel').click();
            await expect(page.getByTestId('confirm-panel')).toHaveCount(0);
            await expect(more(page), 'Cancel returns focus to More').toBeFocused();

            await page.route(/\/api\/agent\/conversations\/\d+\/reset\/$/, (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
            await chooseItem(page, 'conversation-new');
            await expect(heading).toBeFocused();
            await waitForConfirmArmed(page);
            await page.getByTestId('confirm-action').click();
            await expect(page.getByRole('alert').filter({hasText: 'Could not reset the conversation.'})).toBeVisible();
            const retry = page.getByTestId('confirm-action');
            await expect(retry).toHaveText('Try again');
            await expect(retry, 'the retry takes focus after a failure').toBeFocused();
            await expect(page.locator('article[aria-label="Assistant message"]').first()).toBeVisible();

            await page.unroute(/\/api\/agent\/conversations\/\d+\/reset\/$/);
            await retry.click();
            await expect(page.getByTestId('confirm-panel')).toHaveCount(0);
            await expectFocusAfterHistoryAction(page);
        });

        test('Delete conversation: confirmation copy and focus, a failure with its retry, a keyboard confirm after the delay, then success', async ({page}) => {
            await chooseItem(page, 'conversation-delete');
            await expect(page.getByRole('heading', {name: 'Delete conversation?'})).toBeFocused();
            await expect(page.getByTestId('confirm-panel')).toContainText(DELETE_COPY);
            await expect(page.getByTestId('confirm-export-first')).toBeEnabled();
            await page.getByTestId('confirm-cancel').click();
            await expect(more(page)).toBeFocused();

            await page.route(/\/api\/agent\/conversations\/\d+\/delete\/$/, (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
            await chooseItem(page, 'conversation-delete');
            await waitForConfirmArmed(page);
            // A pointer press after the delay is honoured: here it reaches the failing server.
            await page.getByTestId('confirm-action').click();
            await expect(page.getByRole('alert').filter({hasText: 'Could not delete the conversation.'})).toBeVisible();
            const retry = page.getByTestId('confirm-action');
            await expect(retry).toHaveText('Try deleting again');
            await expect(retry).toBeFocused();

            await page.unroute(/\/api\/agent\/conversations\/\d+\/delete\/$/);
            // A keyboard activation is honoured too (it is not a pointer press).
            await page.keyboard.press('Enter');
            await expect(page.getByTestId('confirm-panel')).toHaveCount(0);
            await expectFocusAfterHistoryAction(page);
        });

        test('Export downloads the conversation and returns focus to More; a failure offers a retry', async ({page}) => {
            const download = page.waitForEvent('download');
            await chooseItem(page, 'conversation-export');
            expect((await download).suggestedFilename()).toMatch(/^job-search-\d+\.json$/);
            await expect(page.getByTestId('conversation-status')).toHaveText('Conversation exported');
            await expect(more(page), 'focus returns to More after Export').toBeFocused();

            await page.route(/\/api\/agent\/conversations\/\d+\/export\/$/, (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
            await chooseItem(page, 'conversation-export');
            const panel = page.getByTestId('export-error');
            await expect(panel).toContainText('Could not export your conversation.');
            const retry = panel.getByRole('button', {name: 'Try export again'});
            await expect(retry, 'the retry takes focus after a failure').toBeFocused();
            await page.unroute(/\/api\/agent\/conversations\/\d+\/export\/$/);
            const retried = page.waitForEvent('download');
            await retry.click();
            await retried;
            await expect(panel).toHaveCount(0);
            await expect(more(page)).toBeFocused();

            await page.route(/\/api\/agent\/conversations\/\d+\/export\/$/, (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
            await chooseItem(page, 'conversation-export');
            await expect(panel).toBeVisible();
            await page.getByTestId('export-error-dismiss').click();
            await expect(panel).toHaveCount(0);
            await expect(more(page)).toBeFocused();
        });

        test('while a turn is pending New and Delete are disabled, and a confirmation that was open is withdrawn', async ({page}) => {
            let open: () => void = () => undefined;
            const gate = new Promise<void>((resolve) => { open = resolve; });
            let arrived: () => void = () => undefined;
            const reached = new Promise<void>((resolve) => { arrived = resolve; });
            await page.route(TURN_URL, async (route) => {
                if (route.request().method() !== 'POST') return route.fallback();
                arrived();
                await gate;
                return route.continue();
            });
            await chooseItem(page, 'conversation-delete');
            await expect(page.getByTestId('confirm-panel')).toBeVisible();
            await page.getByTestId('assistant-composer').fill('Hold this turn.');
            await page.getByRole('button', {name: 'Send message'}).click();
            await reached;
            await expect(page.getByTestId('pending-status')).toBeVisible();
            await expect(page.getByTestId('confirm-panel')).toHaveCount(0);
            await more(page).click();
            await expect(page.getByTestId('conversation-new')).toBeDisabled();
            await expect(page.getByTestId('conversation-delete')).toBeDisabled();
            await expect(page.getByTestId('conversation-export')).toBeEnabled();
            await expect(page.getByTestId('conversation-about-history')).toBeEnabled();
            await more(page).click();
            await expect(menu(page)).toHaveCount(0);
            open();
            await expect(page.getByTestId('pending-status')).toHaveCount(0, {timeout: 30_000});
            await more(page).click();
            await expect(page.getByTestId('conversation-new')).toBeEnabled();
        });

        if (size.sheet) {
            test('every control of the menu and its confirmations is a 44px target at 375px', async ({page}) => {
                const tall = async (locator: Locator, what: string) => {
                    const box = (await locator.boundingBox())!;
                    expect(box, `${what} is on screen`).not.toBeNull();
                    expect(box.height, `${what} is at least 44px tall`).toBeGreaterThanOrEqual(43.5);
                    expect(box.width, `${what} is at least 44px wide`).toBeGreaterThanOrEqual(43.5);
                };
                await tall(more(page), 'More');
                await more(page).click();
                for (const id of ['conversation-new', 'conversation-export', 'conversation-about-history', 'conversation-delete']) {
                    await tall(page.getByTestId(id), id);
                }
                await page.getByTestId('conversation-delete').click();
                await tall(page.getByTestId('confirm-action'), 'the confirm button');
                await tall(page.getByTestId('confirm-cancel'), 'Cancel');
                await tall(page.getByTestId('confirm-export-first'), 'Export first');
            });
        }
    });
}
