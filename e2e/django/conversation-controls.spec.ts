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

async function openAssistant(page: Page, settle = true): Promise<void> {
    await page.goto('/');
    const panel = page.getByTestId('assistant-panel');
    const opener = page.locator('[data-testid="assistant-launcher"], [data-testid="assistant-restore"]');
    await expect(panel.or(opener).first()).toBeVisible();
    if (!(await panel.isVisible())) await opener.click({timeout: 3000}).catch(async () => { await expect(panel).toBeVisible(); });
    await expect(page.getByTestId('assistant-composer')).toBeVisible();
    await expect(page.getByTestId('chat-loading')).toHaveCount(0);
    if (settle) await page.waitForLoadState('networkidle');
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
            // The text is typed first: under an open confirmation the composer is inert (issue #547), so the turn is
            // started by a non-pointer trigger, as a late send from elsewhere would be.
            await page.getByTestId('assistant-composer').fill('Hold this turn.');
            await chooseItem(page, 'conversation-delete');
            await expect(page.getByTestId('confirm-panel')).toBeVisible();
            await page.locator('#job-search-chat form').evaluate((form) => (form as HTMLFormElement).requestSubmit());
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

// Issue #547: the consequence comes before the destructive button at every supported size, an ended session is
// reported as one, an export in flight holds Delete back, and nothing beneath an open confirmation can be pressed.
const CONFIRM_SIZES = [
    {name: 'short sheet', width: 320, height: 420},
    {name: 'sheet', width: 320, height: 640},
    {name: 'sheet', width: 375, height: 667},
    {name: 'drawer', width: 1024, height: 800},
    {name: 'docked', width: 1280, height: 900},
];
const SESSION_ENDED = 'Your session has ended. Sign in again, then try again.';
const DELETE_URL = /\/api\/agent\/conversations\/\d+\/delete\/$/;
const EXPORT_URL = /\/api\/agent\/conversations\/\d+\/export\/$/;
// What the priorities row reads with six saved priorities: the long first requirement wraps to two lines on a phone.
const SUMMARY_CHIPS = [
    {path: 'compensation.minimum_salary', label: 'Minimum base salary', display: '150000', hard: true, supported: true},
    {path: 'compensation.minimum_total_compensation', label: 'Minimum total compensation', display: '250000', hard: false, supported: true},
    {path: 'compensation.equity_minimum_percent', label: 'Minimum equity (%)', display: '0.5', hard: false, supported: true},
    {path: 'work_location.modes', label: 'Work arrangement', display: 'Remote, Hybrid', hard: true, supported: true},
    {path: 'industry', label: 'Industry', display: 'Fintech', hard: false, supported: true},
    {path: 'roles.titles', label: 'Role titles', display: 'Staff Engineer', hard: false, supported: true},
];

interface ConfirmationMeasure {
    stack: {top: number; bottom: number};
    heading: {top: number; bottom: number};
    copy: {top: number; bottom: number};
    action: {top: number; bottom: number};
    cancel: {top: number; bottom: number};
    error: {top: number; bottom: number} | null;
    viewportHeight: number;
    controls: {id: string; left: number; right: number; top: number; bottom: number}[];
}

/** Every element of the open confirmation, measured in one pass with getBoundingClientRect. */
async function measureConfirmation(page: Page): Promise<ConfirmationMeasure> {
    return page.evaluate(() => {
        const box = (el: Element | null) => {
            const r = el!.getBoundingClientRect();
            return {top: r.top, bottom: r.bottom};
        };
        const panel = document.querySelector('[data-testid="confirm-panel"]')!;
        const stack = panel.closest('.chat-panel-stack')!;
        return {
            stack: box(stack),
            heading: box(panel.querySelector('.chat-confirm-heading')),
            copy: box(panel.querySelector('.chat-confirm-copy')),
            action: box(panel.querySelector('[data-testid="confirm-action"]')),
            cancel: box(panel.querySelector('[data-testid="confirm-cancel"]')),
            error: panel.querySelector('.chat-confirm-error') ? box(panel.querySelector('.chat-confirm-error')) : null,
            viewportHeight: window.innerHeight,
            // The buttons and the text blocks they must never cover.
            controls: Array.from(panel.querySelectorAll('button, .chat-confirm-heading, .chat-confirm-copy, .chat-confirm-error')).map((b) => {
                const r = b.getBoundingClientRect();
                return {id: (b as HTMLElement).dataset.testid ?? b.className, left: r.left, right: r.right, top: r.top, bottom: r.bottom};
            }),
        };
    });
}

/** The three rules of the confirmation's layout; `opened` adds the rule that holds when it has just opened. */
function checkConfirmation(m: ConfirmationMeasure, opened: boolean): string[] {
    const problems: string[] = [];
    const slack = 0.5;
    if (opened) {
        if (m.heading.top < m.stack.top - slack) problems.push('the heading starts above the stack');
        if (m.copy.bottom > m.stack.bottom + slack) problems.push('the consequence sentence is clipped by the stack');
    }
    // The consequence, a failure's text and the action row are on screen together (no scrolling) at every size.
    if (m.copy.top < m.stack.top - slack || m.copy.bottom > m.stack.bottom + slack) problems.push('the consequence sentence is not inside the stack');
    if (m.error && (m.error.top < m.stack.top - slack || m.error.bottom > m.stack.bottom + slack)) problems.push('the failure text is not inside the stack');
    if (m.action.top < m.copy.top - slack) problems.push('the destructive button is above the sentence');
    for (const [name, box] of [['the action button', m.action], ['Cancel', m.cancel]] as const) {
        if (box.bottom > m.stack.bottom + slack || box.bottom > m.viewportHeight + slack) problems.push(`${name} is not on screen`);
    }
    for (let i = 0; i < m.controls.length; i += 1) {
        for (let j = i + 1; j < m.controls.length; j += 1) {
            const a = m.controls[i];
            const b = m.controls[j];
            const overlap = a.left < b.right - slack && b.left < a.right - slack && a.top < b.bottom - slack && b.top < a.bottom - slack;
            if (overlap) problems.push(`${a.id} overlaps ${b.id}`);
        }
    }
    return problems;
}

test.describe('conversation controls follow-ups (issue #547)', () => {
    test.skip(process.env.CRANK_E2E_PROVIDER_FAILURE === '1', 'needs assistant replies: not part of the provider-outage pass');

    let startPriorities: Awaited<ReturnType<typeof savedPriorities>>;
    test.beforeEach(async ({page}) => {
        requireDjangoTier();
        await page.setViewportSize({width: 1280, height: 900});
        await login(page, E2E_USERNAME, E2E_PASSWORD);
        await openAssistant(page);
        await ensureTurns(page, 2);
        startPriorities = await savedPriorities(page);
    });

    test.afterEach(async ({page}, testInfo) => {
        if (testInfo.status === 'skipped') return;
        await page.unrouteAll({behavior: 'ignoreErrors'});
        await page.goto('/');
        expect(await savedPriorities(page), 'saved priorities are unchanged').toEqual(startPriorities);
    });

    const expectLayout = async (page: Page, opened: boolean) => {
        await expect.poll(async () => checkConfirmation(await measureConfirmation(page), opened), {timeout: 5000}).toEqual([]);
    };
    const failDelete = async (page: Page) => {
        await page.route(DELETE_URL, (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
        await waitForConfirmArmed(page);
        await page.getByTestId('confirm-action').click();
        await expect(page.getByRole('alert').filter({hasText: 'Could not delete the conversation.'})).toBeVisible();
    };

    // After a failed Export first the export-error panel sits above the confirmation: the stack scrolls to its
    // retry, and the heading and the whole sentence must stay inside the stack (the Delete row may be cut at the bottom).
    const expectExportFailureKeepsSentence = async (page: Page) => {
        // A typed draft leaves Send enabled, so only the [inert] dimming (never .btn:disabled) can make it look unavailable.
        const send = page.getByRole('button', {name: 'Send message'});
        await page.getByTestId('assistant-composer').fill('A draft that keeps Send enabled.');
        await expect(send).toBeEnabled();
        expect(await send.evaluate((b) => [getComputedStyle(b).opacity, getComputedStyle(b).cursor]), 'Send is fully shown before the confirmation').toEqual(['1', 'pointer']);
        await chooseItem(page, 'conversation-delete');
        await page.route(EXPORT_URL, (route) => route.fulfill({status: 500, contentType: 'application/json', body: '{}'}));
        await page.getByTestId('confirm-export-first').click();
        const panel = page.getByTestId('export-error');
        await expect(panel).toBeVisible();
        // Two panels in the stack: the gap between them shows the panel background, never the transcript or composer.
        // Under the open confirmation Send (and every footer button) is dimmed as the disabled control it behaves as.
        const chrome = await page.evaluate(() => {
            const stack = document.querySelector('.chat-panel-stack')!;
            const probe = document.createElement('div');
            probe.style.backgroundColor = 'var(--bs-body-bg)';
            stack.parentElement!.appendChild(probe);
            const body = getComputedStyle(probe).backgroundColor;
            probe.remove();
            return {
                stack: getComputedStyle(stack).backgroundColor,
                body,
                footerButtons: Array.from(document.querySelectorAll('#job-search-chat .chat-footer .btn')).map((b) => getComputedStyle(b).opacity),
                sendCursor: getComputedStyle(document.querySelector('#job-search-chat .chat-footer .chat-send')!).cursor,
            };
        });
        expect(chrome.body, 'the panel background resolves').not.toBe('rgba(0, 0, 0, 0)');
        expect(chrome.stack, 'the gap between the two panels shows the panel background').toBe(chrome.body);
        expect(chrome.footerButtons.length, 'the footer has buttons').toBeGreaterThan(0);
        expect(chrome.footerButtons, 'every footer button, Send included, is dimmed under the open confirmation').toEqual(chrome.footerButtons.map(() => '0.65'));
        expect(chrome.sendCursor, 'Send reads as unavailable under the open confirmation').toBe('default');
        await expect.poll(async () => page.evaluate(() => {
            const slack = 0.5;
            const stack = document.querySelector('.chat-panel-stack')!.getBoundingClientRect();
            const box = (sel: string) => document.querySelector(sel)!.getBoundingClientRect();
            const problems: string[] = [];
            for (const [name, sel] of [['the export message', '[data-testid="export-error"] .chat-confirm-error'], ['the retry', '[data-testid="export-error"] button']] as const) {
                const r = box(sel);
                if (r.top < stack.top - slack || r.bottom > stack.bottom + slack) problems.push(`${name} is not inside the stack`);
            }
            // The rule: the destructive button is never on screen while any part of the sentence is clipped.
            const action = box('[data-testid="confirm-action"]');
            const copy = box('[data-testid="confirm-panel"] .chat-confirm-copy');
            const actionShown = action.bottom > stack.top + slack && action.top < stack.bottom - slack;
            if (actionShown && (copy.top < stack.top - slack || copy.bottom > stack.bottom + slack)) problems.push('the destructive button shows while the sentence is clipped');
            // Nothing of the confirmation is scrolled above the stack, so the heading and sentence are only ever cut at the bottom.
            if (box('[data-testid="confirm-panel"] .chat-confirm-heading').top < stack.top - slack) problems.push('the heading is scrolled above the stack');
            return problems;
        }), {timeout: 5000}).toEqual([]);
        // A Delete failure that follows keeps the sentence beside its button.
        await page.unroute(EXPORT_URL);
        await failDelete(page);
        await expectLayout(page, false);
        // The draft is stored per conversation: leave none behind for the next case.
        await page.getByTestId('confirm-cancel').click();
        await page.getByTestId('assistant-composer').fill('');
        await expect(page.getByTestId('assistant-composer')).toHaveValue('');
    };

    for (const size of [{name: 'phone', width: 320, height: 480}, {name: 'phone', width: 375, height: 500}, {name: 'phone', width: 360, height: 520}]) {
        test(`Export first failing keeps the whole consequence in the stack, ${size.name} ${size.width}x${size.height}`, async ({page}) => {
            await page.setViewportSize({width: size.width, height: size.height});
            await openAssistant(page);
            await expectExportFailureKeepsSentence(page);
        });
    }

    for (const size of CONFIRM_SIZES) {
        test.describe(`${size.name} ${size.width}x${size.height}`, () => {
            test.beforeEach(async ({page}) => {
                await page.setViewportSize({width: size.width, height: size.height});
                await openAssistant(page);
            });

            test('the Delete confirmation opens on its heading and the whole consequence, the button after it', async ({page}) => {
                await chooseItem(page, 'conversation-delete');
                await expect(page.getByTestId('confirm-panel')).toContainText(DELETE_COPY);
                await expectLayout(page, true);
            });

            test('the New confirmation opens on its heading and the whole consequence', async ({page}) => {
                await chooseItem(page, 'conversation-new');
                await expect(page.getByTestId('confirm-panel')).toContainText(NEW_COPY);
                await expectLayout(page, true);
            });

            test('a Delete failure keeps the consequence before the button and no controls overlap', async ({page}) => {
                await chooseItem(page, 'conversation-delete');
                await failDelete(page);
                await expectLayout(page, false);
            });

            test('a Delete failure under a two-line priorities summary', async ({page}) => {
                // The browser resolves the test host, the route's own fetch cannot: the real answer is read in the page.
                await page.route('**/api/agent/preferences/', async (route) => {
                    if (route.request().method() !== 'GET' || route.request().headers()['x-spec-real']) return route.fallback();
                    const real = await page.evaluate(async () => (await fetch('/api/agent/preferences/', {headers: {'x-spec-real': '1'}})).json());
                    return route.fulfill({
                        status: 200,
                        contentType: 'application/json',
                        body: JSON.stringify({...real, chips: SUMMARY_CHIPS}),
                    });
                });
                await openAssistant(page, false);
                await expect(page.getByTestId('priorities-summary')).toBeVisible();
                await chooseItem(page, 'conversation-delete');
                await expectLayout(page, true);
                await failDelete(page);
                await expectLayout(page, false);
            });

            test('a Delete failure after the session ended keeps the sentence, the sign-in link and the retry together', async ({page}) => {
                await page.route(DELETE_URL, (route) => route.fulfill({status: 401, contentType: 'application/json', body: '{}'}));
                await chooseItem(page, 'conversation-delete');
                await waitForConfirmArmed(page);
                await page.getByTestId('confirm-action').click();
                await expect(page.getByRole('alert').filter({hasText: SESSION_ENDED})).toBeVisible();
                await expect(page.getByTestId('confirm-sign-in')).toHaveAttribute('href', /^\/accounts\/login\/\?next=/);
                await expectLayout(page, false);
            });

            test('Export first failing keeps the whole consequence in the stack', async ({page}) => {
                await expectExportFailureKeepsSentence(page);
            });

            test('Export first in flight: Exporting…, Delete disabled, no controls overlap', async ({page}) => {
                let release: () => void = () => undefined;
                const gate = new Promise<void>((resolve) => { release = resolve; });
                await page.route(EXPORT_URL, async (route) => { await gate; return route.continue(); });
                await chooseItem(page, 'conversation-delete');
                await page.getByTestId('confirm-export-first').click();
                await expect(page.getByTestId('confirm-export-first')).toHaveText('Exporting…');
                await expectLayout(page, false);
                release();
                await expect(page.getByTestId('confirm-export-first')).toHaveText('Export first');
            });
        });
    }

    test('an ended session on Delete and on New is reported and nothing is removed', async ({page, context}) => {
        const replies = page.locator('article[aria-label="Assistant message"]');
        const before = await replies.count();
        await chooseItem(page, 'conversation-delete');
        await waitForConfirmArmed(page);
        await context.clearCookies({name: 'sessionid'});
        try {
            await page.getByTestId('confirm-action').click();
            await expect(page.getByRole('alert').filter({hasText: SESSION_ENDED})).toBeVisible();
            await expect(page.getByTestId('confirm-action')).toHaveText('Try deleting again');
            expect(await replies.count(), 'the transcript is as it was').toBe(before);
            await page.getByTestId('confirm-cancel').click();
            await chooseItem(page, 'conversation-new');
            await waitForConfirmArmed(page);
            await page.getByTestId('confirm-action').click();
            await expect(page.getByRole('alert').filter({hasText: SESSION_ENDED})).toBeVisible();
            expect(await replies.count()).toBe(before);
        } finally {
            // Signed in again even when an assertion failed, so the shared afterEach reads the saved priorities as the user.
            await login(page);
        }
    });

    test('an ended session on Export is reported in the export-error panel and nothing downloads', async ({page, context}) => {
        const downloads: string[] = [];
        page.on('download', (download) => downloads.push(download.suggestedFilename()));
        await context.clearCookies({name: 'sessionid'});
        try {
            await chooseItem(page, 'conversation-export');
            await expect(page.getByTestId('export-error')).toContainText(SESSION_ENDED);
            await expect(page.getByTestId('conversation-status')).toHaveText('');
            expect(downloads).toEqual([]);
        } finally {
            await login(page);
        }
    });

    test('Delete waits for an export in flight, and a second press starts no second request', async ({page}) => {
        let release: () => void = () => undefined;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        let exports = 0;
        await page.route(EXPORT_URL, async (route) => { exports += 1; await gate; return route.continue(); });
        await chooseItem(page, 'conversation-delete');
        const exportFirst = page.getByTestId('confirm-export-first');
        await exportFirst.click();
        await expect(exportFirst).toHaveAttribute('aria-disabled', 'true');
        await expect.poll(() => exports, {message: 'the first press started one request'}).toBe(1);
        // A second press while the export is held (focus stays on the button it was pressed with) starts nothing.
        await expect(exportFirst).toBeFocused();
        await page.keyboard.press('Enter');
        // aria-disabled, so Playwright's actionability check would wait forever: press it as a pointer would.
        await exportFirst.click({force: true});
        await expect(page.getByTestId('confirm-action')).toBeDisabled();
        await expect(page.getByTestId('confirm-cancel')).toBeEnabled();
        await expect(page.getByTestId('conversation-status')).toHaveText('Exporting conversation…');
        await more(page).click();
        await expect(page.getByTestId('conversation-export')).toBeDisabled();
        await more(page).click();
        release();
        await expect(page.getByTestId('confirm-action')).toBeEnabled();
        expect(exports, 'two more presses while it was held started no second request').toBe(1);
        await page.getByTestId('confirm-cancel').click();
    });

    test('Export first keeps keyboard focus, and its ring, while it exports', async ({page}) => {
        let release: () => void = () => undefined;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        await page.route(EXPORT_URL, async (route) => { await gate; return route.continue(); });
        await chooseItem(page, 'conversation-delete');
        const exportFirst = page.getByTestId('confirm-export-first');
        await page.getByTestId('confirm-action').focus();
        await page.keyboard.press('Tab');
        await page.keyboard.press('Tab');
        await expect(exportFirst).toBeFocused();
        const ring = () => exportFirst.evaluate((el) => {
            const style = getComputedStyle(el);
            return {style: style.outlineStyle, width: style.outlineWidth, color: style.outlineColor};
        });
        const visibleRing = await ring();
        expect(visibleRing, 'the ring shows on the focused button').toMatchObject({style: 'solid', width: '2px'});
        expect(visibleRing.color, 'the ring is not transparent').not.toBe('rgba(0, 0, 0, 0)');
        await page.keyboard.press('Enter');
        await expect(exportFirst).toHaveText('Exporting…');
        await expect(exportFirst, 'focus does not fall to the page body').toBeFocused();
        expect(await ring(), 'the ring stays while it exports').toEqual(visibleRing);
        release();
        await expect(exportFirst).toHaveText('Export first');
        await expect(exportFirst).toBeFocused();
        expect(await ring(), 'the ring is still there when it finishes').toEqual(visibleRing);
        await page.getByTestId('confirm-cancel').click();
    });

    test('nothing beneath an open confirmation can be reached; the composer is off while a Delete is in flight', async ({page}) => {
        let release: () => void = () => undefined;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        await page.route(DELETE_URL, async (route) => {
            await gate;
            return route.fulfill({status: 500, contentType: 'application/json', body: '{}'});
        });
        await chooseItem(page, 'conversation-delete');
        const inertOf = (selector: string) => page.locator(selector).evaluate((el) => el.hasAttribute('inert'));
        expect(await inertOf('.chat-transcript-wrap')).toBe(true);
        expect(await inertOf('.chat-footer')).toBe(true);
        await page.getByTestId('confirm-cancel').focus();
        for (let step = 0; step < 6; step += 1) {
            await page.keyboard.press('Tab');
            const reached = await page.evaluate(() => !!document.activeElement?.closest('.chat-transcript-wrap, .chat-footer'));
            expect(reached, 'Tab never lands in the transcript or the footer').toBe(false);
        }
        await waitForConfirmArmed(page);
        await page.getByTestId('confirm-action').click();
        await expect(page.getByTestId('assistant-composer')).toBeDisabled();
        release();
        await expect(page.getByRole('alert').filter({hasText: 'Could not delete the conversation.'})).toBeVisible();
        await page.getByTestId('confirm-cancel').click();
        await expect.poll(() => inertOf('.chat-transcript-wrap')).toBe(false);
        await expect.poll(() => inertOf('.chat-footer')).toBe(false);
        await expect(page.getByTestId('assistant-composer')).toBeEnabled();
    });
});
