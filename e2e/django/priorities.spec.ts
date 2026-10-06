// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Editable priority chips with a review / apply / undo flow (issue #480).
// Uses the dedicated seeded account `e2e_prefs_user` because these journeys
// mutate the saved preference document.
import {expect, Page, test} from '@playwright/test';
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

/** Opens the assistant on the rankings page (the launcher, or the restore pill after a reload) and returns the row. */
async function openPanel(page: Page) {
    await page.goto('/');
    const panel = page.getByTestId('assistant-panel');
    const opener = page.locator('[data-testid="assistant-launcher"], [data-testid="assistant-restore"]');
    await expect(panel.or(opener).first()).toBeVisible();
    if (!(await panel.isVisible())) await opener.click();
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
        // The review pins its first action row (Apply and Cancel); Edit and This search only are one short
        // scroll below inside the same scroller, so the change itself stays in view on a phone.
        const actions = Array.from(section.querySelectorAll(
            '.priorities-footer button, .chat-actions button, .priorities-details .priorities-actions button',
        )).filter((el) => !el.matches('.priorities-review-compact .priorities-review-edit, .priorities-review-compact .priorities-review-search'));
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
            actionsInSection: actions.every((el) => {
                const r = el.getBoundingClientRect();
                return r.top >= box.top - 1 && r.bottom <= box.bottom + 1;
            }),
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
            await expect(section.getByTestId('priorities-summary')).toHaveText(/^Requires: \$150,000, .+, \+2 \u00b7 6 preferences$/);

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
    // The CI image's fallback font is wider than the local one: force it so the tight sizes are checked either way.
    {name: 'sheet, wide font', width: 375, height: 700, wideFont: true},
    {name: 'short sheet, wide font', width: 320, height: 640, wideFont: true},
];
const TWO_LINE_MESSAGE = 'What else should I compare between two employers before I decide where to apply this month?';
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

            // Editor open: at rest, with a two-line message typed, after the reply, and with keyboard focus on More.
            await section.getByRole('button', {name: 'Edit priorities'}).click();
            await expect(page.getByRole('form', {name: 'Edit priorities'})).toBeVisible();
            await expect(section.getByTestId('priorities-step-title')).toBeVisible();
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
        await expect(tail).toHaveText(/^\+2 \u00b7 2 preferences$/);
        // The counts are whole and inside the row; only the lead gives way.
        const fit = await summary.evaluate((el) => {
            const counts = el.querySelector<HTMLElement>('.priorities-summary-tail')!;
            return {
                countsWhole: counts.scrollWidth <= counts.clientWidth + 1,
                countsInRow: counts.getBoundingClientRect().right <= el.getBoundingClientRect().right + 1,
            };
        });
        expect(fit).toEqual({countsWhole: true, countsInRow: true});
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

    test('an expired session is said once, with the sign-in link and no retry', async ({page}) => {
        await page.setViewportSize({width: 320, height: 640});
        await page.route('**/api/agent/preferences/', (route) => route.fulfill({
            status: 401, contentType: 'application/json', body: '{"error":{"type":"auth_required"}}',
        }));
        await page.goto('/');
        const panel = page.getByTestId('assistant-panel');
        const opener = page.locator('[data-testid="assistant-launcher"], [data-testid="assistant-restore"]');
        await expect(panel.or(opener).first()).toBeVisible();
        if (!(await panel.isVisible())) await opener.click();
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
