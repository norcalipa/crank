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
}

const STATE_KEY = 'crankPosition';
const SCROLL_THROTTLE_MS = 200;

export function saveResultPosition(anchor: string | null = null): void {
    try {
        const state = (window.history.state && typeof window.history.state === 'object')
            ? window.history.state
            : {};
        const position: ResultPosition = {scrollY: Math.round(window.scrollY), anchor};
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
    return {scrollY: raw.scrollY, anchor: typeof raw.anchor === 'string' ? raw.anchor : null};
}

// Restores the saved position: prefers scrolling the anchor into view, else
// falls back to scrollY. Returns whether anything was restored.
export function restoreResultPosition(getAnchor: (id: string) => HTMLElement | null): boolean {
    const position = readResultPosition();
    if (!position) {
        return false;
    }
    if (position.anchor) {
        const el = getAnchor(position.anchor);
        if (el) {
            el.scrollIntoView({block: 'center'});
            return true;
        }
    }
    window.scrollTo(0, position.scrollY);
    return true;
}

// Saves on throttled scroll and on pagehide; returns a teardown.
export function installPositionTracking(getAnchor: () => string | null): () => void {
    let timer: number | null = null;
    const save = (): void => saveResultPosition(getAnchor());
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
