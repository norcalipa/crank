// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Result-position save/restore (issue #479). The position lives in
// `history.state.crankPosition` so Back/Forward restore the entry-specific
// scroll (bfcache-independent). Only `{scrollY, anchor}` is ever stored —
// never draft text, conversation ids, preferences or compensation values.

export interface ResultPosition {
    scrollY: number;
    anchor: string | null;
    // Viewport offset (px) of the anchor's top edge when saved.
    anchorOffset: number;
}

export interface PositionAnchor {
    id: string;
    offset: number;
}

const STATE_KEY = 'crankPosition';
const SCROLL_THROTTLE_MS = 200;

// `behavior: 'instant'` overrides Bootstrap's `:root{scroll-behavior:smooth}`
// so the position read straight after a restore is the settled one.
const INSTANT = 'instant' as ScrollBehavior;

export function saveResultPosition(anchor: string | null = null, anchorOffset = 0): void {
    try {
        const state = (window.history.state && typeof window.history.state === 'object')
            ? window.history.state
            : {};
        const position: ResultPosition = {scrollY: Math.round(window.scrollY),
            anchor,
            anchorOffset: Math.round(anchorOffset),
        };
        window.history.replaceState({...state, [STATE_KEY]: position}, '');
    } catch {
        // history unavailable; position simply is not restored.
    }
}

export function readResultPosition(): ResultPosition | null {
    const raw = window.history.state?.[STATE_KEY];
    if (!raw || typeof raw.scrollY !== 'number' || !Number.isFinite(raw.scrollY)) {
        return null;
    }
    return {
        scrollY: raw.scrollY,
        anchor: typeof raw.anchor === 'string' ? raw.anchor : null,
        anchorOffset: typeof raw.anchorOffset === 'number' && Number.isFinite(raw.anchorOffset)
            ? raw.anchorOffset
            : 0,
    };
}

// Restores the saved position: prefers putting the anchor back at its saved
// viewport offset, else falls back to scrollY. Returns whether anything was restored.
export function restoreResultPosition(getAnchor: (id: string) => HTMLElement | null): boolean {
    const position = readResultPosition();
    if (!position) {
        return false;
    }
    if (position.anchor) {
        const el = getAnchor(position.anchor);
        if (el) {
            const top = el.getBoundingClientRect().top + window.scrollY - position.anchorOffset;
            window.scrollTo({top, behavior: INSTANT});
            return true;
        }
    }
    window.scrollTo({top: position.scrollY, behavior: INSTANT});
    return true;
}

// Saves on throttled scroll and on pagehide; returns a teardown.
export function installPositionTracking(
    getAnchor: () => PositionAnchor | string | null,
): () => void {
    let timer: number | null = null;
    const save = (): void => {
        const anchor = getAnchor();
        if (typeof anchor === 'string' || anchor === null) {
            saveResultPosition(anchor);
        } else {
            saveResultPosition(anchor.id, anchor.offset);
        }
    };
    const onScroll = (): void => {
        if (timer !== null) {
            return;
        }
        timer = window.setTimeout(() => {
            timer = null;
            save();
        }, SCROLL_THROTTLE_MS);
    };
    window.addEventListener('scroll', onScroll, {passive: true});
    window.addEventListener('pagehide', save);
    return () => {
        window.removeEventListener('scroll', onScroll);
        window.removeEventListener('pagehide', save);
        if (timer !== null) {
            window.clearTimeout(timer);
        }
    };
}
