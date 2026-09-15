// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Unit tests for the blocking-dialog background isolation module (issue
// #464, review r2): the isolation must cover every non-dialog focus target
// — the application shell, the main content area and the modal-external
// skip link — and must lock the actual document scroller (the root element
// carries the viewport overflow under the stylesheet's `html, body {
// overflow-x: hidden }` rule), not body alone. Lock/unlock are
// reference-counted and restore every inline value they touch.
import '@testing-library/jest-dom';
import {lockBackground, unlockBackground} from './modalIsolation';

describe('modalIsolation (issue #464 review r2)', () => {
    let shell: HTMLElement;
    let content: HTMLElement;
    let skipLink: HTMLAnchorElement;
    let roots: HTMLElement[];

    beforeEach(() => {
        // Stand up the page structure the isolation module targets: the
        // application shell, the main content area and the modal-external
        // skip link (a direct child of <body>, outside the other roots).
        shell = document.createElement('aside');
        shell.className = 'app-shell';
        document.body.appendChild(shell);
        content = document.createElement('main');
        content.className = 'app-content';
        document.body.appendChild(content);
        skipLink = document.createElement('a');
        skipLink.className = 'skip-to-content';
        skipLink.href = '#main-content';
        skipLink.textContent = 'Skip to content';
        document.body.appendChild(skipLink);
        roots = [shell, content, skipLink];

        document.documentElement.style.overflow = '';
        document.body.style.overflow = '';
        // Assigning '' to padding sub-properties is a no-op in jsdom's
        // cssstyle; remove the property instead.
        document.body.style.removeProperty('padding-right');
    });

    afterEach(() => {
        // Defensive: never leak isolation into other test files.
        lockBackground();
        unlockBackground();
        unlockBackground();
        for (const root of roots) {
            root.remove();
        }
        document.documentElement.style.overflow = '';
        document.body.style.overflow = '';
        document.body.style.removeProperty('padding-right');
    });

    test('inerts every non-dialog focus target, including the modal-external skip link', () => {
        lockBackground();

        for (const root of roots) {
            expect(root).toHaveAttribute('inert');
            expect(root).toHaveAttribute('aria-hidden', 'true');
        }
        // Nothing else in the page picked up the attributes.
        expect(document.querySelectorAll('[inert]').length).toBe(roots.length);

        unlockBackground();
        for (const root of roots) {
            expect(root).not.toHaveAttribute('inert');
            expect(root).not.toHaveAttribute('aria-hidden');
        }
    });

    test('locks the actual document scroller: root element overflow, not body alone', () => {
        lockBackground();

        // The stylesheet gives the root element (html) the viewport overflow
        // via `html, body { overflow-x: hidden }`; locking only body leaves
        // the root viewport scroller free (review r2 finding). Both must be
        // locked while a dialog is open.
        expect(document.documentElement.style.overflow).toBe('hidden');
        expect(document.body.style.overflow).toBe('hidden');

        unlockBackground();
        expect(document.documentElement.style.overflow).toBe('');
        expect(document.body.style.overflow).toBe('');
    });

    test('restores pre-existing inline overflow values instead of clearing them', () => {
        document.documentElement.style.overflow = 'scroll';
        document.body.style.overflow = 'auto';

        lockBackground();
        expect(document.documentElement.style.overflow).toBe('hidden');
        expect(document.body.style.overflow).toBe('hidden');

        unlockBackground();
        expect(document.documentElement.style.overflow).toBe('scroll');
        expect(document.body.style.overflow).toBe('auto');
    });

    test('is reference-counted: a second holder keeps the background isolated', () => {
        // Simulates the details→suggest mutual-exclusion handoff: the next
        // dialog claims the isolation before the previous one releases it.
        lockBackground();
        lockBackground();

        expect(document.documentElement.style.overflow).toBe('hidden');
        for (const root of roots) {
            expect(root).toHaveAttribute('inert');
        }

        unlockBackground();
        // Still held by the second dialog: no early release, no leak.
        expect(document.documentElement.style.overflow).toBe('hidden');
        expect(document.body.style.overflow).toBe('hidden');
        for (const root of roots) {
            expect(root).toHaveAttribute('inert');
            expect(root).toHaveAttribute('aria-hidden', 'true');
        }

        unlockBackground();
        expect(document.documentElement.style.overflow).toBe('');
        expect(document.body.style.overflow).toBe('');
        for (const root of roots) {
            expect(root).not.toHaveAttribute('inert');
            expect(root).not.toHaveAttribute('aria-hidden');
        }
    });

    test('extra unlock calls clamp at zero instead of corrupting later cycles', () => {
        unlockBackground();
        unlockBackground();

        // The clamped counter must not wedge the module: a fresh lock/unlock
        // cycle still isolates and releases cleanly.
        lockBackground();
        expect(document.documentElement.style.overflow).toBe('hidden');
        expect(shell).toHaveAttribute('inert');

        unlockBackground();
        expect(document.documentElement.style.overflow).toBe('');
        expect(shell).not.toHaveAttribute('inert');
    });

    test('reapplies isolation on the next dialog after a full release', () => {
        lockBackground();
        unlockBackground();
        lockBackground();

        for (const root of roots) {
            expect(root).toHaveAttribute('inert');
            expect(root).toHaveAttribute('aria-hidden', 'true');
        }
        expect(document.documentElement.style.overflow).toBe('hidden');

        unlockBackground();
        for (const root of roots) {
            expect(root).not.toHaveAttribute('inert');
        }
    });

    describe('viewport scrollbar compensation', () => {
        const defineProp = (target: object, name: string, value: number): void => {
            Object.defineProperty(target, name, {value, configurable: true});
        };

        test('compensates the disappearing scrollbar and reverts on unlock', () => {
            // jsdom reports clientWidth 0 (no layout), so the module skips
            // compensation there; fake a real browser's geometry.
            const root = document.documentElement;
            const realClientWidth = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth')!;
            defineProp(root, 'clientWidth', 1000);
            defineProp(window, 'innerWidth', 1015);

            try {
                lockBackground();
                // innerWidth (1015) - clientWidth (1000) = 15px of scrollbar.
                expect(document.body.style.paddingRight).toBe('15px');

                unlockBackground();
                expect(document.body.style.paddingRight).toBe('');
            } finally {
                delete (root as {clientWidth?: number}).clientWidth;
                delete (window as unknown as {innerWidth?: number}).innerWidth;
                Object.defineProperty(root, 'clientWidth', realClientWidth);
            }
        });

        test('adds the scrollbar width to a pre-existing inline padding and restores it', () => {
            const root = document.documentElement;
            const realClientWidth = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth')!;
            document.body.style.paddingRight = '10px';
            defineProp(root, 'clientWidth', 990);
            defineProp(window, 'innerWidth', 1015);

            try {
                lockBackground();
                // 1015 - 990 = 25px scrollbar on top of the saved 10px.
                expect(document.body.style.paddingRight).toBe('35px');

                unlockBackground();
                expect(document.body.style.paddingRight).toBe('10px');
            } finally {
                delete (root as {clientWidth?: number}).clientWidth;
                delete (window as unknown as {innerWidth?: number}).innerWidth;
                Object.defineProperty(root, 'clientWidth', realClientWidth);
                document.body.style.removeProperty('padding-right');
            }
        });

        test('skips compensation when there is no scrollbar (zero width)', () => {
            const root = document.documentElement;
            const realClientWidth = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth')!;
            defineProp(root, 'clientWidth', 1015);
            defineProp(window, 'innerWidth', 1015);

            try {
                lockBackground();
                expect(document.body.style.paddingRight).toBe('');
                expect(document.documentElement.style.overflow).toBe('hidden');

                unlockBackground();
                expect(document.body.style.paddingRight).toBe('');
            } finally {
                delete (root as {clientWidth?: number}).clientWidth;
                delete (window as unknown as {innerWidth?: number}).innerWidth;
                Object.defineProperty(root, 'clientWidth', realClientWidth);
            }
        });
    });
});
