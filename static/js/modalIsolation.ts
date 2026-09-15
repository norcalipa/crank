// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Background isolation for blocking dialogs (issue #464). While a blocking
// dialog is open, the application shell and the page content behind it are
// made inert and hidden from assistive technology, and document scrolling is
// locked. Keyboard focus, screen-reader virtual navigation, programmatic
// focus and wheel/touch scroll chaining therefore cannot reach the page
// behind the dialog — the issue's "prevent background interaction"
// acceptance criterion.
//
// The skip link deliberately stays outside the isolated roots: the shared
// layer contract keeps it above blocking dialogs so keyboard users always
// retain a page-level escape route while a dialog is open.
//
// The lock is reference-counted. Only one blocking dialog is ever active at a
// time (OrganizationList mutual exclusion), but the count keeps overlapping
// open/close transitions — one dialog replacing another in a single render
// commit — from releasing the isolation early or from wedging the page
// locked/inert after every dialog has closed.

const BACKGROUND_ROOT_SELECTOR = '.app-shell, main.app-content';

let referenceCount = 0;
let savedBodyOverflow = '';
let isolatedRoots: HTMLElement[] = [];

const findBackgroundRoots = (): HTMLElement[] =>
    Array.from(document.querySelectorAll<HTMLElement>(BACKGROUND_ROOT_SELECTOR));

// Marks the non-dialog page inert and locks document scrolling. Call once per
// dialog that becomes active.
export const lockBackground = (): void => {
    referenceCount += 1;
    if (referenceCount > 1) {
        // Already isolated by another open dialog.
        return;
    }
    isolatedRoots = findBackgroundRoots();
    for (const element of isolatedRoots) {
        element.setAttribute('inert', '');
        element.setAttribute('aria-hidden', 'true');
    }
    savedBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
};

// Releases one hold on the background isolation; the inert/aria-hidden
// attributes and the saved body overflow are only restored when the last
// dialog has closed. Extra calls (defensive double release) clamp at zero
// rather than corrupting the counter.
export const unlockBackground = (): void => {
    referenceCount = Math.max(0, referenceCount - 1);
    if (referenceCount > 0 || isolatedRoots.length === 0) {
        return;
    }
    for (const element of isolatedRoots) {
        element.removeAttribute('inert');
        element.removeAttribute('aria-hidden');
    }
    isolatedRoots = [];
    document.body.style.overflow = savedBodyOverflow;
};
