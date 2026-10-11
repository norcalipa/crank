// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// One transcript scroller in the assistant panel (issue #483), on the real seeded page: the transcript is the
// only element that scrolls, it gets most of the panel body, and the follow rule still holds.
import {expect, Page, test} from '@playwright/test';
import {E2E_PASSWORD, login, requireDjangoTier} from './support';

const PREFS_USER = 'e2e_prefs_user';
const WIDE_FONT_CSS = "body, button, input, textarea, select { font-family: 'DejaVu Sans', sans-serif !important; }";
const ASSISTANT_MESSAGE = 'article[aria-label="Assistant message"]';
const TEN = {set: {
    'compensation.minimum_salary': 150000, 'compensation.minimum_total_compensation': 250000,
    'compensation.equity_minimum_percent': 0.5, 'compensation.require_public_company': true,
    'work_location.modes': ['remote', 'hybrid'], 'work_location.countries': ['US', 'CA'],
    'work_location.max_in_office_days': 2, industry: ['fintech'], funding_stage: ['series_b'],
    'roles.titles': ['Staff Engineer'],
    importance: {'compensation.minimum_salary': 1.0, 'work_location.modes': 1.0, 'work_location.max_in_office_days': 1.0},
}};

interface Gate {
    // Transcript clientHeight over panel body clientHeight, at least.
    share?: number;
    // Transcript clientHeight in px, at least.
    transcript?: number;
    // (Transcript + chat footer) over panel body, at least.
    withFooter?: number;
}

// Owner decision (plan, open question 1), the default: 0.60 at the three roomy layouts, 0.57 at 375x667, 0.50 at
// 320x640 (0.47 with the CI image's wider fallback font), and at least 190px at the short phone sizes.
const LAYOUT_SIZES: Array<{name: string; width: number; height: number; wideFont?: boolean; gate: Gate}> = [
    {name: 'docked', width: 1280, height: 900, gate: {share: 0.60, transcript: 539}},
    {name: 'drawer', width: 1024, height: 800, gate: {share: 0.60, transcript: 447}},
    {name: 'sheet', width: 375, height: 700, gate: {share: 0.60}},
    {name: 'phone', width: 375, height: 667, gate: {share: 0.57}},
    {name: 'short sheet', width: 320, height: 640, gate: {share: 0.50}},
    {name: 'short sheet, wide font', width: 320, height: 640, wideFont: true, gate: {share: 0.47}},
    {name: 'small phone with browser bars', width: 375, height: 553, gate: {transcript: 190, withFooter: 0.60}},
    {name: 'older small phone', width: 320, height: 568, gate: {transcript: 190, withFooter: 0.60}},
    {name: 'small android', width: 360, height: 560, gate: {transcript: 190, withFooter: 0.60}},
];
// A keyboard up, a phone on its side: the row stays on screen and the transcript keeps a few lines.
const SHORT_SIZES = [{width: 320, height: 420}, {width: 375, height: 380}];
const MIN_TRANSCRIPT_PX = 56;
const SWEEP_HEIGHTS = Array.from({length: 27}, (_, i) => 380 + i * 20);
const SWEEP_WIDTHS = [320, 360, 375, 1024, 1280];

/** Resets the saved priorities, then applies `patch` through the real API. The dev server's SQLite answers 409
 *  while another request holds the write lock, so a write is retried. */
async function setPriorities(page: Page, patch: object | null): Promise<void> {
    const failed = await page.evaluate(async (todo) => {
        const csrf = document.cookie.split('; ').find((c) => c.startsWith('csrftoken='))?.split('=')[1] || '';
        const headers = {'Content-Type': 'application/json', 'X-CSRFToken': decodeURIComponent(csrf)};
        const send = (url: string, body: unknown) => fetch(url, {method: 'POST', headers, body: JSON.stringify(body)});
        const attempt = async (): Promise<string> => {
            const current = await (await fetch('/api/agent/preferences/')).json();
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
        let result = await attempt();
        for (let retry = 0; retry < 3 && result.endsWith(' 409'); retry += 1) {
            await new Promise((resolve) => setTimeout(resolve, 200));
            result = await attempt();
        }
        return result;
    }, patch);
    expect(failed).toBe('');
}

/** Opens the assistant on the rankings page with the conversation loaded, the collapsed row showing and no notice. */
async function openChat(page: Page, wideFont = false, idle = true): Promise<void> {
    await page.goto('/');
    const panel = page.getByTestId('assistant-panel');
    const opener = page.locator('[data-testid="assistant-launcher"], [data-testid="assistant-restore"]');
    await expect(panel.or(opener).first()).toBeVisible();
    if (!(await panel.isVisible())) {
        // The page may restore an open panel between the check and the click, which removes the launcher.
        await opener.click({timeout: 3000}).catch(async () => { await expect(panel).toBeVisible(); });
    }
    await expect(page.getByTestId('assistant-composer')).toBeVisible();
    await expect(page.getByTestId('chat-loading')).toHaveCount(0);
    // The chat takes focus once when its history has loaded; let that pass. (`idle` is off where a test holds a request.)
    if (idle) await page.waitForLoadState('networkidle');
    await expect(page.getByTestId('priorities-sidebar').getByRole('button', {name: /Edit priorities|Add priorities/})).toBeVisible();
    if (wideFont) await page.addStyleTag({content: WIDE_FONT_CSS});
    // "Preferences updated." after the priorities were written through the API: a notice, not part of the layout under test.
    const banner = page.locator('#job-search-chat .chat-pref-alert .btn-close');
    if (await banner.count()) await banner.first().click();
    await expect(banner).toHaveCount(0);
}

/** A real conversation of at least `turns` replies: the seeded demo provider answers each turn. */
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

interface Layout {
    viewport: string;
    body: number;
    bodyScrollRange: number;
    bodyScrollTop: number;
    documentScrollTop: number;
    row: number;
    header: number;
    footer: number;
    transcript: number;
    transcriptScrollRange: number;
    transcriptScrollTop: number;
    fromEnd: number;
    composerInViewport: boolean;
    composerHit: boolean;
    moreHit: boolean;
    positions: string[];
    barsInOrder: boolean;
    pill: boolean;
    // Composer band's top minus the last message's bottom: zero or more when the last message is fully in view.
    lastToFooter: number | null;
    // Everything under the panel body with a vertical scroll range.
    scrollers: string[];
}

/** One reading of the panel, taken in a single evaluate so no number is paired with a later layout. */
function readLayout(page: Page): Promise<Layout> {
    return page.evaluate(() => {
        const body = document.querySelector<HTMLElement>('.assistant-panel-body')!;
        const card = body.querySelector<HTMLElement>('[data-testid="job-search-chat"]')!;
        const log = card.querySelector<HTMLElement>('[role="log"]')!;
        const header = card.querySelector<HTMLElement>('.card-header')!;
        const footer = card.querySelector<HTMLElement>('.chat-footer')!;
        const form = card.querySelector<HTMLElement>('form')!;
        const row = body.querySelector<HTMLElement>('[data-testid="priorities-sidebar"]');
        const hit = (el: Element) => {
            const r = el.getBoundingClientRect();
            const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return !!top && (el === top || el.contains(top));
        };
        const formRect = form.getBoundingClientRect();
        const stack = [header, log, footer];
        const articles = log.querySelectorAll('article');
        const last = articles[articles.length - 1];
        const describe = (el: Element) => el.getAttribute('data-testid') || el.getAttribute('role') || `${el.tagName.toLowerCase()}.${String(el.className).split(' ')[0]}`;
        return {
            viewport: `${window.innerWidth}x${window.innerHeight}`,
            body: body.clientHeight,
            bodyScrollRange: body.scrollHeight - body.clientHeight,
            bodyScrollTop: body.scrollTop,
            documentScrollTop: document.scrollingElement!.scrollTop,
            row: row ? row.offsetHeight : 0,
            header: header.offsetHeight,
            footer: footer.offsetHeight,
            transcript: log.clientHeight,
            transcriptScrollRange: log.scrollHeight - log.clientHeight,
            transcriptScrollTop: log.scrollTop,
            fromEnd: log.scrollHeight - log.clientHeight - log.scrollTop,
            composerInViewport: formRect.top >= -0.5 && formRect.bottom <= window.innerHeight + 0.5
                && formRect.left >= -0.5 && formRect.right <= window.innerWidth + 0.5,
            composerHit: hit(card.querySelector('[data-testid="assistant-composer"]')!),
            moreHit: hit(card.querySelector('[data-testid="conversation-more"]')!),
            positions: stack.map((el) => getComputedStyle(el).position),
            barsInOrder: stack.every((el, i) => i === 0 || stack[i - 1].getBoundingClientRect().bottom <= el.getBoundingClientRect().top + 0.5),
            pill: !!card.querySelector('[data-testid="jump-to-latest"]'),
            lastToFooter: last ? Math.round(footer.getBoundingClientRect().top - last.getBoundingClientRect().bottom) : null,
            scrollers: [body, ...Array.from(body.querySelectorAll<HTMLElement>('*'))]
                .filter((el) => el.scrollHeight - el.clientHeight > 1 && /auto|scroll/.test(getComputedStyle(el).overflowY))
                .map(describe),
        };
    });
}

/** Fonts loaded, then polled until two consecutive readings agree. */
async function settledLayout(page: Page): Promise<Layout> {
    await page.evaluate(() => document.fonts.ready.then(() => undefined));
    let previous = '';
    let reading = await readLayout(page);
    await expect.poll(async () => {
        reading = await readLayout(page);
        const now = JSON.stringify(reading);
        const same = now === previous;
        previous = now;
        return same;
    }, {message: 'the panel layout settles'}).toBe(true);
    return reading;
}

/** The transcript is the only scroller and the composer is on screen and reachable. Returns what is wrong. */
function scrollerProblems(state: string, now: Layout): string[] {
    const problems: string[] = [];
    if (now.bodyScrollRange > 1) problems.push(`the panel body scrolls ${now.bodyScrollRange}px`);
    if (now.bodyScrollTop !== 0) problems.push(`the panel body is scrolled ${now.bodyScrollTop}px`);
    if (!now.composerInViewport) problems.push('the composer is not fully inside the viewport');
    if (!now.composerHit) problems.push('the composer is covered');
    if (now.positions.includes('sticky')) problems.push('a chat bar is sticky');
    if (!now.barsInOrder) problems.push('a chat bar overlaps another');
    const others = now.scrollers.filter((name) => name !== 'log' && name !== 'conversation-menu');
    if (others.length) problems.push(`other elements scroll: ${others.join(', ')}`);
    return problems.map((problem) => `${now.viewport} ${state}: ${problem}`);
}

/** Holds the next chat turn at the network until `release` is called, so the pending state can be measured. */
async function holdNextTurn(page: Page): Promise<{reached: Promise<void>; release: () => Promise<void>}> {
    let open: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { open = resolve; });
    let arrived: () => void = () => undefined;
    const reached = new Promise<void>((resolve) => { arrived = resolve; });
    let passed: () => void = () => undefined;
    const sent = new Promise<void>((resolve) => { passed = resolve; });
    const pattern = /\/api\/agent\/conversations\/\d+\/$/;
    await page.route(pattern, async (route) => {
        if (route.request().method() !== 'POST') {
            await route.fallback();
            return;
        }
        arrived();
        await gate;
        await route.continue();
        passed();
    });
    // The handler finishes with the held request before it is removed.
    return {reached, release: async () => { open(); await sent; await page.unroute(pattern); }};
}

test.describe('one transcript scroller in the assistant panel (issue #483)', () => {
    // These journeys send chat turns: the provider-outage pass answers none.
    test.skip(process.env.CRANK_E2E_PROVIDER_FAILURE === '1', 'needs assistant replies: not part of the provider-outage pass');

    test.beforeEach(async ({page}) => {
        requireDjangoTier();
        await login(page, PREFS_USER, E2E_PASSWORD);
    });

    test.afterEach(async ({page}, testInfo) => {
        // Hooks still run for a test the tier guard skipped (the fixture tier has no API to reset).
        if (testInfo.status === 'skipped') return;
        await page.unrouteAll({behavior: 'ignoreErrors'});
        await page.goto('/');
        await setPriorities(page, null);
    });

    for (const size of LAYOUT_SIZES) {
        test(`${size.name} ${size.width}x${size.height}: the transcript is the only scroller in every chat state and gets its share of the panel`, async ({page}) => {
            test.setTimeout(180_000);
            await page.setViewportSize({width: size.width, height: size.height});
            await page.goto('/');
            await setPriorities(page, TEN);
            await openChat(page, size.wideFont);
            // Six or more messages.
            await ensureConversation(page, 3);
            const composer = page.getByTestId('assistant-composer');
            const log = page.getByRole('log');
            const replies = page.locator(ASSISTANT_MESSAGE);
            const problems: string[] = [];

            // Long transcript, the reader at its end: the share of the panel body the transcript gets.
            await log.evaluate((el) => { el.scrollTop = el.scrollHeight; });
            const atRest = await settledLayout(page);
            problems.push(...scrollerProblems('long transcript', atRest));
            expect(atRest.row, 'the collapsed priorities row is showing').toBeGreaterThan(0);
            expect(atRest.transcriptScrollRange, 'the conversation overflows the transcript').toBeGreaterThan(48);
            expect(atRest.fromEnd, 'the reader is at the end').toBeLessThanOrEqual(2);
            expect(atRest.pill).toBe(false);
            const share = atRest.transcript / atRest.body;
            test.info().annotations.push({type: 'transcript share', description: `${size.width}x${size.height}${size.wideFont ? ' wide font' : ''}: `
                + `${atRest.transcript}px of ${atRest.body}px = ${share.toFixed(3)} (row ${atRest.row}, header ${atRest.header}, footer ${atRest.footer})`});
            if (size.gate.share) expect(share, 'transcript share of the panel body').toBeGreaterThanOrEqual(size.gate.share);
            if (size.gate.transcript) expect(atRest.transcript, 'transcript height').toBeGreaterThanOrEqual(size.gate.transcript);
            if (size.gate.withFooter) {
                expect((atRest.transcript + atRest.footer) / atRest.body, 'transcript and composer band share of the panel body')
                    .toBeGreaterThanOrEqual(size.gate.withFooter);
            }

            // The wheel and the keys move the transcript, never the panel body or the page.
            const box = (await log.boundingBox())!;
            await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
            await page.mouse.wheel(0, -120);
            await expect.poll(async () => (await readLayout(page)).fromEnd, {message: 'the wheel scrolls the transcript'}).toBeGreaterThan(48);
            await expect(page.getByTestId('jump-to-latest')).toBeVisible();
            const wheeled = await settledLayout(page);
            problems.push(...scrollerProblems('after the wheel', wheeled));
            expect(wheeled.documentScrollTop).toBe(0);
            const pillHit = await page.getByTestId('jump-to-latest').evaluate((el) => {
                const r = el.getBoundingClientRect();
                const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                return !!top && (top === el || el.contains(top));
            });
            expect(pillHit, 'nothing covers the jump pill').toBe(true);
            await log.locator('article').last().evaluate((el) => (el as HTMLElement).focus({preventScroll: true}));
            await page.keyboard.press('End');
            await expect.poll(async () => (await readLayout(page)).fromEnd, {message: 'End scrolls the transcript to its end'}).toBeLessThanOrEqual(2);
            await expect(page.getByTestId('jump-to-latest')).toHaveCount(0);
            problems.push(...scrollerProblems('after End', await settledLayout(page)));

            // More open, then a confirmation open.
            await page.getByTestId('conversation-more').click();
            await expect(page.getByTestId('conversation-menu')).toBeVisible();
            problems.push(...scrollerProblems('More open', await settledLayout(page)));
            await page.getByTestId('conversation-new').click();
            await expect(page.getByTestId('confirm-panel')).toBeVisible();
            const confirming = await settledLayout(page);
            problems.push(...scrollerProblems('confirmation open', confirming).filter((problem) => !problem.endsWith('the composer is covered')));
            expect(confirming.transcript, 'the confirmation overlays the transcript without resizing it').toBe(atRest.transcript);
            await page.getByTestId('confirm-cancel').click();
            await expect(page.getByTestId('confirm-panel')).toHaveCount(0);

            // Pending, then the reply: a reader at the end ends with the reply fully in view.
            const had = await replies.count();
            const turn = await holdNextTurn(page);
            await composer.fill('Which of these employers are remote first?');
            await page.getByRole('button', {name: 'Send message'}).click();
            await turn.reached;
            await expect(page.getByTestId('pending-status')).toBeVisible();
            const pending = await settledLayout(page);
            problems.push(...scrollerProblems('pending', pending));
            expect(pending.fromEnd, 'pending: the reader follows their own message').toBeLessThanOrEqual(2);
            await turn.release();
            await expect(replies.nth(had)).toBeVisible({timeout: 30_000});
            await expect.poll(async () => (await readLayout(page)).fromEnd, {message: 'the reply is followed'}).toBeLessThanOrEqual(2);
            const replied = await settledLayout(page);
            problems.push(...scrollerProblems('after the reply', replied));
            expect(replied.lastToFooter, 'the reply ends above the composer band').toBeGreaterThanOrEqual(0);
            expect(replied.pill).toBe(false);

            // A failed turn: the card in the transcript and the error above the composer.
            await page.route(/\/api\/agent\/conversations\/\d+\/$/, (route) => (route.request().method() === 'POST'
                ? route.fulfill({status: 503, contentType: 'application/json',
                    body: JSON.stringify({error: {type: 'assistant_unavailable', message: 'The assistant is unavailable right now. Try again shortly.'}})})
                : route.fallback()));
            await composer.fill('And which of them are hiring now?');
            await page.getByRole('button', {name: 'Send message'}).click();
            const failedTurn = page.getByTestId('failed-turn');
            await expect(failedTurn).toBeVisible();
            await expect(page.getByTestId('chat-error')).toBeVisible();
            await expect.poll(async () => (await readLayout(page)).fromEnd, {message: 'the failed turn is followed'}).toBeLessThanOrEqual(2);
            const failed = await settledLayout(page);
            problems.push(...scrollerProblems('failed turn', failed));
            // The failed-turn card ends in view. From 640px of height a pointer reaches both of its actions, and
            // where the transcript is tall enough the whole card shows. At the short phone sizes (and with the
            // wide font) the error above the composer leaves the transcript about 100px until that notice can
            // collapse (the next part of #483): one action is in reach and the other a transcript scroll away.
            const card = await failedTurn.evaluate((el) => {
                const r = el.getBoundingClientRect();
                const bounds = el.closest('[role="log"]')!.getBoundingClientRect();
                const reachable = Array.from(el.querySelectorAll('button')).filter((button) => {
                    const box = button.getBoundingClientRect();
                    const top = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
                    const middle = box.top + box.height / 2;
                    return middle > bounds.top && middle < bounds.bottom && !!top && button.contains(top);
                }).length;
                return {height: r.height, room: bounds.height, topInside: r.top >= bounds.top - 1, bottomInside: r.bottom <= bounds.bottom + 1, reachable};
            });
            expect(card.bottomInside, 'the failed-turn card ends inside the transcript').toBe(true);
            expect(card.reachable, 'failed-turn actions a pointer reaches').toBeGreaterThanOrEqual(size.height >= 640 && !size.wideFont ? 2 : 1);
            // Room: the transcript's box less its padding and the message's own margin.
            if (card.height <= card.room - 48) expect(card.topInside, 'the failed-turn card is fully inside the transcript').toBe(true);
            await page.unroute(/\/api\/agent\/conversations\/\d+\/$/);

            // A status notice above the composer, on a fresh load.
            await page.route('**/api/agent/assistant-status/', (route) => route.fulfill({status: 200, contentType: 'application/json',
                body: JSON.stringify({state: 'temporarily_unavailable', actions: ['retry'], checked_at: '2026-10-10T00:00:00Z'})}));
            await openChat(page, size.wideFont);
            await expect(page.getByTestId('assistant-status-notice')).toBeVisible();
            await expect.poll(async () => (await readLayout(page)).fromEnd, {message: 'the conversation opens at its end'}).toBeLessThanOrEqual(2);
            const noticed = await settledLayout(page);
            problems.push(...scrollerProblems('status notice', noticed));
            expect(noticed.lastToFooter, 'the last message ends above the composer band').toBeGreaterThanOrEqual(0);

            expect(problems).toEqual([]);
        });
    }

    for (const size of SHORT_SIZES) {
        test(`${size.width}x${size.height}: the row stays on screen and the transcript keeps a few lines; long transcript, a draft and a pending turn`, async ({page}) => {
            test.setTimeout(120_000);
            await page.setViewportSize(size);
            await page.goto('/');
            await setPriorities(page, TEN);
            await openChat(page);
            await ensureConversation(page, 3);
            const problems: string[] = [];
            const rowOnScreen = () => page.getByTestId('priorities-summary-toggle').evaluate((el) => {
                const r = el.getBoundingClientRect();
                const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                return r.top >= 0 && r.bottom <= window.innerHeight && !!top && el.contains(top);
            });

            const atRest = await settledLayout(page);
            problems.push(...scrollerProblems('long transcript', atRest));
            expect(atRest.transcript, 'transcript height').toBeGreaterThanOrEqual(MIN_TRANSCRIPT_PX);
            expect(atRest.fromEnd).toBeLessThanOrEqual(2);
            expect(await rowOnScreen(), 'the priorities row is on screen and reachable').toBe(true);
            test.info().annotations.push({type: 'transcript share', description: `${size.width}x${size.height}: `
                + `${atRest.transcript}px of ${atRest.body}px (row ${atRest.row}, header ${atRest.header}, footer ${atRest.footer})`});
            // The disclaimer is hidden visually only: still in the accessibility tree.
            await expect(page.getByRole('note').filter({hasText: 'AI can be wrong'})).toHaveCount(1);
            const disclaimer = await page.locator('#job-search-chat .chat-disclaimer').evaluate((el) => {
                const style = getComputedStyle(el);
                return {display: style.display, visibility: style.visibility, width: el.getBoundingClientRect().width};
            });
            expect(disclaimer).toEqual({display: 'block', visibility: 'visible', width: 1});
            // The row's summary is clipped too and stays in the toggle's accessible name.
            await expect(page.getByTestId('priorities-summary-toggle')).toHaveAccessibleName(/Your priorities.*\d/);

            await page.getByTestId('assistant-composer').fill('Is it remote?');
            const drafted = await settledLayout(page);
            problems.push(...scrollerProblems('one-line draft', drafted));
            expect(drafted.transcript).toBeGreaterThanOrEqual(MIN_TRANSCRIPT_PX);

            const replies = page.locator(ASSISTANT_MESSAGE);
            const had = await replies.count();
            const turn = await holdNextTurn(page);
            await page.getByRole('button', {name: 'Send message'}).click();
            await turn.reached;
            await expect(page.getByTestId('pending-status')).toBeVisible();
            const pending = await settledLayout(page);
            problems.push(...scrollerProblems('pending', pending));
            expect(pending.transcript).toBeGreaterThanOrEqual(MIN_TRANSCRIPT_PX);
            await turn.release();
            await expect(replies.nth(had)).toBeVisible({timeout: 30_000});
            expect(problems).toEqual([]);
        });
    }

    for (const width of SWEEP_WIDTHS) {
        test(`${width}px wide, every height from 380 to 900: only the transcript scrolls with the row collapsed (long transcript, a draft, a pending turn)`, async ({page}) => {
            test.setTimeout(300_000);
            await page.setViewportSize({width, height: 900});
            await page.goto('/');
            await setPriorities(page, TEN);
            await openChat(page);
            await ensureConversation(page, 3);
            const problems: string[] = [];
            const sweep = async (state: string) => {
                for (const height of SWEEP_HEIGHTS) {
                    await page.setViewportSize({width, height});
                    await expect.poll(async () => (await readLayout(page)).viewport).toBe(`${width}x${height}`);
                    const now = await settledLayout(page);
                    problems.push(...scrollerProblems(state, now));
                    if (now.transcript < MIN_TRANSCRIPT_PX) problems.push(`${now.viewport} ${state}: the transcript is ${now.transcript}px tall`);
                    if (now.fromEnd > 2) problems.push(`${now.viewport} ${state}: the reader is ${now.fromEnd}px from the end`);
                }
            };
            await sweep('long transcript');
            await page.getByTestId('assistant-composer').fill('Is it remote?');
            await sweep('one-line draft');
            const replies = page.locator(ASSISTANT_MESSAGE);
            const had = await replies.count();
            const turn = await holdNextTurn(page);
            await page.getByRole('button', {name: 'Send message'}).click();
            await turn.reached;
            await expect(page.getByTestId('pending-status')).toBeVisible();
            await sweep('pending');
            await turn.release();
            await expect(replies.nth(had)).toBeVisible({timeout: 30_000});
            expect(problems).toEqual([]);
        });
    }

    test('more than 48px above the end nothing moves and the pill counts the replies; at the end the late availability notice is followed', async ({page}) => {
        test.setTimeout(120_000);
        await page.setViewportSize({width: 375, height: 667});
        await page.goto('/');
        await setPriorities(page, TEN);
        // The job-match status the chat reads after a reply without results: held, so it arrives after the reply was followed.
        let releaseStatus: () => void = () => undefined;
        const statusGate = new Promise<void>((resolve) => { releaseStatus = resolve; });
        let statusAsked: () => void = () => undefined;
        const statusRequested = new Promise<void>((resolve) => { statusAsked = resolve; });
        await page.route('**/api/job-matches/status/', async (route) => {
            statusAsked();
            await statusGate;
            await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
                state: 'no_matches', title: 'No job matches yet', message: 'Nothing matches your priorities yet. Try widening them.', actions: [],
            })});
        });
        await openChat(page, false, false);
        await ensureConversation(page, 3);
        const log = page.getByRole('log');
        const replies = page.locator(ASSISTANT_MESSAGE);

        // At the end: the notice that arrives under the last reply after it was followed ends in view.
        await statusRequested;
        await expect.poll(async () => (await readLayout(page)).fromEnd).toBeLessThanOrEqual(2);
        const beforeNotice = await settledLayout(page);
        releaseStatus();
        const notice = log.getByTestId('availability-notice');
        await expect(notice).toBeVisible();
        await expect.poll(async () => (await readLayout(page)).transcriptScrollRange, {message: 'the notice made the conversation longer'})
            .toBeGreaterThan(beforeNotice.transcriptScrollRange);
        await expect.poll(async () => (await readLayout(page)).fromEnd, {message: 'the late notice is followed'}).toBeLessThanOrEqual(2);
        const followed = await settledLayout(page);
        expect(scrollerProblems('late notice', followed)).toEqual([]);
        expect(followed.pill).toBe(false);

        // Away from the end: a reply does not move the reader, and the pill counts it.
        const had = await replies.count();
        const turn = await holdNextTurn(page);
        await page.getByTestId('assistant-composer').fill('What else should I know?');
        await page.getByRole('button', {name: 'Send message'}).click();
        await turn.reached;
        await expect(page.getByTestId('pending-status')).toBeVisible();
        await settledLayout(page);
        await log.evaluate((el) => { el.scrollTop = el.scrollHeight - el.clientHeight - 300; });
        await expect(page.getByTestId('jump-to-latest')).toBeVisible();
        const away = await settledLayout(page);
        expect(away.fromEnd).toBeGreaterThan(48);
        await turn.release();
        await expect(replies.nth(had)).toBeAttached({timeout: 30_000});
        await expect(page.getByTestId('jump-to-latest')).toHaveAccessibleName('Jump to latest message, 1 new message');
        const after = await settledLayout(page);
        expect(Math.abs(after.transcriptScrollTop - away.transcriptScrollTop), 'the reader was not moved').toBeLessThanOrEqual(1);
        expect(scrollerProblems('reply while away', after)).toEqual([]);
        // The pill takes them to the newest turn.
        await page.getByTestId('jump-to-latest').click();
        await expect.poll(async () => (await readLayout(page)).fromEnd).toBeLessThanOrEqual(2);
        await expect(page.getByTestId('jump-to-latest')).toHaveCount(0);
    });

    test('a fresh load with the drawer open ends at the latest message', async ({page}) => {
        test.setTimeout(120_000);
        await page.setViewportSize({width: 1024, height: 800});
        await page.goto('/');
        await setPriorities(page, TEN);
        await openChat(page);
        await ensureConversation(page, 3);
        // The panel is open: this load restores it and the conversation with it.
        await page.reload();
        await expect(page.getByTestId('assistant-panel')).toBeVisible();
        await expect(page.locator(ASSISTANT_MESSAGE).first()).toBeVisible();
        await page.waitForLoadState('networkidle');
        await expect.poll(async () => (await readLayout(page)).fromEnd, {message: 'the conversation opens at its end'}).toBeLessThanOrEqual(2);
        const loaded = await settledLayout(page);
        expect(loaded.lastToFooter, 'the last message ends at or above the composer band').toBeGreaterThanOrEqual(0);
        expect(loaded.pill).toBe(false);
        expect(loaded.bodyScrollRange).toBeLessThanOrEqual(1);
    });

    // Last: it starts a new conversation, so the long one the tests above share is gone after it.
    test('an empty conversation: only the transcript can scroll and "Jump to latest" never shows', async ({page}) => {
        test.setTimeout(180_000);
        await page.setViewportSize({width: 1280, height: 900});
        await page.goto('/');
        await setPriorities(page, TEN);
        await openChat(page);
        if (await page.locator('[role="log"] article').count()) {
            await page.getByTestId('conversation-more').click();
            await page.getByTestId('conversation-new').click();
            await page.getByTestId('confirm-action').click();
        }
        await expect(page.getByTestId('empty-history')).toBeVisible();
        const problems: string[] = [];
        for (const size of [...LAYOUT_SIZES.filter((entry) => !entry.wideFont), ...SHORT_SIZES]) {
            await page.setViewportSize({width: size.width, height: size.height});
            await openChat(page);
            await expect(page.getByTestId('empty-history')).toBeVisible();
            const now = await settledLayout(page);
            problems.push(...scrollerProblems('empty conversation', now));
            // A short log scrolls its empty state; that is not a reader away from the latest message.
            await page.getByRole('log').evaluate((el) => { el.scrollTop = el.scrollHeight; });
            await settledLayout(page);
            if (await page.getByTestId('jump-to-latest').count()) problems.push(`${now.viewport}: "Jump to latest" shows in an empty conversation`);
        }
        expect(problems).toEqual([]);
    });
});
