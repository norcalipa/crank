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
| Company row, 1280px and 1440px wide | at most **96px** tall |
| Companies fully visible above the fold, 1440x900, signed out | at least **7** |
| Company card, 375px wide | at most **180px** tall, no horizontal overflow |
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
| Row | `.organization-row` | `role="button"`, `tabindex="0"`, `aria-label="View details for <name>"`, `aria-describedby` points at a visually hidden summary that begins "Rank N." |
| Rank | `.organization-rank` | Decorative; the rank is spoken through the summary and the ordered list |
| Logo tile | `.organization-tile` | The company's first letter (no logo is stored) |
| Name | `.organization-name` | One line, ellipsis, full name in `title` |
| Meta line | `.organization-meta` | Funding round, office policy, rating coverage ("N of M rated") |
| Evidence chips | `.crank-chips` > `.crank-chip--verified \| stale \| unknown \| pending \| vesting` | From `static/js/evidence/EvidenceChips.tsx` |
| Score | `.organization-score` | `4.30 / 5` with `.organization-score-bar` |
| Action | `.organization-cta` | "View company"; the whole row opens the company |

Chip rules: every chip is a marker plus a word (`✓ 2 verified`, `! 1 stale`,
`? 5 unknown`), so status is never colour alone. When nothing is verified or
stale the row shows one muted dashed `? Not yet verified` chip instead of zero
counts. Chip colours reuse the evidence palette (`EvidenceBadge`) and meet 4.5:1
on the row surface.

Toolbar: one control row (search, "Sort by" preset select, the vesting filter as
a toggle chip), then one line holding the result count and the compact pager;
the second pager sits under the list. "How ranking works" is a small inline
disclosure link beside the subtitle (`.how-ranking-works--inline`), never a
full-width bar.

Every changed dynamic component implements and tests four states: populated
default, shape-matched loading skeleton (`.choices-skeleton` inside the meta
line while labels load), an explanatory empty state with a primary action
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
