// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Editable priority chips with a review / apply / undo flow (issue #480).
// Uses the dedicated seeded account `e2e_prefs_user` because these journeys
// mutate the saved preference document.
import {expect, Locator, Page, test} from '@playwright/test';
import {E2E_PASSWORD, expectNoHorizontalOverflow, login, requireDjangoTier} from './support';

const PREFS_USER = 'e2e_prefs_user';
const SALARY = /Minimum base salary/;

async function openEditor(page: Page): Promise<void> {
    await page.goto('/chat/');
    // Desktop shows the main variant; narrow viewports show the sidebar variant.
    const desktop = (page.viewportSize()?.width ?? 1280) >= 768;
    const section = page.getByTestId(desktop ? 'priorities-main' : 'priorities-sidebar');
    await expect(section).toBeVisible();
    await section.getByRole('button', {name: /Edit priorities|Add priorities/}).click();
    await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
}

async function applySalary(page: Page, value: string): Promise<void> {
    await openEditor(page);
    await page.getByRole('spinbutton', {name: SALARY}).fill(value);
    await page.getByRole('button', {name: 'Review changes'}).click();
    await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
    await page.getByRole('button', {name: 'Apply to account'}).click();
    await expect(page.getByRole('list', {name: 'Changed priorities'})).toBeVisible();
}

test.describe('priorities editor (issue #480)', () => {
    test.beforeEach(async ({page}) => {
        requireDjangoTier();
        await page.setViewportSize({width: 1280, height: 900});
        await login(page, PREFS_USER, E2E_PASSWORD);
        // Start every journey from the defaults.
        await page.goto('/chat/');
        const reset = page.getByTestId('priorities-main').getByRole('button', {name: 'Reset priorities'});
        if (await reset.isVisible().catch(() => false)) {
            await reset.click();
            await page.getByTestId('priorities-main').getByRole('button', {name: 'Reset priorities'}).click();
            await expect(page.getByTestId('priorities-main').getByRole('group', {name: 'Confirm reset'})).toHaveCount(0);
        }
    });

    test('edit, review, apply and undo from the editor', async ({page}) => {
        await applySalary(page, '150000');
        await page.getByRole('button', {name: 'Undo'}).click();
        await expect(page.getByText('Change undone.')).toBeVisible();
        await page.getByRole('button', {name: 'Done'}).click();
        await expect(page.getByTestId('priorities-main').getByText(/150,?000/)).toHaveCount(0);
    });

    test('an invalid value keeps the edits and shows an inline error', async ({page}) => {
        await openEditor(page);
        const input = page.getByRole('spinbutton', {name: SALARY});
        await input.fill('-5');
        await page.getByRole('button', {name: 'Review changes'}).click();
        await expect(page.getByRole('alert').first()).toBeVisible();
        await expect(input).toHaveValue('-5');
    });

    test('a stale apply is refused and asks for a fresh review', async ({page, context}) => {
        await openEditor(page);
        await page.getByRole('spinbutton', {name: SALARY}).fill('120000');
        await page.getByRole('button', {name: 'Review changes'}).click();
        await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();

        const other = await context.newPage();
        await applySalary(other, '130000');
        await other.close();

        await page.getByRole('button', {name: 'Apply to account'}).click();
        await expect(page.getByRole('alert').filter({hasText: 'Your priorities changed elsewhere'})).toBeVisible();
        await expect(page.getByRole('button', {name: 'Review latest'})).toBeVisible();
    });

    test('resetting priorities is separate from starting a new conversation', async ({page}) => {
        await applySalary(page, '140000');
        await page.getByRole('button', {name: 'Done'}).click();
        await page.getByTestId('priorities-main').getByRole('button', {name: 'Reset priorities'}).click();
        await expect(page.getByText(/Your conversations are not changed/)).toBeVisible();
        await page.getByRole('button', {name: 'Keep priorities'}).click();
        await expect(page.getByTestId('conversation-more')).toBeVisible();
        await expect(page.getByRole('button', {name: 'Reset chat'})).toHaveCount(0);
    });

    test('the last entry of a list and the only work arrangement can be cleared and saved', async ({page}) => {
        await openEditor(page);
        const places = page.getByRole('textbox', {name: /Excluded locations/});
        await places.fill('San Francisco, CA');
        await places.press('Enter');
        await page.getByRole('checkbox', {name: 'Remote'}).check();
        await page.getByRole('button', {name: 'Review changes'}).click();
        await page.getByRole('button', {name: 'Apply to account'}).click();
        await expect(page.getByRole('list', {name: 'Changed priorities'})).toBeVisible();
        await page.getByRole('button', {name: 'Done'}).click();

        await openEditor(page);
        await page.getByRole('button', {name: 'Remove San Francisco, CA from Excluded locations'}).click();
        await page.getByRole('checkbox', {name: 'Remote'}).uncheck();
        await page.getByRole('button', {name: 'Review changes'}).click();
        await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
        await expect(page.getByText(/must list items/)).toHaveCount(0);
        await page.getByRole('button', {name: 'Apply to account'}).click();
        await expect(page.getByRole('list', {name: 'Changed priorities'})).toBeVisible();
        await page.getByRole('button', {name: 'Done'}).click();
        await expect(page.getByTestId('priorities-main').getByText(/San Francisco/)).toHaveCount(0);
    });

    test('Review latest rebases the review onto the changed priorities', async ({page, context}) => {
        await openEditor(page);
        await page.getByRole('spinbutton', {name: SALARY}).fill('120000');
        await page.getByRole('button', {name: 'Review changes'}).click();
        await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();

        const other = await context.newPage();
        await applySalary(other, '130000');
        await other.close();

        await page.getByRole('button', {name: 'Apply to account'}).click();
        await page.getByRole('button', {name: 'Review latest'}).click();
        const review = page.getByRole('list', {name: 'Proposed changes'});
        await expect(review).toContainText('130,000');
        await expect(review).toContainText('120,000');
        await expect(page.getByRole('button', {name: 'Apply to account'})).toBeVisible();
    });

    test('tabbing through the main editor never puts the focused control under its pinned footer', async ({page}) => {
        await page.setViewportSize({width: 1280, height: 900});
        await openEditor(page);
        const {fields, covered, inFooter} = await tabThroughEditor(page, 'priorities-main');
        expect(inFooter, 'the walk ends in the editor\'s footer').toBe(true);
        expect(fields).toBeGreaterThanOrEqual(30);
        expect(covered, 'focused controls a pointer at their centre does not reach').toEqual([]);
    });

    test('chips stay within the viewport at 375px', async ({page}) => {
        await page.setViewportSize({width: 375, height: 800});
        await applySalary(page, '150000');
        await page.getByRole('button', {name: 'Done'}).click();
        // The sidebar row is collapsed by default: expand it to lay the chips out.
        const section = page.getByTestId('priorities-sidebar');
        await section.getByTestId('priorities-summary-toggle').click();
        await expect(section.getByTestId('priority-chip')).toHaveCount(1);
        await expectNoHorizontalOverflow(page);
    });
});

// Collapsed sidebar row (issue #480, follow-up #539 item 2). The block above the
// chat is one row until expanded, and every expanded step stays within 60% of the
// panel body, so the composer is reachable at every width.
const THREE = {set: {
    'compensation.minimum_salary': 150000, 'work_location.modes': ['remote'], industry: ['fintech'],
    importance: {'compensation.minimum_salary': 1.0, 'work_location.modes': 1.0},
}};
const TEN = {set: {
    'compensation.minimum_salary': 150000, 'compensation.minimum_total_compensation': 250000,
    'compensation.equity_minimum_percent': 0.5, 'compensation.require_public_company': true,
    'work_location.modes': ['remote', 'hybrid'], 'work_location.countries': ['US', 'CA'],
    'work_location.max_in_office_days': 2, industry: ['fintech'], funding_stage: ['series_b'],
    'roles.titles': ['Staff Engineer'],
    importance: {'compensation.minimum_salary': 1.0, 'work_location.modes': 1.0, 'work_location.max_in_office_days': 1.0},
}};
const PANEL_VIEWPORTS = [
    {name: 'docked', width: 1280, height: 900},
    {name: 'drawer', width: 1024, height: 800},
    {name: 'sheet', width: 375, height: 700},
    {name: 'short sheet', width: 320, height: 640},
];
const COLLAPSED_MAX_PX = 72;
const EXPANDED_MAX_SHARE = 0.6;

/** Resets the saved priorities, then applies `patch` (if any) through the real API. Returns the chip count.
 *  The dev server's SQLite answers 409 "retry" while another request holds the write lock, so a write is retried. */
async function setPriorities(page: Page, patch: object | null): Promise<number> {
    const result = await page.evaluate(async (todo) => {
        const csrf = document.cookie.split('; ').find((c) => c.startsWith('csrftoken='))?.split('=')[1] || '';
        const headers = {'Content-Type': 'application/json', 'X-CSRFToken': decodeURIComponent(csrf)};
        const send = (url: string, body: unknown) => fetch(url, {method: 'POST', headers, body: JSON.stringify(body)});
        const read = async () => (await fetch('/api/agent/preferences/')).json();
        const attempt = async (): Promise<string> => {
            const current = await read();
            if (current.chips.length > 0) {
                const reset = await send('/api/agent/preferences/reset/', {expected_revision: current.revision});
                if (!reset.ok) return `reset ${reset.status}`;
            }
            if (!todo) return '';
            const proposed = await send('/api/agent/preferences/propose/', {patch: todo, scope: 'account'});
            if (!proposed.ok) return `propose ${proposed.status}`;
            const applied = await send('/api/agent/preferences/apply/', {proposal: (await proposed.json()).token, decision: 'apply'});
            return applied.ok ? '' : `apply ${applied.status}`;
        };
        let failed = await attempt();
        for (let retry = 0; retry < 3 && failed.endsWith(' 409'); retry += 1) {
            await new Promise((resolve) => setTimeout(resolve, 200));
            failed = await attempt();
        }
        return {failed, chips: failed ? -1 : (await read()).chips.length as number};
    }, patch);
    expect(result.failed).toBe('');
    return result.chips;
}

/** Opens the assistant when it is closed. The page may restore an open panel between the check and the click, which removes the launcher: that is not a failure. */
async function openIfClosed(page: Page) {
    const panel = page.getByTestId('assistant-panel');
    const opener = page.locator('[data-testid="assistant-launcher"], [data-testid="assistant-restore"]');
    if (await panel.isVisible()) return;
    await opener.click({timeout: 3000}).catch(async () => {
        await expect(panel).toBeVisible();
    });
}

/** Opens the assistant on the rankings page (the launcher, or the restore pill after a reload) and returns the row. */
async function openPanel(page: Page) {
    await page.goto('/');
    const panel = page.getByTestId('assistant-panel');
    const opener = page.locator('[data-testid="assistant-launcher"], [data-testid="assistant-restore"]');
    await expect(panel.or(opener).first()).toBeVisible();
    await openIfClosed(page);
    await expect(page.getByTestId('assistant-composer')).toBeVisible();
    // The chat takes focus once when its history has loaded; let that pass before driving the keyboard.
    await page.waitForLoadState('networkidle');
    const section = page.getByTestId('priorities-sidebar');
    await expect(section.getByRole('button', {name: /Edit priorities|Add priorities/})).toBeVisible();
    return section;
}

interface PanelMeasure {
    section: number;
    share: number;
    lines: number;
    pinned: boolean;
    composerInViewport: boolean;
    toggleOnTop: boolean;
    scrollable: boolean;
    actionsInSection: boolean;
}

async function measurePanel(page: Page): Promise<PanelMeasure> {
    return page.evaluate(() => {
        const section = document.querySelector<HTMLElement>('[data-testid="priorities-sidebar"]')!;
        const body = document.querySelector<HTMLElement>('.assistant-panel-body')!;
        const composer = document.querySelector('[data-testid="assistant-composer"]')!.getBoundingClientRect();
        const summary = section.querySelector<HTMLElement>('[data-testid="priorities-summary"], [data-testid="priorities-empty"]');
        const toggle = section.querySelector('[data-testid="priorities-summary-toggle"]');
        const scroller = section.querySelector('.priorities-scroll');
        const box = section.getBoundingClientRect();
        const onTop = (el: Element) => {
            const r = el.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return !!hit && el.contains(hit);
        };
        const actions = Array.from(section.querySelectorAll<HTMLElement>(
            '.priorities-footer button, .chat-actions button, .priorities-details .priorities-actions button',
        ));
        const inBlock = (el: Element) => {
            const r = el.getBoundingClientRect();
            return r.top >= box.top - 1 && r.bottom <= box.bottom + 1;
        };
        // The review pins its first action row (Apply and Cancel) so the change stays in view on a short phone;
        // Edit and This search only are in the block and clickable once its list is scrolled to the end.
        const second = actions.filter((el) => el.matches('.priorities-review-compact .priorities-review-edit, .priorities-review-compact .priorities-review-search'));
        const first = actions.filter((el) => !second.includes(el));
        const firstReachable = first.every(inBlock);
        const at = scroller ? scroller.scrollTop : 0;
        if (scroller) scroller.scrollTop = scroller.scrollHeight;
        const secondReachable = second.every((el) => inBlock(el) && onTop(el));
        if (scroller) scroller.scrollTop = at;
        return {
            section: section.offsetHeight,
            share: section.offsetHeight / body.clientHeight,
            lines: summary ? Math.round(summary.clientHeight / parseFloat(getComputedStyle(summary).lineHeight)) : 0,
            // Where the system font is wider the chat may not fit and the panel body scrolls: the block must stay put.
            pinned: box.top >= body.getBoundingClientRect().top - 1,
            composerInViewport: composer.top >= 0 && composer.bottom <= window.innerHeight
                && composer.left >= 0 && composer.right <= window.innerWidth,
            toggleOnTop: toggle ? onTop(toggle) : true,
            scrollable: !!scroller && scroller.scrollHeight > scroller.clientHeight + 1,
            actionsInSection: firstReachable && secondReachable,
        };
    });
}

/** The chat re-measures itself a frame after the block above it resizes: wait for that, then check the bound. */
async function expectBounded(page: Page, bodyHeight: number): Promise<PanelMeasure> {
    // The composer is in view and the panel body has not scrolled the block (and its row) away.
    await expect.poll(async () => {
        const now = await measurePanel(page);
        return now.composerInViewport && now.pinned;
    }).toBe(true);
    const measure = await measurePanel(page);
    // One device pixel of rounding on the share.
    expect(measure.share).toBeLessThanOrEqual(EXPANDED_MAX_SHARE + 1 / bodyHeight);
    expect(measure.actionsInSection).toBe(true);
    return measure;
}

/** The control that holds the keyboard focus inside the priorities block: whether a pointer at its centre reaches
 *  it (or its label). A control that ignores the pointer (the list's Add while its box is empty) is reached through
 *  the row that holds it, never through the editor's pinned footer. Null when the focus is elsewhere. */
async function focusedControl(page: Page, block = 'priorities-sidebar'): Promise<{name: string; inFooter: boolean; reached: boolean} | null> {
    return page.evaluate(async (testId) => {
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const el = document.activeElement as HTMLElement | null;
        const section = document.querySelector<HTMLElement>(`[data-testid="${testId}"]`)!;
        if (!el || !section.contains(el)) return null;
        const footer = section.querySelector<HTMLElement>('.priorities-footer')!;
        const r = el.getBoundingClientRect();
        const x = r.left + r.width / 2;
        const y = r.top + r.height / 2;
        const top = x >= 0 && y >= 0 && x < window.innerWidth && y < window.innerHeight ? document.elementFromPoint(x, y) : null;
        const label = el.closest('label') || (el.id ? document.querySelector(`label[for="${el.id}"]`) : null);
        const inert = getComputedStyle(el).pointerEvents === 'none';
        const reached = !!top && (el === top || el.contains(top) || (!!label && label.contains(top))
            || (inert && top.contains(el) && !footer.contains(top)));
        return {
            name: (el.getAttribute('aria-label') || label?.textContent || el.textContent || el.tagName).trim().slice(0, 40),
            inFooter: footer.contains(el),
            reached,
        };
    }, block);
}

/** Tabs from the editor's first field to its footer. Returns the fields walked and those a pointer does not reach. */
async function tabThroughEditor(page: Page, block: string): Promise<{fields: number; covered: string[]; inFooter: boolean}> {
    await page.getByRole('spinbutton', {name: SALARY}).focus();
    const covered: string[] = [];
    let fields = 0;
    let inFooter = false;
    for (let stop = 0; stop < 80 && !inFooter; stop += 1) {
        const at = await focusedControl(page, block);
        expect(at, `stop ${stop}: the focus is in the priorities block`).not.toBeNull();
        inFooter = at!.inFooter;
        if (!inFooter) {
            fields += 1;
            if (!at!.reached) covered.push(`stop ${stop}: ${at!.name}`);
            await page.keyboard.press('Tab');
        }
    }
    return {fields, covered, inFooter};
}

/** Whole inside the viewport and the block's scroller, with a pointer at its centre reaching it. */
async function wholeAndOnTop(target: Locator): Promise<boolean> {
    return target.first().evaluate((el) => {
        const r = el.getBoundingClientRect();
        const scroller = el.closest('.priorities-scroll')!.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return r.height > 0 && r.top >= Math.max(0, scroller.top) - 1 && r.bottom <= Math.min(window.innerHeight, scroller.bottom) + 1
            && !!hit && (el === hit || el.contains(hit));
    });
}

/** A pointer press at the control's centre, where it is: unlike click(), nothing is scrolled into view first. */
async function pointerPress(page: Page, target: Locator): Promise<void> {
    const box = (await target.boundingBox())!;
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
}

test.describe('collapsed priorities row in the assistant sidebar (issue #480)', () => {
    test.beforeEach(async ({page}) => {
        requireDjangoTier();
        await login(page, PREFS_USER, E2E_PASSWORD);
    });

    test.afterEach(async ({page}, testInfo) => {
        // Hooks still run for a test the tier guard skipped (the fixture tier has no API to reset).
        if (testInfo.status === 'skipped') return;
        await page.goto('/');
        await setPriorities(page, null);
    });

    for (const viewport of PANEL_VIEWPORTS) {
        test(`${viewport.name} ${viewport.width}x${viewport.height}: one row collapsed, bounded when expanded`, async ({page}) => {
            await page.setViewportSize({width: viewport.width, height: viewport.height});
            const narrow = viewport.width < 375;

            // Collapsed with 0, 3 and 10 priorities: one row, at most 72px, composer in view.
            for (const [patch, count] of [[null, 0], [THREE, 3], [TEN, 10]] as const) {
                await page.goto('/');
                expect(await setPriorities(page, patch)).toBe(count);
                const section = await openPanel(page);
                await expect(section.getByTestId('priority-chip')).toHaveCount(0);
                await expect(section.getByTestId('priorities-summary-toggle')).toHaveCount(count > 0 ? 1 : 0);
                await expect.poll(async () => (await measurePanel(page)).composerInViewport).toBe(true);
                const collapsed = await measurePanel(page);
                expect(collapsed.section).toBeLessThanOrEqual(COLLAPSED_MAX_PX);
                expect(collapsed.lines).toBeLessThanOrEqual(narrow ? 2 : 1);
                expect(collapsed.lines).toBeGreaterThanOrEqual(1);
                await expectNoHorizontalOverflow(page);
            }

            const section = page.getByTestId('priorities-sidebar');
            const bodyHeight = await page.locator('.assistant-panel-body').evaluate((el) => el.clientHeight);
            const toggle = section.getByTestId('priorities-summary-toggle');
            // Four requirements: the first is named whole and the rest are counted (a second one is named only while the
            // line stays short), so the row never cuts the value it names.
            await expect(section.getByTestId('priorities-summary')).toHaveText('Requires: $150,000, +3 \u00b7 6 preferences');
            expect(await section.getByTestId('priorities-summary').locator('.priorities-summary-lead')
                .evaluate((el) => el.scrollWidth <= el.clientWidth + 1), 'the named requirement is not cut').toBe(true);

            // Expanded with ten: every chip, an inner scroller, Reset in view, the row still on top.
            await toggle.click();
            await expect(section.getByTestId('priority-chip')).toHaveCount(10);
            const expanded = await expectBounded(page, bodyHeight);
            expect(expanded.scrollable).toBe(true);
            expect(expanded.toggleOnTop).toBe(true);
            await expectNoHorizontalOverflow(page);

            // Reset confirmation, then Keep returns focus to Reset with the list still expanded.
            await section.getByRole('button', {name: 'Reset priorities'}).click();
            await expect(section.getByRole('group', {name: 'Confirm reset'})).toBeVisible();
            await expectBounded(page, bodyHeight);
            await section.getByRole('button', {name: 'Keep priorities'}).click();
            await expect(section.getByRole('button', {name: 'Reset priorities'})).toBeFocused();
            await expect(toggle).toHaveAttribute('aria-expanded', 'true');

            // Editor, review and the applied summary keep the same bound.
            await section.getByRole('button', {name: 'Edit priorities'}).click();
            await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
            const editor = await expectBounded(page, bodyHeight);
            expect(editor.scrollable).toBe(true);
            await page.getByRole('spinbutton', {name: SALARY}).fill('165000');
            await page.getByRole('button', {name: 'Review changes'}).click();
            await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
            await expectBounded(page, bodyHeight);
            await page.getByRole('button', {name: 'Apply to account'}).click();
            await expect(page.getByRole('list', {name: 'Changed priorities'})).toBeVisible();
            await expectBounded(page, bodyHeight);
            await expectNoHorizontalOverflow(page);

            // Done returns to the row with focus on Edit, and the summary shows the saved value without a reload.
            await section.getByRole('button', {name: 'Done'}).click();
            await expect(section.getByRole('button', {name: 'Edit priorities'})).toBeFocused();
            await expect(section.getByTestId('priorities-summary')).toContainText('$165,000');
        });
    }


    for (const viewport of [{width: 320, height: 640}, {width: 375, height: 700}, {width: 320, height: 568}]) {
        test(`${viewport.width}x${viewport.height}: a failed Apply says so above the review's actions`, async ({page}) => {
            await page.setViewportSize(viewport);
            await page.goto('/');
            expect(await setPriorities(page, TEN)).toBe(10);
            const section = await openPanel(page);
            await section.getByRole('button', {name: 'Edit priorities'}).click();
            await page.getByRole('spinbutton', {name: SALARY}).fill('165000');
            await page.getByRole('button', {name: 'Review changes'}).click();
            await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
            const seen = (testId: string) => section.getByTestId(testId).evaluate((el) => {
                const r = el.getBoundingClientRect();
                const scroller = el.closest('.priorities-scroll')!.getBoundingClientRect();
                const actions = el.parentElement!.querySelector('.chat-actions')!.getBoundingClientRect();
                const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                return {
                    whole: r.top >= Math.max(0, scroller.top) - 1 && r.bottom <= Math.min(window.innerHeight, scroller.bottom, actions.top) + 1,
                    onTop: !!hit && el.contains(hit),
                };
            });

            // The server refuses: the reason shows whole, above Apply and Cancel, and is announced.
            await page.route('**/api/agent/preferences/apply/', (route) => route.fulfill({
                status: 500, contentType: 'application/json', body: '{"error":{"type":"server","message":"Could not apply your changes. Try again."}}',
            }));
            await page.getByRole('button', {name: 'Apply to account'}).click();
            const error = section.getByTestId('priorities-review-error');
            await expect(error).toHaveText(/Could not apply your changes/);
            await expect(error).toHaveAttribute('role', 'alert');
            await expect.poll(() => seen('priorities-review-error')).toEqual({whole: true, onTop: true});
            await expect(section.getByRole('button', {name: 'Apply to account'})).toBeEnabled();
            // The button that was pressed still holds the focus: Enter tries again.
            await expect(section.getByRole('button', {name: 'Apply to account'})).toBeFocused();

            // The priorities changed elsewhere: the reason for "Review latest" shows the same way.
            await page.unroute('**/api/agent/preferences/apply/');
            await page.route('**/api/agent/preferences/apply/', (route) => route.fulfill({
                status: 409, contentType: 'application/json', body: '{"error":{"type":"preference_stale","message":"stale"}}',
            }));
            await page.getByRole('button', {name: 'Apply to account'}).click();
            await expect(section.getByRole('button', {name: 'Review latest'})).toBeVisible();
            await expect(error).toHaveText(/Your priorities changed elsewhere/);
            await expect.poll(() => seen('priorities-review-error')).toEqual({whole: true, onTop: true});
            await page.unroute('**/api/agent/preferences/apply/');
        });
    }

    for (const viewport of [{width: 320, height: 640}, {width: 375, height: 700}, {width: 1280, height: 900}]) {
        test(`${viewport.width}x${viewport.height}: tabbing through the editor never puts the focused control under its pinned footer`, async ({page}) => {
            await page.setViewportSize(viewport);
            await page.goto('/');
            expect(await setPriorities(page, TEN)).toBe(10);
            const section = await openPanel(page);
            await section.getByRole('button', {name: 'Edit priorities'}).click();
            await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
            await settleLayout(page);
            const {fields, covered, inFooter} = await tabThroughEditor(page, 'priorities-sidebar');
            // Every field of the form was walked, down to Review changes in the footer.
            expect(inFooter, 'the walk ends in the editor\'s footer').toBe(true);
            expect(fields).toBeGreaterThanOrEqual(37);
            expect(covered, 'focused controls a pointer at their centre does not reach').toEqual([]);
        });
    }

    for (const viewport of [{width: 320, height: 568, more: true}, {width: 375, height: 553, more: true}, {width: 320, height: 640, more: false}]) {
        test(`${viewport.width}x${viewport.height}: the review leads to its second row of actions, and the applied card shows what was saved`, async ({page}) => {
            await page.setViewportSize({width: viewport.width, height: viewport.height});
            await page.goto('/');
            expect(await setPriorities(page, TEN)).toBe(10);
            const section = await openPanel(page);
            await section.getByRole('button', {name: 'Edit priorities'}).click();
            await page.getByRole('spinbutton', {name: SALARY}).fill('165000');
            await page.getByRole('button', {name: 'Review changes'}).click();
            await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
            await settleLayout(page);
            const actions = section.getByRole('group', {name: 'Review actions'});
            const more = actions.getByRole('button', {name: 'More options'});
            const edit = actions.getByRole('button', {name: 'Edit'});
            const searchOnly = actions.getByRole('button', {name: 'This search only'});
            if (viewport.more) {
                // The block shows one row of actions: Apply, Cancel and More, each whole and reachable; the change is not hidden.
                for (const button of [actions.getByRole('button', {name: 'Apply to account'}), actions.getByRole('button', {name: 'Cancel'}), more]) {
                    expect(await wholeAndOnTop(button), await button.textContent() || '').toBe(true);
                }
                expect(await wholeAndOnTop(section.locator('.priorities-review .pref-change-values'))).toBe(true);
                expect(await wholeAndOnTop(edit)).toBe(false);
                expect((await more.boundingBox())!.width).toBeGreaterThanOrEqual(44);
                // More leads to the second row: it is on screen, Edit holds the focus, and More has done its job.
                await pointerPress(page, more);
                await expect(edit).toBeFocused();
                await expect(more).toHaveCount(0);
            } else {
                // Both rows fit: nothing to lead to.
                await expect(more).toHaveCount(0);
            }
            await expect.poll(() => wholeAndOnTop(edit)).toBe(true);
            await expect.poll(() => wholeAndOnTop(searchOnly)).toBe(true);
            await expectNoHorizontalOverflow(page);

            // Applied: the field, its old and new value, and Undo and Done are all on screen and uncovered.
            await section.locator('.priorities-scroll').evaluate((el) => { el.scrollTop = 0; });
            await pointerPress(page, actions.getByRole('button', {name: 'Apply to account'}));
            const applied = section.getByTestId('priorities-applied');
            await expect(applied.getByRole('list', {name: 'Changed priorities'})).toBeVisible();
            await settleLayout(page);
            await expect(applied.getByRole('heading')).toBeFocused();
            for (const part of ['.priorities-heading', '.pref-change-path', '.pref-change-old', '.pref-change-new']) {
                expect(await wholeAndOnTop(applied.locator(part)), `applied card: ${part}`).toBe(true);
            }
            await expect(applied.locator('.pref-change-new')).toHaveText('$165,000');
            for (const name of ['Undo', 'Done']) {
                expect(await wholeAndOnTop(applied.getByRole('button', {name})), name).toBe(true);
            }
            await expectNoHorizontalOverflow(page);
        });
    }

    for (const viewport of [{width: 320, height: 568}, {width: 1280, height: 650}]) {
        for (const many of [false, true]) {
            test(`${viewport.width}x${viewport.height}: Tab through the review's actions, with ${many ? 'four changes' : 'one change'}, never lands on a button that is not on screen`, async ({page}) => {
                await page.setViewportSize(viewport);
                await page.goto('/');
                expect(await setPriorities(page, TEN)).toBe(10);
                const section = await openPanel(page);
                await section.getByRole('button', {name: 'Edit priorities'}).click();
                await page.getByRole('spinbutton', {name: SALARY}).fill('165000');
                if (many) {
                    await page.getByRole('checkbox', {name: 'Hybrid'}).uncheck();
                    await page.getByRole('spinbutton', {name: /equity/i}).fill('0.75');
                    await page.getByRole('spinbutton', {name: /office days/i}).fill('3');
                }
                await page.getByRole('button', {name: 'Review changes'}).click();
                await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
                await expect(page.getByRole('list', {name: 'Proposed changes'}).getByRole('listitem')).toHaveCount(many ? 4 : 1);
                await settleLayout(page);
                const actions = section.getByRole('group', {name: 'Review actions'});
                const apply = actions.getByRole('button', {name: 'Apply to account'});
                await apply.focus();
                // Where a button's ring and centre are: whole inside the block's scroller, and a pointer reaches it.
                const stops: string[] = [];
                const problems: string[] = [];
                for (let i = 0; i < 4; i += 1) {
                    await page.keyboard.press('Tab');
                    const name = await page.evaluate(() => (document.activeElement as HTMLElement).getAttribute('aria-label') || document.activeElement!.textContent || '');
                    stops.push(name.trim());
                    const visible = await page.evaluate(() => {
                        const el = document.activeElement as HTMLElement;
                        const r = el.getBoundingClientRect();
                        const scroller = el.closest('.priorities-scroll')!.getBoundingClientRect();
                        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                        const top = Math.max(r.top, scroller.top, 0);
                        const bottom = Math.min(r.bottom, scroller.bottom, window.innerHeight);
                        return {px: Math.round(Math.max(0, bottom - top)), of: Math.round(r.height), reached: !!hit && el.contains(hit)};
                    });
                    if (visible.px < visible.of - 1 || !visible.reached) problems.push(`${name.trim()}: ${visible.px} of ${visible.of} px on screen, reached by a pointer: ${visible.reached}`);
                }
                expect(stops.slice(-2)).toEqual(['Edit', 'This search only']);
                expect(problems, 'every stop of the walk is on screen and uncovered').toEqual([]);
            });
        }
    }

    for (const viewport of [{width: 1280, height: 650}, {width: 320, height: 568}]) {
        test(`${viewport.width}x${viewport.height}: a mouse press on Edit and on This search only acts while its row is only partly in view`, async ({page}) => {
            await page.setViewportSize(viewport);
            await page.goto('/');
            expect(await setPriorities(page, TEN)).toBe(10);
            const section = await openPanel(page);
            const actions = section.getByRole('group', {name: 'Review actions'});
            const toReview = async () => {
                await section.getByRole('button', {name: 'Edit priorities'}).click();
                await page.getByRole('spinbutton', {name: SALARY}).fill(String(160000 + Math.floor(Math.random() * 9000)));
                await page.getByRole('checkbox', {name: 'Hybrid'}).uncheck();
                await page.getByRole('spinbutton', {name: /equity/i}).fill('0.75');
                await page.getByRole('button', {name: 'Review changes'}).click();
                await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
                await settleLayout(page);
            };
            // The list is scrolled until `left` px of the end are still below, and the press is at the middle of what shows of the row.
            // Returns whether the row was partly in view (a position where it is whole or gone is skipped, and counted by the caller).
            const pressPartlyShown = async (name: string, left: number): Promise<boolean> => {
                const scroller = section.locator('.priorities-scroll');
                await section.locator('.priorities-review .priorities-heading').evaluate((el) => (el as HTMLElement).focus({preventScroll: true}));
                await scroller.evaluate((el, rest) => { el.scrollTop = el.scrollHeight - el.clientHeight - rest; }, left);
                await settleLayout(page);
                const target = actions.getByRole('button', {name, exact: true});
                const shown = await target.evaluate((el) => {
                    const r = el.getBoundingClientRect();
                    const edge = el.closest('.priorities-scroll')!.getBoundingClientRect();
                    const top = Math.max(r.top, edge.top, 0);
                    const bottom = Math.min(r.bottom, edge.bottom, window.innerHeight);
                    return {x: r.left + r.width / 2, y: (top + bottom) / 2, px: bottom - top, of: r.height};
                });
                if (shown.px < 4 || shown.px > shown.of - 2) return false;
                await page.mouse.move(shown.x, shown.y);
                await page.mouse.down();
                await page.waitForTimeout(60);
                await page.mouse.up();
                return true;
            };
            const rests = [70, 60, 50, 40, 30, 20];
            await toReview();
            let editPressed = 0;
            for (const left of rests) {
                if (!(await pressPartlyShown('Edit', left))) continue;
                editPressed += 1;
                await expect(page.getByRole('form', {name: 'Edit priorities'}), `Edit pressed with ${left}px of the list below the edge`).toBeVisible();
                await page.getByRole('button', {name: 'Review changes'}).click();
                await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
                await settleLayout(page);
            }
            expect(editPressed, 'positions where Edit was partly in view').toBeGreaterThanOrEqual(2);
            // This search only: the press reaches the server (answered by a stub, so nothing is applied).
            let sent = 0;
            await page.route('**/api/agent/preferences/apply/', (route) => {
                sent += 1;
                return route.fulfill({status: 500, contentType: 'application/json', body: '{"error":{"type":"server","message":"stub"}}'});
            });
            let searchPressed = 0;
            for (const left of rests) {
                sent = 0;
                if (!(await pressPartlyShown('This search only', left))) continue;
                searchPressed += 1;
                await expect.poll(() => sent, `This search only pressed with ${left}px of the list below the edge`).toBeGreaterThanOrEqual(1);
                await expect(section.getByTestId('priorities-review-error')).toBeVisible();
            }
            expect(searchPressed, 'positions where This search only was partly in view').toBeGreaterThanOrEqual(2);
            await page.unroute('**/api/agent/preferences/apply/');
        });
    }

    test('320x568: This search only pressed by keyboard keeps the focus while it runs and after it fails', async ({page}) => {
        await page.setViewportSize({width: 320, height: 568});
        await page.goto('/');
        expect(await setPriorities(page, TEN)).toBe(10);
        const section = await openPanel(page);
        await section.getByRole('button', {name: 'Edit priorities'}).click();
        await page.getByRole('spinbutton', {name: SALARY}).fill('165000');
        await page.getByRole('button', {name: 'Review changes'}).click();
        await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
        await settleLayout(page);
        let release: () => void = () => undefined;
        const held = new Promise<void>((resolve) => { release = resolve; });
        await page.route('**/api/agent/preferences/apply/', async (route) => {
            await held;
            await route.fulfill({status: 500, contentType: 'application/json', body: '{"error":{"type":"server","message":"Could not apply your changes. Try again."}}'});
        });
        const searchOnly = section.getByRole('group', {name: 'Review actions'}).getByRole('button', {name: 'This search only'});
        await searchOnly.focus();
        await searchOnly.press('Enter');
        // The request is under way: the button is dimmed for assistive technology and still holds the focus.
        await expect(searchOnly).toHaveAttribute('aria-disabled', 'true');
        await expect(searchOnly).toBeFocused();
        release();
        await expect(section.getByTestId('priorities-review-error')).toHaveText(/Could not apply your changes/);
        await expect(searchOnly).toBeFocused();
        expect(await page.evaluate(() => document.activeElement === document.body)).toBe(false);
        await settleLayout(page);
        expect(await wholeAndOnTop(section.getByTestId('priorities-review-error')), 'the reason is on screen').toBe(true);
        await page.unroute('**/api/agent/preferences/apply/');
    });

    for (const viewport of [{width: 320, height: 568}, {width: 360, height: 640}, {width: 375, height: 553}]) {
        test(`${viewport.width}x${viewport.height}: the result of "This search only" shows the job count whole`, async ({page}) => {
            await page.setViewportSize(viewport);
            await page.goto('/');
            expect(await setPriorities(page, TEN)).toBe(10);
            const section = await openPanel(page);
            await section.getByRole('button', {name: 'Edit priorities'}).click();
            await page.getByRole('spinbutton', {name: SALARY}).fill('165000');
            await page.getByRole('button', {name: 'Review changes'}).click();
            await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
            await settleLayout(page);
            const searchOnly = section.getByRole('group', {name: 'Review actions'}).getByRole('button', {name: 'This search only'});
            await searchOnly.focus();
            await searchOnly.press('Enter');
            const applied = section.getByTestId('priorities-applied');
            await expect(applied.getByRole('heading')).toContainText(/\d+\+? jobs? match/);
            await settleLayout(page);
            const text = applied.locator('.priorities-heading > span');
            expect(await wholeAndOnTop(text), 'the whole result is on screen').toBe(true);
            // Cut to a line it would be wider than the box that shows it.
            expect(await text.evaluate((el) => el.scrollWidth <= el.clientWidth + 1), 'the result is not cut off').toBe(true);
            expect(await text.evaluate((el) => (el.textContent || '').includes('The list below still reflects your saved priorities'))).toBe(true);
            await expectNoHorizontalOverflow(page);
        });
    }

    test('keyboard: Enter and Space toggle the row and focus stays on it; Cancel returns focus to Edit', async ({page}) => {
        await page.setViewportSize({width: 375, height: 700});
        await page.goto('/');
        await setPriorities(page, THREE);
        const section = await openPanel(page);
        const toggle = section.getByTestId('priorities-summary-toggle');
        const edit = section.getByRole('button', {name: 'Edit priorities'});

        // The toggle comes right before Edit in the tab order.
        await edit.focus();
        await page.keyboard.press('Shift+Tab');
        await expect(toggle).toBeFocused();
        await expect(toggle).toHaveAttribute('aria-expanded', 'false');
        const details = page.locator(`#${await toggle.getAttribute('aria-controls')}`);
        await expect(details).toBeHidden();

        await page.keyboard.press('Enter');
        await expect(toggle).toHaveAttribute('aria-expanded', 'true');
        await expect(details.getByTestId('priority-chip')).toHaveCount(3);
        // The key to the chip marks takes the summary's line in the row while the list is open.
        await expect(toggle.getByTestId('priority-chip-legend')).toBeVisible();
        await expect(section.getByTestId('priorities-summary')).toBeHidden();
        await expect(details.getByRole('button', {name: 'Reset priorities'})).toBeVisible();
        await expect(toggle).toBeFocused();

        await page.keyboard.press('Space');
        await expect(toggle).toHaveAttribute('aria-expanded', 'false');
        await expect(section.getByTestId('priority-chip')).toHaveCount(0);
        await expect(toggle).toBeFocused();

        await page.keyboard.press('Tab');
        await expect(edit).toBeFocused();
        await page.keyboard.press('Enter');
        await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
        await page.getByRole('button', {name: 'Cancel'}).click();
        await expect(edit).toBeFocused();
    });
});

// The open block and the chat under it (issue #480, visual round 2). The block is pinned above a
// scrolling panel only where that leaves the chat room; it never covers the conversation header, More,
// the jump pill or the composer, and the review step shows the change it asks to apply.
const WIDE_FONT_CSS = "body, button, input, textarea, select { font-family: 'DejaVu Sans', sans-serif !important; }";
const OPEN_BLOCK_VIEWPORTS = [
    ...PANEL_VIEWPORTS.map((viewport) => ({...viewport, wideFont: false})),
    {name: 'short sheet, panel scrolls', width: 320, height: 420, wideFont: false},
    // Phone heights between the layout sizes: a small iPhone with Safari's toolbars showing, and an older one.
    {name: 'small phone with browser bars', width: 375, height: 553, wideFont: false},
    {name: 'older small phone', width: 320, height: 568, wideFont: false},
    // The CI image's fallback font is wider than the local one: force it so the tight sizes are checked either way.
    {name: 'sheet, wide font', width: 375, height: 700, wideFont: true},
    {name: 'short sheet, wide font', width: 320, height: 640, wideFont: true},
];
const TWO_LINE_MESSAGE = 'What else should I compare between two employers before I decide where to apply this month?';
const FIVE_LINE_MESSAGE = 'What else should I compare between two employers before I decide where to apply this month, and which of the verified '
    + 'facts about pay, remote work and funding should I weigh most when two of them score the same on this list?';
const ASSISTANT_MESSAGE = 'article[aria-label="Assistant message"]';

/** The chat re-measures a frame after anything above it resizes, and may then scroll the panel: let that settle. */
async function settleLayout(page: Page): Promise<void> {
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.waitForTimeout(500);
}

interface ChatBars {
    // Bars a pointer cannot reach because the priorities block lies on them (centre, top edge, bottom edge).
    covered: string[];
    headerReachable: boolean;
    moreReachable: boolean;
    composerReachable: boolean;
    scrollOwner: string | null;
    pinned: boolean;
    share: number;
}

async function chatBars(page: Page): Promise<ChatBars> {
    return page.evaluate(() => {
        const section = document.querySelector<HTMLElement>('[data-testid="priorities-sidebar"]')!;
        const body = document.querySelector<HTMLElement>('.assistant-panel-body')!;
        const card = document.querySelector<HTMLElement>('[data-testid="job-search-chat"]')!;
        const bars: Record<string, Element | null> = {
            header: card.querySelector(':scope > .card-header'),
            more: card.querySelector('[data-testid="conversation-more"]'),
            title: card.querySelector(':scope > .card-header h2'),
            pill: card.querySelector('[data-testid="jump-to-latest"]'),
            composer: document.querySelector('[data-testid="assistant-composer"]'),
            send: card.querySelector('[aria-label="Send message"]'),
        };
        const points = (el: Element) => {
            const r = el.getBoundingClientRect();
            const x = r.left + r.width / 2;
            return [r.top + r.height / 2, r.top + 2, r.bottom - 2]
                .filter((y) => y >= 0 && y < window.innerHeight)
                .map((y) => document.elementFromPoint(x, y));
        };
        const covered = Object.entries(bars)
            .filter(([, el]) => el && points(el).some((hit) => !!hit && section.contains(hit)))
            .map(([name]) => name);
        const reachable = (el: Element | null) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const y = r.top + r.height / 2;
            if (y < 0 || y >= window.innerHeight) return false;
            const hit = document.elementFromPoint(r.left + r.width / 2, y);
            return !!hit && (el.contains(hit) || hit.contains(el));
        };
        return {
            covered,
            headerReachable: reachable(bars.header),
            moreReachable: reachable(bars.more),
            composerReachable: reachable(bars.composer),
            scrollOwner: card.getAttribute('data-scroll-owner'),
            pinned: section.hasAttribute('data-pinned'),
            share: section.offsetHeight / body.clientHeight,
        };
    });
}

/** The chat's bars are free of the block; with `typed` the panel has followed the composer, so all of them are reachable too. */
async function expectChatUsable(page: Page, state: string, typed: boolean): Promise<ChatBars> {
    await settleLayout(page);
    const bars = await chatBars(page);
    expect(bars.covered, `${state}: chat bars under the priorities block`).toEqual([]);
    expect(bars.share, `${state}: share of the panel body`).toBeLessThanOrEqual(EXPANDED_MAX_SHARE + 0.005);
    if (typed) {
        expect(bars.headerReachable, `${state}: conversation header reachable`).toBe(true);
        expect(bars.moreReachable, `${state}: More reachable`).toBe(true);
        expect(bars.composerReachable, `${state}: composer reachable`).toBe(true);
    }
    return bars;
}

/** A real conversation long enough to scroll: the seeded demo provider answers each turn. */
async function ensureConversation(page: Page, turns: number): Promise<void> {
    const replies = page.locator(ASSISTANT_MESSAGE);
    const questions = ['How are the company scores on this list worked out?', 'Which of these employers publish the most verified facts?',
        'What does the Stale badge next to a company mean?', 'How often is the evidence behind a ranking refreshed?'];
    for (let have = await replies.count(); have < turns; have += 1) {
        await page.getByTestId('assistant-composer').fill(questions[have % questions.length]);
        await page.getByRole('button', {name: 'Send message'}).click();
        await expect(replies.nth(have)).toBeVisible({timeout: 30_000});
    }
}

interface ReviewMeasure {
    valuesHeight: number;
    valuesVisible: number;
    pathVisible: number;
    applyVisible: boolean;
    cancelVisible: boolean;
}

/** How much of the proposed change a reader sees in the review: inside its scroller and above its pinned actions. */
async function measureReview(page: Page): Promise<ReviewMeasure> {
    return page.evaluate(() => {
        const section = document.querySelector<HTMLElement>('[data-testid="priorities-sidebar"]')!;
        const scroller = section.querySelector('.priorities-scroll')!.getBoundingClientRect();
        const actions = section.querySelector('.priorities-review > .chat-actions')!.getBoundingClientRect();
        const seen = (el: Element) => {
            const r = el.getBoundingClientRect();
            const top = Math.max(r.top, scroller.top, 0);
            const bottom = Math.min(r.bottom, scroller.bottom, actions.top, window.innerHeight);
            return Math.max(0, bottom - top);
        };
        const pressable = (name: string) => {
            const button = Array.from(section.querySelectorAll('.priorities-review > .chat-actions button'))
                .find((el) => el.textContent?.trim() === name);
            if (!button) return false;
            const r = button.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return r.top >= scroller.top - 1 && r.bottom <= scroller.bottom + 1 && !!hit && button.contains(hit);
        };
        const values = section.querySelector('.pref-change-values')!;
        return {
            valuesHeight: values.getBoundingClientRect().height,
            valuesVisible: seen(values),
            pathVisible: seen(section.querySelector('.pref-change-path')!),
            applyVisible: pressable('Apply to account'),
            cancelVisible: pressable('Cancel'),
        };
    });
}


// What a reader has in front of them after a step (issue #480, visual round 3): the block, the control that
// just took the focus, the chat's bars and their place in the conversation.
interface ReaderState {
    body: number;
    block: number;
    blockVisible: number;
    blockTopInView: boolean;
    open: boolean;
    pinned: boolean;
    owner: string | null;
    panelScrollRange: number;
    focus: {name: string; inBlock: boolean; inside: boolean; hit: boolean} | null;
    // Named controls of the open step: inside the viewport and reached by a pointer at their centre.
    controls: Record<string, boolean>;
    covered: string[];
    pill: boolean;
    // Composer band's top minus the last message's bottom: zero or more when the reader is at the end.
    lastToComposer: number | null;
}

async function readerState(page: Page): Promise<ReaderState> {
    return page.evaluate(() => {
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const section = document.querySelector<HTMLElement>('[data-testid="priorities-sidebar"]')!;
        const body = document.querySelector<HTMLElement>('.assistant-panel-body')!;
        const card = document.querySelector<HTMLElement>('[data-testid="job-search-chat"]')!;
        const sr = section.getBoundingClientRect();
        const br = body.getBoundingClientRect();
        const seen = (el: Element) => {
            const r = el.getBoundingClientRect();
            const inside = r.top >= -0.5 && r.left >= -0.5 && r.bottom <= vh + 0.5 && r.right <= vw + 0.5;
            const x = r.left + r.width / 2;
            const y = r.top + r.height / 2;
            const top = x >= 0 && y >= 0 && x < vw && y < vh ? document.elementFromPoint(x, y) : null;
            return {inside, hit: !!top && (el === top || el.contains(top))};
        };
        const active = document.activeElement;
        const focus = active && active !== document.body
            ? {name: (active.getAttribute('aria-label') || active.getAttribute('data-testid') || active.textContent || active.tagName).trim().slice(0, 40),
                inBlock: section.contains(active), ...seen(active)}
            : null;
        const controls: Record<string, boolean> = {};
        section.querySelectorAll('button').forEach((button) => {
            const state = seen(button);
            controls[(button.textContent || '').trim()] = state.inside && (state.hit || button.disabled);
        });
        const bars: Record<string, Element | null> = {
            header: card.querySelector('.card-header'),
            more: card.querySelector('[data-testid="conversation-more"]'),
            title: card.querySelector('.card-header h2'),
            pill: card.querySelector('[data-testid="jump-to-latest"]'),
            composer: document.querySelector('[data-testid="assistant-composer"]'),
            send: card.querySelector('[aria-label="Send message"]'),
        };
        const covered = Object.entries(bars).filter(([, el]) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            return [r.top + r.height / 2, r.top + 2, r.bottom - 2]
                .filter((y) => y >= 0 && y < vh)
                .some((y) => { const top = document.elementFromPoint(r.left + r.width / 2, y); return !!top && section.contains(top); });
        }).map(([name]) => name);
        const messages = card.querySelectorAll('[role="log"] article');
        const last = messages[messages.length - 1];
        const footer = card.querySelector('.chat-footer');
        return {
            body: body.clientHeight,
            block: section.offsetHeight,
            blockVisible: Math.round(Math.max(0, Math.min(sr.bottom, br.bottom, vh) - Math.max(sr.top, br.top, 0))),
            blockTopInView: sr.top >= br.top - 1 && sr.top < Math.min(br.bottom, vh),
            open: !!section.querySelector('.priorities-scroll'),
            pinned: section.hasAttribute('data-pinned'),
            owner: card.getAttribute('data-scroll-owner'),
            panelScrollRange: body.scrollHeight - body.clientHeight,
            focus,
            controls,
            covered,
            pill: !!bars.pill,
            lastToComposer: last && footer ? Math.round(footer.getBoundingClientRect().top - last.getBoundingClientRect().bottom) : null,
        };
    });
}

/** The chat re-measures over a few frames after the block changes, and the block then keeps the reader's place:
 *  wait those frames out, then until two looks a few frames apart agree. */
async function settledReader(page: Page): Promise<ReaderState> {
    const frames = (count: number) => page.evaluate((left) => new Promise<void>((resolve) => {
        const tick = () => { left -= 1; if (left > 0) requestAnimationFrame(tick); else resolve(); };
        requestAnimationFrame(tick);
    }), count);
    await frames(6);
    let previous = JSON.stringify(await readerState(page));
    for (let i = 0; i < 30; i += 1) {
        await frames(3);
        const now = JSON.stringify(await readerState(page));
        if (now === previous) break;
        previous = now;
    }
    return JSON.parse(previous) as ReaderState;
}

/** A reader following the conversation: at its end, whichever element scrolls. Where the panel is too short to
 *  show the row there, they scroll up to it. With `blur` nothing holds the focus (a row that does stays pinned). */
async function readerAtRow(page: Page, blur = true, toRow = true): Promise<ReaderState> {
    await page.evaluate(async ([drop, up]) => {
        if (drop && document.activeElement instanceof HTMLElement) document.activeElement.blur();
        const body = document.querySelector<HTMLElement>('.assistant-panel-body')!;
        const log = body.querySelector<HTMLElement>('[role="log"]')!;
        log.scrollTop = log.scrollHeight;
        body.scrollTop = body.scrollHeight;
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const row = document.querySelector<HTMLElement>('[data-testid="priorities-sidebar"] .priorities-summary-row')!;
        if (up && row.getBoundingClientRect().top < body.getBoundingClientRect().top) body.scrollTop = 0;
    }, [blur, toRow]);
    return settledReader(page);
}

/** What must hold after every step. `before` is the reader's state before the block was opened. */
function readerProblems(step: string, now: ReaderState, before: ReaderState | null, expected: string[]): string[] {
    const problems: string[] = [];
    if (now.covered.length) problems.push(`the block covers ${now.covered.join(', ')}`);
    // The editor's form takes the focus as a whole; its controls are checked by name instead.
    if (!now.focus) problems.push('nothing holds the focus');
    else if (!(step === 'edit' && now.focus.name === 'Edit priorities')) {
        if (!now.focus.inside) problems.push(`the focused control (${now.focus.name}) is off screen`);
        else if (!now.focus.hit) problems.push(`the focused control (${now.focus.name}) is covered`);
    }
    if (now.open && !now.blockTopInView) problems.push('the open block starts off screen');
    if (now.open && now.blockVisible < now.block - 1) problems.push(`only ${now.blockVisible} of ${now.block}px of the open block is on screen`);
    for (const name of expected) {
        if (!now.controls[name]) problems.push(`${name} is not on screen or is covered`);
    }
    if (now.open && now.owner === 'transcript' && now.panelScrollRange > 1) problems.push(`the panel scrolls ${now.panelScrollRange}px beside the transcript`);
    // No jump: a reader at the end of the conversation is still there while the block is pinned open and once it is closed.
    if (before && before.lastToComposer !== null && now.lastToComposer !== null && (!now.open || now.pinned)) {
        if (before.lastToComposer >= -1 && now.lastToComposer < -1) problems.push(`the conversation jumped: its last message is ${-now.lastToComposer}px below the composer band`);
        if (!now.open && !before.pill && now.pill) problems.push('"Jump to latest" appeared');
    }
    return problems;
}

const SWEEP_HEIGHTS = Array.from({length: 27}, (_, i) => 380 + i * 20);
const SWEEP_WIDTHS = [
    {width: 320, also: [568]},
    {width: 360, also: [560]},
    {width: 375, also: [553]},
    {width: 1024, also: []},
    {width: 1280, also: []},
];

test.describe('open priorities block over a conversation (issue #480)', () => {
    // These journeys send chat turns: the provider-outage pass answers none.
    test.skip(process.env.CRANK_E2E_PROVIDER_FAILURE === '1', 'needs assistant replies: not part of the provider-outage pass');

    test.beforeEach(async ({page}) => {
        requireDjangoTier();
        await login(page, PREFS_USER, E2E_PASSWORD);
    });

    test.afterEach(async ({page}, testInfo) => {
        if (testInfo.status === 'skipped') return;
        await page.goto('/');
        await setPriorities(page, null);
    });

    for (const viewport of OPEN_BLOCK_VIEWPORTS) {
        test(`${viewport.name} ${viewport.width}x${viewport.height}: the open block never covers the chat header, More or the composer`, async ({page}) => {
            test.setTimeout(120_000);
            await page.setViewportSize({width: viewport.width, height: viewport.height});
            await page.goto('/');
            expect(await setPriorities(page, TEN)).toBe(10);
            const section = await openPanel(page);
            if (viewport.wideFont) await page.addStyleTag({content: WIDE_FONT_CSS});
            await ensureConversation(page, 3);
            await settleLayout(page);
            const composer = page.getByTestId('assistant-composer');
            const replies = page.locator(ASSISTANT_MESSAGE);

            // A tap on the row, Enter on it, and Edit each open the block where the reader is looking.
            const row = section.getByTestId('priorities-summary-toggle');
            const atRest = await readerAtRow(page);
            await row.click();
            await expect(section.getByTestId('priority-chip')).toHaveCount(10);
            expect(readerProblems('row tapped', await settledReader(page), atRest, ['Reset priorities'])).toEqual([]);
            await page.keyboard.press('Enter');
            await expect(section.getByTestId('priority-chip')).toHaveCount(0);
            expect(readerProblems('row closed', await settledReader(page), atRest, [])).toEqual([]);
            await page.keyboard.press('Enter');
            await expect(section.getByTestId('priority-chip')).toHaveCount(10);
            expect(readerProblems('Enter on the row', await settledReader(page), atRest, ['Reset priorities'])).toEqual([]);
            await page.keyboard.press('Enter');
            await expect(section.getByTestId('priority-chip')).toHaveCount(0);

            // Editor open: at rest, with a two-line message typed, after the reply, and with keyboard focus on More.
            await section.getByRole('button', {name: 'Edit priorities'}).click();
            await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
            await expect(section.getByTestId('priorities-step-title')).toBeVisible();
            expect(readerProblems('edit', await settledReader(page), atRest, ['Review changes', 'Cancel'])).toEqual([]);
            await expectChatUsable(page, 'editor', false);
            await composer.fill(TWO_LINE_MESSAGE);
            await expectChatUsable(page, 'editor, two lines typed', true);
            const had = await replies.count();
            await page.getByRole('button', {name: 'Send message'}).click();
            await expect(replies.nth(had)).toBeVisible({timeout: 30_000});
            await expectChatUsable(page, 'editor, after the reply', true);
            await page.getByTestId('conversation-more').focus();
            const focused = await expectChatUsable(page, 'editor, More focused', true);
            expect(focused.moreReachable).toBe(true);

            // Review: the same, and the reader sees the change above the pinned Apply and Cancel.
            await page.getByRole('spinbutton', {name: SALARY}).fill('165000');
            await page.getByRole('button', {name: 'Review changes'}).click();
            await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
            await expectChatUsable(page, 'review', false);
            await composer.fill(TWO_LINE_MESSAGE);
            await expectChatUsable(page, 'review, two lines typed', true);
            await composer.fill('');
            await section.getByRole('heading', {name: 'Review your changes'}).scrollIntoViewIfNeeded();
            await settleLayout(page);
            const review = await measureReview(page);
            expect(review.applyVisible, 'Apply to account in view and not covered').toBe(true);
            expect(review.cancelVisible, 'Cancel in view and not covered').toBe(true);
            expect(review.pathVisible, 'visible height of the changed field name').toBeGreaterThanOrEqual(20);
            // At the four layout sizes the old and new values show whole; with the keyboard up most of the line does.
            const whole = viewport.height >= 640;
            expect(review.valuesVisible, 'visible height of the old and new values')
                .toBeGreaterThanOrEqual(whole ? review.valuesHeight - 1 : 12);
            await section.getByRole('group', {name: 'Review actions'}).getByRole('button', {name: 'Cancel'}).click();

            // Chips expanded: the same.
            const toggle = section.getByTestId('priorities-summary-toggle');
            await toggle.click();
            await expect(section.getByTestId('priority-chip')).toHaveCount(10);
            await expectChatUsable(page, 'chips expanded', false);
            await composer.fill(TWO_LINE_MESSAGE);
            await expectChatUsable(page, 'chips expanded, two lines typed', true);
            // One scroller: the list scrolls, the block itself does not, and nothing scrolls sideways.
            const ranges = await section.evaluate((el) => {
                const list = el.querySelector('.priorities-scroll')!;
                return {own: el.scrollHeight - el.clientHeight, sideways: list.scrollWidth - list.clientWidth};
            });
            expect(ranges.own).toBeLessThanOrEqual(1);
            expect(ranges.sideways).toBeLessThanOrEqual(1);
            await composer.fill('');
            await expectNoHorizontalOverflow(page);
        });
    }


    test('375x667 to 375x380 (a height-only shrink) with the editor open and a draft being typed: the composer stays on screen and the reader at the end', async ({page}) => {
        test.setTimeout(120_000);
        await page.setViewportSize({width: 375, height: 667});
        await page.goto('/');
        expect(await setPriorities(page, TEN)).toBe(10);
        const section = await openPanel(page);
        await ensureConversation(page, 6);
        await section.getByRole('button', {name: 'Edit priorities'}).click();
        await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
        const composer = page.getByTestId('assistant-composer');
        await composer.click();
        await page.keyboard.type('Does this one sponsor visas');
        const before = await readerAtRow(page, false, false);
        expect(before.lastToComposer, 'the reader is at the end of the conversation').toBeGreaterThanOrEqual(-1);
        await page.setViewportSize({width: 375, height: 380});
        const now = await settledReader(page);
        await expect(composer).toBeFocused();
        await expect(composer).toHaveValue('Does this one sponsor visas');
        expect(now.focus?.inside, 'the composer is on screen').toBe(true);
        expect(now.focus?.hit, 'the composer is uncovered').toBe(true);
        expect(now.lastToComposer, 'the reader is still at the end of the conversation').toBeGreaterThanOrEqual(-1);
        expect(now.pill, '"Jump to latest" is not showing').toBe(false);
        await page.setViewportSize({width: 375, height: 667});
        const back = await settledReader(page);
        expect(back.lastToComposer, 'and still there when the panel is tall again').toBeGreaterThanOrEqual(-1);
        await composer.fill('');
    });

    for (const {width, also} of SWEEP_WIDTHS) {
        test(`${width}px wide, every height from 380 to 900: what the reader opens is on screen, the chat's bars stay free and nothing jumps`, async ({page}) => {
            test.setTimeout(420_000);
            await page.setViewportSize({width, height: 900});
            await page.goto('/');
            expect(await setPriorities(page, TEN)).toBe(10);
            const section = await openPanel(page);
            await ensureConversation(page, 3);
            const toggle = section.getByTestId('priorities-summary-toggle');
            const edit = section.getByRole('button', {name: 'Edit priorities'});
            const chips = section.getByTestId('priority-chip');
            const failures: string[] = [];
            const pins: string[] = [];
            const heights = Array.from(new Set([...SWEEP_HEIGHTS, ...also])).sort((a, b) => a - b);

            for (const height of heights) {
                await page.setViewportSize({width, height});
                const check = async (step: string, before: ReaderState | null, expected: string[]) => {
                    const now = await settledReader(page);
                    for (const problem of readerProblems(step, now, before, expected)) failures.push(`${width}x${height} ${step}: ${problem}`);
                    return now;
                };
                const before = await readerAtRow(page);

                // Enter on the row opens the list and closes it again; the focus stays on the row.
                await toggle.focus();
                await page.keyboard.press('Enter');
                await expect(chips).toHaveCount(10);
                const opened = await check('Enter on the row', before, ['Reset priorities']);
                pins.push(`${height}:${opened.pinned ? 'pinned' : 'in the panel'}`);
                await page.keyboard.press('Enter');
                await expect(chips).toHaveCount(0);
                await check('Enter again', before, []);

                // Edit, Review changes, Cancel: each step's controls are on screen and Cancel returns the focus to Edit.
                await edit.click();
                await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
                await check('edit', before, ['Review changes', 'Cancel']);
                await page.getByRole('spinbutton', {name: SALARY}).fill('165000');
                await page.getByRole('button', {name: 'Review changes'}).click();
                await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
                await check('review', before, ['Apply to account', 'Cancel']);
                await section.getByRole('group', {name: 'Review actions'}).getByRole('button', {name: 'Cancel'}).click();
                await expect(edit).toBeFocused();
                const closed = await check('cancel', before, []);

                // The row holds the focus and is pinned over a scrolling panel: the reader returns to the end of the
                // conversation and uses the row from there. It stays on screen, and closing brings them back to the end.
                if (closed.owner !== 'panel' || !closed.pinned) continue;
                const held = await readerAtRow(page, false, false);
                for (const problem of readerProblems('row held at the end', held, null, [])) failures.push(`${width}x${height} row held at the end: ${problem}`);
                await page.keyboard.press('Shift+Tab');
                await page.keyboard.press('Enter');
                await expect(chips).toHaveCount(10);
                await check('Enter on the held row', held, ['Reset priorities']);
                await page.keyboard.press('Enter');
                await expect(chips).toHaveCount(0);
                await check('Enter again on the held row', held, []);
            }
            expect(failures).toEqual([]);
            // The block is pinned on the tall panels and scrolls with the panel on the shortest: both are exercised.
            expect(pins.some((pin) => pin.endsWith(':pinned'))).toBe(true);
            if (width < 768) expect(pins.some((pin) => pin.endsWith(':in the panel'))).toBe(true);
        });
    }

    test('closing the editor with a draft typed keeps the reader at the end of the conversation', async ({page}) => {
        test.setTimeout(120_000);
        for (const viewport of [{width: 375, height: 700}, {width: 320, height: 640}, {width: 1280, height: 900}]) {
            await page.setViewportSize(viewport);
            await page.goto('/');
            expect(await setPriorities(page, TEN)).toBe(10);
            const section = await openPanel(page);
            await ensureConversation(page, 3);
            const state = `${viewport.width}x${viewport.height}`;
            await page.getByTestId('assistant-composer').fill(TWO_LINE_MESSAGE);
            const before = await readerAtRow(page);
            expect(before.lastToComposer, `${state}: the last message is above the composer band`).toBeGreaterThanOrEqual(-1);

            // Cancel from the editor.
            await section.getByRole('button', {name: 'Edit priorities'}).click();
            await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
            expect(readerProblems('edit', await settledReader(page), before, ['Review changes', 'Cancel']), state).toEqual([]);
            await page.getByRole('form', {name: 'Edit priorities'}).getByRole('button', {name: 'Cancel'}).click();
            await expect(section.getByRole('button', {name: 'Edit priorities'})).toBeFocused();
            expect(readerProblems('cancel', await settledReader(page), before, []), state).toEqual([]);
            await expect(page.getByTestId('jump-to-latest')).toHaveCount(0);

            // Review, Apply, Done: the same.
            await section.getByRole('button', {name: 'Edit priorities'}).click();
            await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
            await settledReader(page);
            await page.getByRole('spinbutton', {name: SALARY}).fill('165000');
            await page.getByRole('button', {name: 'Review changes'}).click();
            await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
            // The chat re-measures and may scroll the panel after a step changes: press the next button once it has
            // settled, so Playwright's own scroll into view cannot take the reader up in between.
            await settledReader(page);
            await page.getByRole('button', {name: 'Apply to account'}).click();
            await expect(page.getByRole('list', {name: 'Changed priorities'})).toBeVisible();
            expect(readerProblems('applied', await settledReader(page), before, ['Done']), state).toEqual([]);
            await section.getByRole('button', {name: 'Done'}).click();
            await expect(section.getByRole('button', {name: 'Edit priorities'})).toBeFocused();
            expect(readerProblems('done', await settledReader(page), before, []), state).toEqual([]);
            await expect(page.getByTestId('jump-to-latest')).toHaveCount(0);
            await page.getByTestId('assistant-composer').fill('');
        }
    });

    test('320x700 with a long draft: a failed Apply is still on screen, with the focus on its button', async ({page}) => {
        test.setTimeout(120_000);
        await page.setViewportSize({width: 320, height: 700});
        await page.goto('/');
        expect(await setPriorities(page, TEN)).toBe(10);
        const section = await openPanel(page);
        await ensureConversation(page, 3);
        await page.getByTestId('assistant-composer').fill(FIVE_LINE_MESSAGE);
        await readerAtRow(page);
        await section.getByRole('button', {name: 'Edit priorities'}).click();
        await page.getByRole('spinbutton', {name: SALARY}).fill('165000');
        await page.getByRole('button', {name: 'Review changes'}).click();
        await expect(page.getByRole('list', {name: 'Proposed changes'})).toBeVisible();
        // The reader is at the end of the conversation with the review pinned above it.
        await readerAtRow(page, false, false);
        const apply = section.getByRole('button', {name: 'Apply to account'});
        const error = section.getByTestId('priorities-review-error');
        const onScreen = (target: Locator) => target.evaluate((el) => {
            const r = el.getBoundingClientRect();
            const panel = el.closest('.assistant-panel-body')!.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return r.top >= Math.max(0, panel.top) - 1 && r.bottom <= Math.min(window.innerHeight, panel.bottom) + 1 && !!hit && el.contains(hit);
        });
        expect(await onScreen(apply), 'Apply is on screen before it is pressed').toBe(true);

        // The server refuses. The message makes the block too tall to stay pinned: it is brought back, not left
        // at the top of the panel above the reader.
        await page.route('**/api/agent/preferences/apply/', (route) => route.fulfill({
            status: 500, contentType: 'application/json', body: '{"error":{"type":"server","message":"Could not apply your changes. Try again."}}',
        }));
        await pointerPress(page, apply);
        await expect(error).toHaveText(/Could not apply your changes/);
        await settledReader(page);
        expect(await onScreen(error), 'the reason is on screen and uncovered').toBe(true);
        expect(await onScreen(apply), 'Apply is on screen and uncovered').toBe(true);
        await expect(apply).toBeFocused();

        // The priorities changed elsewhere: the same for "Review latest".
        await page.unroute('**/api/agent/preferences/apply/');
        await page.route('**/api/agent/preferences/apply/', (route) => route.fulfill({
            status: 409, contentType: 'application/json', body: '{"error":{"type":"preference_stale","message":"stale"}}',
        }));
        await pointerPress(page, apply);
        const latest = section.getByRole('button', {name: 'Review latest'});
        await expect(latest).toBeFocused();
        await settledReader(page);
        expect(await onScreen(error), 'the stale reason is on screen and uncovered').toBe(true);
        expect(await onScreen(latest), 'Review latest is on screen and uncovered').toBe(true);
        await page.unroute('**/api/agent/preferences/apply/');

        // Closing returns a reader who was following the conversation to its end.
        await pointerPress(page, section.getByRole('group', {name: 'Review actions'}).getByRole('button', {name: 'Cancel'}));
        await expect(section.getByRole('button', {name: 'Edit priorities'})).toBeFocused();
        const after = await settledReader(page);
        expect(after.lastToComposer, 'the last message is above the composer band again').toBeGreaterThanOrEqual(-1);
        await expect(page.getByTestId('jump-to-latest')).toHaveCount(0);
        await page.getByTestId('assistant-composer').fill('');
    });

    test('the summary keeps its counts when the requirements are long, and a long value wraps inside its chip', async ({page}) => {
        await page.setViewportSize({width: 375, height: 700});
        await page.goto('/');
        await setPriorities(page, {set: {
            'roles.titles': ['Principal Distributed Systems Reliability Engineering Manager for Payments Infrastructure'],
            'compensation.minimum_salary': 150000, 'compensation.require_public_company': true,
            'work_location.max_in_office_days': 2, industry: ['fintech'], 'work_location.modes': ['remote'],
            importance: {'roles.titles': 1.0, 'compensation.minimum_salary': 1.0, 'compensation.require_public_company': 1.0,
                'work_location.max_in_office_days': 1.0},
        }});
        const section = await openPanel(page);
        const summary = section.getByTestId('priorities-summary');
        const tail = summary.locator('.priorities-summary-tail');
        await expect(summary).toHaveText('Requires: $150,000, +3 \u00b7 2 preferences');
        await expect(tail).toHaveText(/^\+3 \u00b7 2 preferences$/);
        // The counts are whole and inside the row; only the lead gives way.
        const fit = await summary.evaluate((el) => {
            const counts = el.querySelector<HTMLElement>('.priorities-summary-tail')!;
            const lead = el.querySelector<HTMLElement>('.priorities-summary-lead')!;
            return {
                countsWhole: counts.scrollWidth <= counts.clientWidth + 1,
                countsInRow: counts.getBoundingClientRect().right <= el.getBoundingClientRect().right + 1,
                leadWhole: lead.scrollWidth <= lead.clientWidth + 1,
            };
        });
        expect(fit).toEqual({countsWhole: true, countsInRow: true, leadWhole: true});
        expect((await measurePanel(page)).section).toBeLessThanOrEqual(COLLAPSED_MAX_PX);

        await section.getByTestId('priorities-summary-toggle').click();
        await expect(section.getByTestId('priority-chip')).toHaveCount(6);
        const list = await section.locator('.priorities-scroll').evaluate((el) => ({
            sideways: el.scrollWidth - el.clientWidth,
            widest: Math.max(...Array.from(el.querySelectorAll('[data-testid="priority-chip"]')).map((chip) => chip.getBoundingClientRect().width)),
            width: el.clientWidth,
        }));
        expect(list.sideways).toBeLessThanOrEqual(1);
        expect(list.widest).toBeLessThanOrEqual(list.width);
        await expectNoHorizontalOverflow(page);
    });

    for (const viewport of [{width: 320, height: 568}, {width: 375, height: 700}, {width: 768, height: 800}, {width: 834, height: 1194}]) {
        for (const [name, patch] of [
            ['two work arrangements', {set: {'work_location.modes': ['remote', 'hybrid'], industry: ['fintech'], importance: {'work_location.modes': 1.0}}}],
            ['three industries', {set: {industry: ['Financial technology', 'Developer tools', 'Healthcare'], 'work_location.countries': ['US'], importance: {industry: 1.0}}}],
        ] as const) {
            test(`${viewport.width}x${viewport.height}: a list of values as the first requirement (${name}) is cut with the lead, over nothing`, async ({page}) => {
                await page.setViewportSize(viewport);
                await page.goto('/');
                await setPriorities(page, patch);
                const section = await openPanel(page);
                const fit = await section.evaluate((sec) => {
                    const box = (el: Element) => el.getBoundingClientRect();
                    const toggle = sec.querySelector<HTMLElement>('[data-testid="priorities-summary-toggle"]')!;
                    const edit = sec.querySelector<HTMLElement>('.priorities-edit')!;
                    const lead = sec.querySelector<HTMLElement>('.priorities-summary-lead')!;
                    const tail = sec.querySelector<HTMLElement>('.priorities-summary-tail');
                    const keep = sec.querySelector<HTMLElement>('.priorities-summary-keep');
                    const visibleLead = lead.getBoundingClientRect();
                    // Painted over one another: the boxes share area (below 375px the counts sit under the lead, not beside it).
                    const overlap = (a: DOMRect, b: DOMRect) => Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left))
                        * Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
                    return {
                        text: sec.querySelector('[data-testid="priorities-summary"]')!.textContent,
                        leadWidth: Math.round(visibleLead.width),
                        // What is painted of the lead is its box: the text beyond it is cut with an ellipsis.
                        leadOverCounts: tail ? overlap(visibleLead, box(tail)) : 0,
                        leadOverKeep: keep && keep.textContent ? overlap(visibleLead, box(keep)) : 0,
                        countsOutsideToggle: tail ? Math.round(Math.max(0, box(tail).right - box(toggle).right)) : 0,
                        toggleOverEdit: overlap(box(toggle), box(edit)),
                        pastBlock: Math.round(Math.max(0, box(lead).right - box(sec).right, tail ? box(tail).right - box(sec).right : 0)),
                        sideways: sec.scrollWidth - sec.clientWidth,
                    };
                });
                expect(fit.text).toMatch(/^Requires: (Work arrangement: Remote, Hybrid|Industr(y|ies): Financial technology)/);
                expect(fit.leadWidth, 'the lead keeps some room').toBeGreaterThan(0);
                expect(fit.leadOverCounts).toBe(0);
                expect(fit.leadOverKeep).toBe(0);
                expect(fit.countsOutsideToggle).toBe(0);
                expect(fit.toggleOverEdit).toBe(0);
                expect(fit.pastBlock).toBe(0);
                expect(fit.sideways).toBeLessThanOrEqual(1);
                await expectNoHorizontalOverflow(page);
            });
        }
    }

    test('an expired session is said once, with the sign-in link and no retry', async ({page}) => {
        await page.setViewportSize({width: 320, height: 640});
        await page.route('**/api/agent/preferences/', (route) => route.fulfill({
            status: 401, contentType: 'application/json', body: '{"error":{"type":"auth_required"}}',
        }));
        await page.goto('/');
        const panel = page.getByTestId('assistant-panel');
        const opener = page.locator('[data-testid="assistant-launcher"], [data-testid="assistant-restore"]');
        await expect(panel.or(opener).first()).toBeVisible();
        await openIfClosed(page);
        const section = page.getByTestId('priorities-sidebar');
        await expect(section.getByTestId('priorities-session-expired')).toBeVisible();
        await expect(section.getByRole('link', {name: 'Sign in'})).toBeVisible();
        await expect(section.getByRole('alert')).toHaveCount(1);
        await expect(section.getByTestId('priorities-load-error')).toHaveCount(0);
        await page.unroute('**/api/agent/preferences/');
    });
});

test.describe('priorities signed out (issue #480)', () => {
    test.beforeEach(() => {
        requireDjangoTier();
    });

    test('the chat page offers one sign in instead of the editor', async ({page}) => {
        await page.goto('/chat/');
        await expect(page.getByTestId('job-search-sign-in-cta')).toBeVisible();
        await expect(page.getByTestId('priorities-main')).toContainText('Sign in to save your priorities.');
        await expect(page.getByTestId('priorities-sidebar')).toHaveCount(0);
        await expect(page.getByRole('button', {name: /Edit priorities/})).toHaveCount(0);
    });
});

test.describe('priorities during a provider outage @outage', () => {
    test.skip(
        process.env.CRANK_E2E_PROVIDER_FAILURE !== '1',
        'provider-outage pass only: run with CRANK_E2E_PROVIDER_FAILURE=1',
    );

    test('the editor applies and undoes without the assistant provider', async ({page}) => {
        requireDjangoTier();
        await login(page, PREFS_USER, E2E_PASSWORD);
        await applySalary(page, '111000');
        await page.getByRole('button', {name: 'Undo'}).click();
        await expect(page.getByText('Change undone.')).toBeVisible();
    });
});
