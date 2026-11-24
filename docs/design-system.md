<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# CRank design system (UI gate reference)

This page is what UI pull requests are checked against. It was started in #617
(rankings and navigation) as part of the design-board work in #454.

## Design boards

The approved boards live in the #454 gallery:
<https://github.com/norcalipa/crank/issues/454#issuecomment-5658485685>

| Board | What it fixes |
| --- | --- |
| `desktop-workspace.png` | Page header, controls row, company cards, score with bar, type scale, spacing rhythm |
| `mobile-results-sheet.png` | Stacked cards at phone width, the assistant sheet |
| `evidence-comparison-jobs.png` | Chip and score styles, evidence and comparison surfaces, job rows |
| `preference-recovery-correction.png` | Priorities, recovery and correction surfaces |

The boards use fictional data and a top navigation. CRank keeps its left rail
and its data semantics; copy the boards' **visual language** (card surfaces,
logo tile, chip style, score with bar, type scale, spacing), not their fixture
content. The boards' card height is a style reference only: the density budgets
below win.

## Density budgets

Measured in the real app (Django dev server, `ENV=dev`, SQLite, seeded with 43
organizations), never in a fixture shell.

| Surface | Budget |
| --- | --- |
| Company row, 1280px and 1440px wide | **58–64px** target, at most **96px** hard, every row the same height |
| Companies fully visible above the fold, 1440x900, signed out | at least **7** |
| Company card, 375px wide | about **118px**, at most **180px** hard, no horizontal overflow |
| First card, 375x812, signed out | top at or above about y=280, at least **3** cards fully visible |
| Horizontal overflow, any page, any width | none |
| Touch targets | at least 44px tall (form controls, pager links, buttons) |

Folding rule: a fact that is identical on every row never gets a column. Fold
it into the meta line or into a chip.

## Shared row and chip components

Rankings rows are `ol.organization-list > li > article.organization-row`
(`static/js/OrganizationList.tsx`, `static/css/popup.css`). One layout serves
every width; the grid restacks on the width of `.organization-results` (a
container query), not on the viewport, so a docked assistant narrows it
correctly.

| Piece | Class | Notes |
| --- | --- | --- |
| Row | `.organization-row` | `role="button"`, `tabindex="0"`, `aria-label="View company: <name>"`, `aria-describedby` points at a visually hidden summary that begins "Rank N." |
| Rank | `.organization-rank` | Decorative; the rank is spoken through the summary and the ordered list |
| Logo tile | `.organization-tile` | The company's first letter (no logo is stored) |
| Name | `.organization-name` | One line on wide rows (a fixed row height), clamped to 2 lines on a phone card; full name in `title` |
| Meta line | `.organization-meta` | Funding round, office policy; "Rated on N of M" only when that row differs from the coverage most rows share, which is said once on the count line ("· most rated on N of M") |
| Evidence cell | `.organization-evidence` | Chips, then `.organization-checked` ("Checked Nov 20, 2026"; a never-checked company has no caption, its chip says "? Never checked") |
| Evidence chips | `.crank-chips` > `.crank-chip--verified \| stale \| unknown \| pending \| vesting \| more` | From `static/js/evidence/EvidenceChips.tsx`; never wrap and are never cut: a container query folds the 3rd and later chips into a `+N` chip (title names them). The vesting chip reads `◷ Vests < 1 yr`, with the full sentence in `title` |
| List header | `.organization-list-header` | "Company · Evidence · Score", 11px uppercase, on the row grid from 34rem of container width |
| Score | `.organization-score` | `4.30 / 5` with `.organization-score-bar` |
| Action | `.organization-cta` | "View company", quiet (transparent, `#93c5fd`) until the row is hovered or focused, then filled `#60a5fa`; the whole row opens the company |

Chip rules: every chip is a marker plus a word (`✓ 2 verified`, `! 1 stale`,
`? 5 unknown`), so status is never colour alone. When nothing is verified or
stale the row shows one muted chip instead of zero counts: `? Never checked` if the
company was never re-checked (and then no "Checked" caption), otherwise
`? Not yet verified`. Chips are filled tint pills with no border (`color-mix(currentColor 16%)`).
`✓` only ever means verified: the first-vesting chip is neutral with a `◷`
marker. Chip colours reuse the evidence palette (`EvidenceBadge`) and meet 4.5:1
on the row surface.

Palette: dark theme is one blue on navy. `--bs-body-bg #0c1620`, `--bs-tertiary-bg
#132131` (cards, rows, with a `#24364b` row border via `--crank-row-border`), `--bs-secondary-bg #142235`, `--bs-border-color #1f2d3d`,
`--bs-primary #60a5fa` (dark `#0b1220` text on it), links `#93c5fd`. The score bar
is flat `#94a3b8`. Do not add other blues.
Contrast on the `#132131` row surface, measured on the rendered rankings (round 3,
every text style, 58 at 1440px): lowest 6.23:1 (pending-review chip `#c4b5fd`),
then 7.36:1 and up for the other chips, captions and the rank; all above 4.5:1.

Headings: every page H1 is 1.75rem/700 (Rankings, Help, Privacy, company, Job Search);
`.readable-page-container` h2 is 1.375rem/600 and h3 1.125rem/600, so the H1 is always the
largest. On a company page the H1 is visually hidden and the Assistant pill sits in
`.company-view-header` (no empty strip); Job Search uses `.page-title-row` (h1 + slot).
The panel title reads "Assistant"; the phone sheet hides its x while "Back to results" shows.

The Assistant entry never floats over content: wide screens put it in the page's
title row (`[data-assistant-slot="title"]`), phones in the top bar beside Menu
(`[data-assistant-slot="topbar"]`). A page with neither slot gets a 44px
icon-only circle named "Assistant" and reserves matching bottom padding.

Toolbar: one control row (search, "Sort by" preset select, the vesting filter as
a toggle chip), then one line holding the result count and the compact pager
(both pagers right-aligned); a label-loading or error message takes its own full-width line after
them, so the pager never moves. "Page N of M" is not shown whenever a pager is visible (the pager names the page). Below 576px the top pager is
hidden, the subtitle is hidden, the count line is one line: "most rated on N of M" gives way to the loading words or the "Updated" mark and is dropped for a count of 100 or more (`.organization-results-count--long`), which has no room for it; the vesting toggle reads "Vests < 1 yr" (the full words are hidden below 576px, so the accessible name is always the visible text), the preset select shows a short label ("Culture-focused", not "... Algorithm"), "Sort by" is visually hidden (still the select's name) and shares a line
with the vesting toggle, and "Suggest a company" moves under the list. "How ranking works" is a small inline
disclosure link beside the subtitle (`.how-ranking-works--inline`), never a
full-width bar.

Every changed dynamic component implements and tests four states: populated
default, shape-matched loading skeleton (`.choices-skeleton` inside the meta
line while labels load), an explanatory empty state with one action
("Clear search and filters"), and inline error feedback (the label error with
"Retry labels").

## Navigation

Signed out, the rail and the mobile menu each show exactly one entry in the
footer: **Sign in** (`/accounts/login/`). Google sign-in is offered on the login
page itself. Signed-in behaviour (Admin, account label, Logout) is unchanged.

## Pages every UI PR must screenshot

Render the real app from the PR head and from `origin/main` (a temporary second
worktree), signed out and signed in where the page differs, and compare:

1. Company Rankings at 1440x900, 1280x800 and 375x812, signed out and signed in
2. Rankings states: loading (stall the label APIs), empty search, error (fail
   the label APIs), at 1440 and 375
3. Company view (open a row) at 1440 and 375
4. Job Search (`/chat/`) at 1440 and 375
5. Help (`/help/`) at 1440 and 375
6. The mobile navigation menu, signed out and signed in

Save the images, the harness script and a `measurements.json` (every company row
height, count of rows fully above the fold, horizontal overflow, nav entries
signed out) with the PR evidence.

## Accessibility floor

Keyboard operation with a visible focus ring (`3px solid var(--bs-warning)`),
programmatic labels, a semantic ordered list with rank announced, text contrast
of at least 4.5:1, `prefers-reduced-motion` honoured for every transition and
animation, and non-colour status cues.
