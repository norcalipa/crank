// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Background isolation for blocking dialogs (issue #464). While a blocking
// dialog is open, everything outside the dialog — the application shell
// (navigation rail, mobile topbar/drawer chrome), the main content area and
// the modal-external skip link — is marked inert and hidden from assistive
// technology, and document scrolling is locked. Keyboard focus,
// screen-reader virtual navigation, programmatic focus and wheel/touch
// scroll chaining therefore cannot reach the page behind the dialog — the
// issue's "prevent background interaction" acceptance criterion.
//
// The skip link is contained too (review r2 finding): it lives outside the
// isolated roots and used to remain a focusable page-level target while a
// dialog was open, leaving `skip.focus()` and virtual navigation a way out
// of the supposedly blocking dialog. While a dialog is open there is no
// "content to skip to" that should receive focus, so the link is inerted
// with the rest of the background and is released when the dialog closes.
//
// Scroll locking targets the actual viewport scroller. The stylesheet sets
// `html, body { overflow-x: hidden; }`, so the root element (html) — not
// body — carries the viewport's overflow and body's own overflow no longer
// propagates to the viewport (CSS overflow propagation: a non-visible root
// value wins, and body's overflow applies to body itself). Locking only
// body therefore leaves the root viewport scroller free, and wheel/touch
// input over the backdrop still moves the document (review r2 finding).
// The lock sets `overflow: hidden` on BOTH the root element and body,
// compensates the disappearing viewport scrollbar on body's padding so
// background content does not shift horizontally, and saves/restores every
// inline value it touches.
//
// SSR/no-JS degradation: every attribute and inline style is applied
// imperatively from effect/commit hooks at runtime, never rendered into
// markup. Server-rendered pages and clients without JavaScript never
// carry `inert`/`aria-hidden` on the shell or a scroll lock — they degrade
// to the plain, unisolated page.
//
// The lock is reference-counted. Only one blocking dialog is ever active at
// a time (OrganizationList mutual exclusion), but the count keeps the
// details→suggest handoff — one dialog replacing another across a single
// render commit — from releasing the isolation early or from wedging the
// page locked/inert after every dialog has closed. The background of the
// topmost (and only) dialog is therefore always isolated, never the
// dialog itself: both dialogs render through portals on <body>, outside
// the isolated roots.

const BACKGROUND_ROOT_SELECTOR = '.app-shell, main.app-content, .skip-to-content';

let referenceCount = 0;
// Whether the isolation is currently applied. Tracked separately from the
// count so the release cannot be skipped (leaking inline overflow styles)
// when a dialog opens on a page whose shell/content/skip-link elements are
// absent, and so extra unlock calls before any lock cannot restore
// never-saved values over legitimate inline styles.
let isLocked = false;
let savedRootOverflow = '';
let savedBodyOverflow = '';
// Only meaningful together with `bodyPaddingCompensated`: an empty string is
// a legitimate saved value ("no inline padding"), so the flag — not the
// string — decides whether the compensation must be reverted.
let savedBodyPaddingRight = '';
let bodyPaddingCompensated = false;
let isolatedRoots: HTMLElement[] = [];

const findBackgroundRoots = (): HTMLElement[] =>
    Array.from(document.querySelectorAll<HTMLElement>(BACKGROUND_ROOT_SELECTOR));

// Compensates the viewport scrollbar that the root overflow lock removes, so
// background content keeps its position instead of shifting by the scrollbar
// width. `documentElement.clientWidth` is 0 in jsdom, which usefully makes the
// guard a no-op there (real browsers always report a positive value).
const scrollbarCompensation = (): number => {
    const root = document.documentElement;
    if (!root || root.clientWidth <= 0) {
        return 0;
    }
    return Math.max(0, window.innerWidth - root.clientWidth);
};

// Marks the non-dialog page inert and locks the actual document scroller.
// Call once per dialog that becomes active.
export const lockBackground = (): void => {
    referenceCount += 1;
    if (isLocked) {
        // Already isolated by another open dialog.
        return;
    }
    isLocked = true;
    isolatedRoots = findBackgroundRoots();
    for (const element of isolatedRoots) {
        element.setAttribute('inert', '');
        element.setAttribute('aria-hidden', 'true');
    }
    const root = document.documentElement;
    const compensation = scrollbarCompensation();
    if (compensation > 0) {
        savedBodyPaddingRight = document.body.style.paddingRight;
        bodyPaddingCompensated = true;
        const currentPadding = parseFloat(getComputedStyle(document.body).paddingRight) || 0;
        document.body.style.paddingRight = `${currentPadding + compensation}px`;
    }
    // The root element carries the viewport overflow under the stylesheet's
    // `html, body { overflow-x: hidden }` rule — locking body alone leaves the
    // viewport scroller free (review r2). Lock both.
    savedRootOverflow = root.style.overflow;
    root.style.overflow = 'hidden';
    savedBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
};

// Releases one hold on the background isolation; the inert/aria-hidden
// attributes and the saved overflow/padding values are only restored when
// the last dialog has closed. Extra calls (defensive double release) clamp
// at zero rather than corrupting the counter or restoring values that were
// never saved.
export const unlockBackground = (): void => {
    referenceCount = Math.max(0, referenceCount - 1);
    if (referenceCount > 0 || !isLocked) {
        return;
    }
    isLocked = false;
    for (const element of isolatedRoots) {
        element.removeAttribute('inert');
        element.removeAttribute('aria-hidden');
    }
    isolatedRoots = [];
    document.body.style.overflow = savedBodyOverflow;
    document.documentElement.style.overflow = savedRootOverflow;
    if (bodyPaddingCompensated) {
        // Assigning the empty string to a style sub-property is a no-op in
        // some DOM implementations (seen in jsdom's cssstyle), so an absent
        // saved value must be removed explicitly rather than assigned.
        if (savedBodyPaddingRight === '') {
            document.body.style.removeProperty('padding-right');
        } else {
            document.body.style.paddingRight = savedBodyPaddingRight;
        }
        bodyPaddingCompensated = false;
    }
};
