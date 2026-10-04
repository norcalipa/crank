// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Validated page context and explicit assistant actions (issue #484). The
// seeded demo provider answers "only remote" with a propose_filters action and
// "open it" with open_company, and only when the page context was sent.
import {expect, Locator, Page, test} from '@playwright/test';
import {E2E_PASSWORD, expectNoHorizontalOverflow, login, requireDjangoTier} from './support';

const PREFS_USER = 'e2e_prefs_user';

async function openAssistant(page: Page): Promise<void> {
    const launcher = page.getByTestId('assistant-launcher');
    if (await launcher.count() > 0) {
        await launcher.click();
    }
    await expect(page.getByTestId('assistant-panel')).toBeVisible();
    await expect(page.getByTestId('assistant-composer')).toBeEnabled();
}

async function ask(page: Page, text: string): Promise<void> {
    await page.getByTestId('assistant-composer').fill(text);
    await page.getByRole('button', {name: 'Send message'}).click();
}

// The adopted #542 rule: the jump pill shows exactly when the reader is more
// than 48px from the bottom of whichever element scrolls the transcript.
async function expectPillFollowsPosition(page: Page): Promise<void> {
    await expect.poll(async () => {
        const away = await page.evaluate(() => {
            const log = document.querySelector('[role="log"]') as HTMLElement;
            const panel = log.closest('.assistant-panel-body') as HTMLElement | null;
            const owner = document.querySelector('#job-search-chat > [data-scroll-owner]')?.getAttribute('data-scroll-owner');
            const el = owner === 'panel' && panel ? panel : log;
            return el.scrollHeight - el.clientHeight - el.scrollTop > 48;
        });
        const shown = await page.getByTestId('jump-to-latest').count();
        return shown === (away ? 1 : 0);
    }, {message: 'the jump pill follows the 48px near-bottom rule'}).toBe(true);
}

async function postJson(page: Page, url: string, body: unknown): Promise<{status: number; body: any}> {
    return page.evaluate(async ({url: target, body: payload}) => {
        const cookie = document.cookie.split('; ').find((row) => row.startsWith('csrftoken='));
        const res = await fetch(target, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': cookie ? decodeURIComponent(cookie.slice('csrftoken='.length)) : '',
            },
            body: JSON.stringify(payload),
        });
        return {status: res.status, body: await res.json()};
    }, {url, body});
}

async function setWorkModes(page: Page, modes: string[]): Promise<void> {
    const proposed = await postJson(page, '/api/agent/preferences/propose/', {
        patch: {set: {'work_location.modes': modes}}, scope: 'account',
    });
    expect(proposed.status).toBe(200);
    const applied = await postJson(page, '/api/agent/preferences/apply/', {
        proposal: proposed.body.token, decision: 'apply',
    });
    expect(applied.status).toBeLessThan(300);
}

// Visible, inside the transcript and not covered by anything (a pinned
// composer, a sticky bar): what the reader can actually see and press.
async function expectUnobstructed(page: Page, target: Locator): Promise<void> {
    await expect(target).toBeVisible();
    await expect.poll(() => target.evaluate((el) => {
        const rect = el.getBoundingClientRect();
        const log = document.querySelector('[role="log"]');
        const bounds = log ? log.getBoundingClientRect() : {top: 0, bottom: window.innerHeight};
        if (rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1) {
            return 'outside the transcript';
        }
        const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
        return hit && (hit === el || el.contains(hit) || hit.contains(el)) ? 'clear' : 'covered';
    }), {timeout: 5000}).toBe('clear');
}

const applyRemote = (page: Page) => page.getByRole('button', {name: 'Apply remote filter'});

test.describe('assistant actions (issue #484)', () => {
    test.beforeEach(async ({page}) => {
        requireDjangoTier();
        await page.setViewportSize({width: 1280, height: 900});
    });

    test('a filter suggestion is a button; applying it filters, survives reload and Back restores', async ({page}) => {
        await login(page);
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        await openAssistant(page);
        await ask(page, 'Show only remote companies');

        const button = applyRemote(page);
        await expect(button).toBeVisible();
        // Nothing changes until the button is pressed.
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        expect(new URL(page.url()).searchParams.has('rto')).toBe(false);

        await button.focus();
        await button.press('Enter');
        await expect(page.getByText(/Showing 1-1 of 1 organizations/)).toBeVisible();
        await expect(page.getByTestId('filter-chip-rto')).toContainText('RTO policy: Remote');
        expect(new URL(page.url()).searchParams.get('rto')).toBe('R');
        // Keyboard focus stays on the button, which reports the outcome.
        const applied = page.getByRole('button', {name: 'Remote filter applied'});
        await expect(applied).toBeFocused();
        await expect(applied).toBeDisabled();

        await page.reload();
        await expect(page.getByText(/Showing 1-1 of 1 organizations/)).toBeVisible();
        await expect(page.getByTestId('filter-chip-rto')).toBeVisible();

        await page.goBack();
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        await expect(page.getByTestId('filter-chip-rto')).toHaveCount(0);
    });

    test('removing the RTO chip clears the filter and the URL', async ({page}) => {
        await login(page);
        await page.goto('/?rto=R');
        await expect(page.getByText(/Showing 1-1 of 1 organizations/)).toBeVisible();
        await page.getByRole('button', {name: 'Remove filter: RTO policy Remote'}).click();
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        expect(new URL(page.url()).searchParams.has('rto')).toBe(false);
    });

    test('an unknown rto value in the URL is ignored', async ({page}) => {
        await login(page);
        await page.goto('/?rto=ZZZ');
        await expect(page.getByText('Showing 1-4 of 4 organizations')).toBeVisible();
        await expect(page.getByTestId('filter-chip-rto')).toHaveCount(0);
    });

    test('a reply that lands after the user moved on is disabled with a reason', async ({page}) => {
        await login(page);
        await openAssistant(page);
        // Hold the reply until the page has changed.
        let release: () => void = () => undefined;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        let held: () => void = () => undefined;
        const reachedServer = new Promise<void>((resolve) => { held = resolve; });
        await page.route('**/api/agent/conversations/*/', async (route) => {
            if (route.request().method() === 'POST') {
                held();
                await gate;
            }
            await route.continue();
        });
        await ask(page, 'Show only remote companies');
        // The turn is sent under the context at this moment; change it after.
        await reachedServer;
        await page.getByTestId('accelerated-vesting-checkbox').check();
        await expect.poll(() => new URL(page.url()).searchParams.get('accelerated_vesting')).toBe('1');
        release();
        const button = applyRemote(page);
        await expect(button).toBeDisabled();
        await expect(page.getByTestId('assistant-actions-stale')).toHaveText('This suggestion was for an earlier view.');
        // The reason is shown, in view, and the pill agrees with the scroll position.
        await expectUnobstructed(page, page.getByTestId('assistant-actions-stale'));
        await expectPillFollowsPosition(page);
        await button.click({force: true});
        expect(new URL(page.url()).searchParams.has('rto')).toBe(false);
    });

    test('Back after applying removes the filter, so the same suggestion can be applied again', async ({page}) => {
        await login(page);
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await applyRemote(page).click();
        await expect(page.getByTestId('filter-chip-rto')).toBeVisible();
        await expect(page.getByRole('button', {name: 'Remote filter applied'})).toBeDisabled();
        await page.goBack();
        await expect(page.getByTestId('filter-chip-rto')).toHaveCount(0);
        // The filter is gone, so the button no longer claims it is applied.
        await expect(page.getByRole('button', {name: 'Remote filter applied'})).toHaveCount(0);
        await expect(applyRemote(page)).toBeEnabled();
        await applyRemote(page).click();
        await expect(page.getByTestId('filter-chip-rto')).toBeVisible();
        expect(new URL(page.url()).searchParams.get('rto')).toBe('R');
    });

    test('applying one action leaves the other action of the same reply usable', async ({page}) => {
        await login(page);
        await page.getByRole('button', {name: /View details for E2E Beta/}).first().click();
        await page.getByRole('link', {name: /Ask the assistant about E2E Beta/}).click();
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        await ask(page, 'Show only remote companies and open it');
        const open = page.getByRole('button', {name: /^Open E2E Beta/});
        await expect(applyRemote(page)).toBeVisible();
        await expect(open).toBeVisible();
        await applyRemote(page).click();
        await expect(page.getByTestId('filter-chip-rto')).toBeVisible();
        await expect(page.getByRole('button', {name: 'Remote filter applied'})).toBeDisabled();
        await expect(open).toBeEnabled();
        await expect(page.getByTestId('assistant-actions-stale')).toHaveText('');
        await open.click();
        await expect(page.getByRole('dialog')).toBeVisible();
        await expect(page.getByRole('button', {name: /^Opened E2E Beta/})).toBeDisabled();
    });

    test('Open, close the dialog, then Apply: the sibling stays usable whatever the click order', async ({page}) => {
        await login(page);
        await page.getByRole('button', {name: /View details for E2E Beta/}).first().click();
        await page.getByRole('link', {name: /Ask the assistant about E2E Beta/}).click();
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        await ask(page, 'Show only remote companies and open it');
        const open = page.getByRole('button', {name: /^Open E2E Beta/});
        await expect(applyRemote(page)).toBeVisible();
        await open.click();
        await expect(page.getByRole('dialog')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.getByRole('dialog')).toHaveCount(0);
        // Past any short window a time-based check would allow.
        await page.waitForTimeout(600);
        await expect(applyRemote(page)).toBeEnabled();
        await expect(page.getByTestId('assistant-actions-stale')).toHaveText('');
        await applyRemote(page).click();
        await expect(page.getByTestId('filter-chip-rto')).toBeVisible();
        await expect(page.getByRole('button', {name: 'Remote filter applied'})).toBeDisabled();
        await expect(page.getByRole('button', {name: /^Opened E2E Beta/})).toBeDisabled();
        await expect(page.getByTestId('assistant-actions-stale')).toHaveText('');
    });

    test('an open_company suggestion reopens the company dialog from the assistant', async ({page}) => {
        await login(page);
        await page.getByRole('button', {name: /View details for E2E Alpha/}).first().click();
        await page.getByRole('link', {name: /Ask the assistant about E2E Alpha/}).click();
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        await expect(page.getByRole('dialog')).toHaveCount(0);
        await ask(page, 'Open this company');
        const open = page.getByRole('button', {name: /^Open E2E Alpha/});
        await expect(open).toBeVisible();
        await open.click();
        await expect(page.getByRole('dialog')).toBeVisible();
    });

    test('signing in as another account leaves no actions behind', async ({page}) => {
        await login(page);
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await expect(applyRemote(page)).toBeVisible();
        await page.goto('/accounts/logout/');
        const confirm = page.getByRole('button', {name: /Sign Out/});
        if (await confirm.count() > 0) {
            await confirm.click();
        }
        await login(page, PREFS_USER, E2E_PASSWORD);
        await openAssistant(page);
        await expect(page.getByRole('button', {name: /remote filter/})).toHaveCount(0);
    });

    test('save as a lasting requirement says so when it is already saved; nothing is written', async ({page}) => {
        await login(page, PREFS_USER, E2E_PASSWORD);
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await applyRemote(page).click();
        // The seeded account already prefers remote work: the background check
        // says so up front and Save is never offered.
        await expect(page.getByTestId('assistant-action-already')).toContainText('Already one of your requirements');
        await expect(page.getByRole('button', {name: 'Save as a requirement'})).toHaveCount(0);
        await expect(page.getByTestId('assistant-action-review')).toHaveCount(0);
    });

    test('save as a requirement through the real API: Cancel writes nothing, Save writes, Undo restores', async ({page}) => {
        await login(page, PREFS_USER, E2E_PASSWORD);
        await page.goto('/');
        // Move the account off remote so there is something to save; put it back at the end.
        await setWorkModes(page, ['hybrid']);
        try {
            await page.goto('/');
            let applies = 0;
            let undos = 0;
            page.on('request', (request) => {
                if (request.method() !== 'POST') {
                    return;
                }
                applies += request.url().includes('/api/agent/preferences/apply/') ? 1 : 0;
                undos += request.url().includes('/api/agent/preferences/undo/') ? 1 : 0;
            });
            await openAssistant(page);
            await ask(page, 'Show only remote companies');
            await applyRemote(page).click();
            const follow = page.getByRole('button', {name: 'Save as a requirement'});
            await follow.click();
            const review = page.getByTestId('assistant-action-review');
            await expect(review).toContainText('Work arrangement');
            await review.getByRole('button', {name: 'Cancel'}).click();
            await expect(review).toHaveCount(0);
            await expect(follow).toBeFocused();
            expect(applies).toBe(0);

            await follow.click();
            // Save is armed ~500ms after the review mounts, so an accidental double activation cannot commit.
            await page.waitForTimeout(700);
            await review.getByRole('button', {name: 'Save', exact: true}).click();
            const saved = page.getByTestId('assistant-action-saved');
            await expect(saved).toContainText('Saved to your account.');
            await expect(saved).toBeFocused();
            expect(applies).toBe(1);

            await saved.getByRole('button', {name: 'Undo'}).click();
            await expect(saved).toContainText('Change undone.');
            expect(undos).toBe(1);
        } finally {
            await setWorkModes(page, ['remote']);
        }
    });

    async function openSaveCard(page: Page, height: number) {
        await page.setViewportSize({width: 320, height});
        await login(page);
        await page.route('**/api/agent/preferences/propose/', (route) => route.fulfill({
            status: 200, contentType: 'application/json', body: JSON.stringify({
                id: 'p1', scope: 'account', change_count: 1, base_revision: 4, unsupported_criteria: [],
                changes: [{path: 'work_location.modes', old: ['hybrid'], new: ['remote']}],
                token: {patch: {set: {'work_location.modes': ['remote']}}, scope: 'account', base_revision: 4},
            }),
        }));
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await applyRemote(page).click();
        await page.getByRole('button', {name: 'Save as a requirement'}).click();
        const review = page.getByTestId('assistant-action-review');
        await expect(review).toBeVisible();
        await expect(review).toContainText('Work arrangement');
        return {
            review,
            save: review.getByRole('button', {name: 'Save', exact: true}),
            heading: review.getByRole('heading', {name: 'Save as a requirement?'}),
            changes: review.getByText('Work arrangement'),
        };
    }

    test('the save card confirm button is fully visible at 320x800 and the jump pill follows the 48px rule', async ({page}) => {
        const {save, heading} = await openSaveCard(page, 800);
        await expect(heading).toBeFocused();
        // Both the focused heading and the confirm button stay visible and uncovered.
        await expectUnobstructed(page, heading);
        await expectUnobstructed(page, save);
        await expectPillFollowsPosition(page);
        // Nothing keeps moving the transcript after it has settled.
        const top = await page.getByRole('log').evaluate((el) => el.scrollTop);
        await page.waitForTimeout(1500);
        expect(await page.getByRole('log').evaluate((el) => el.scrollTop)).toBe(top);
        await expectUnobstructed(page, heading);
        await expectUnobstructed(page, save);
    });

    // Real phones: the transcript is 126-211px tall, shorter than the 223px card,
    // so the heading and Save cannot both show. Save and the change list win, and
    // focus lands on something the reader can see.
    for (const height of [568, 640]) {
        test(`the save card at 320x${height} keeps Save and the changes visible, focus visible, and a scrolled-up reader in place`, async ({page}) => {
            const {save, heading, changes} = await openSaveCard(page, height);
            await expectUnobstructed(page, save);
            await expectUnobstructed(page, changes);
            await expectPillFollowsPosition(page);

            const focus = await page.evaluate(() => {
                const el = document.activeElement as HTMLElement | null;
                const log = document.querySelector('[role="log"]') as HTMLElement;
                if (!el || !log) {
                    return {visible: false, label: 'none'};
                }
                const rect = el.getBoundingClientRect();
                const bounds = log.getBoundingClientRect();
                const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                return {
                    visible: rect.bottom > bounds.top && rect.top < bounds.bottom && !!hit && (hit === el || el.contains(hit)),
                    label: el.tagName === 'H3' || el.tagName === 'H4' ? 'heading' : (el.textContent ?? '').trim(),
                };
            });
            expect(focus.visible).toBe(true);
            // The primary action is never the focus target: the heading when it
            // fits, otherwise Cancel (Save stays one Tab away either way).
            expect(['heading', 'Cancel']).toContain(focus.label);
            await expect(save).not.toBeFocused();
            await page.keyboard.press(focus.label === 'heading' ? 'Tab' : 'Shift+Tab');
            await expect(save).toBeFocused();
            await expectUnobstructed(page, save);
            await expect(heading).toHaveCount(1);

            // A reader who scrolls up is not pulled back.
            const log = page.getByRole('log');
            await log.hover();
            await page.mouse.wheel(0, -400);
            await expect.poll(() => log.evaluate((el) => el.scrollTop)).toBeLessThan(
                await log.evaluate((el) => el.scrollHeight) - 400,
            );
            const top = await log.evaluate((el) => el.scrollTop);
            await page.waitForTimeout(1500);
            expect(await log.evaluate((el) => el.scrollTop)).toBe(top);
            await expectPillFollowsPosition(page);
        });
    }

    test('in panel scroll mode (320x420) the save card is scrolled clear of the sticky footer', async ({page}) => {
        const {review, save} = await openSaveCard(page, 420);
        await expect(page.locator('#job-search-chat > [data-scroll-owner="panel"]')).toHaveCount(1);
        await expectUnobstructed(page, save);
        await expectUnobstructed(page, review.getByText('Work arrangement'));
        const clear = await page.evaluate(() => {
            const body = document.querySelector('.assistant-panel-body') as HTMLElement;
            const footer = document.querySelector('#job-search-chat .chat-footer') as HTMLElement;
            return {padding: parseFloat(getComputedStyle(body).scrollPaddingBottom), footer: footer.getBoundingClientRect().height};
        });
        expect(clear.padding).toBeGreaterThanOrEqual(clear.footer - 1);
        await expectPillFollowsPosition(page);
    });

    test('actions and the RTO chip fit at 375px', async ({page}) => {
        await page.setViewportSize({width: 375, height: 800});
        await login(page);
        await page.goto('/?rto=R');
        await expect(page.getByTestId('filter-chip-rto')).toBeVisible();
        await expectNoHorizontalOverflow(page);
        await page.goto('/');
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await expect(applyRemote(page)).toBeVisible();
        await expectNoHorizontalOverflow(page);
        const box = await applyRemote(page).boundingBox();
        expect(box?.height ?? 0).toBeGreaterThanOrEqual(36);
    });

    test('the drawer at 1024px shows the action and applies it', async ({page}) => {
        await page.setViewportSize({width: 1024, height: 800});
        await login(page);
        await openAssistant(page);
        await ask(page, 'Show only remote companies');
        await expect(applyRemote(page)).toBeVisible();
        await expectNoHorizontalOverflow(page);
    });
});
