// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

const NEAR_BOTTOM_PX = 48;
// Our own scrollTo/scrollTop writes (a smooth scroll fires scroll events for a
// while) must not read as the reader scrolling away.
const PROGRAMMATIC_WINDOW_MS = 1000;
const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ']);

// Exactly one element scrolls the conversation: the transcript, at every
// size. The assistant panel's body only scrolls as a last resort, when the
// chat's own bars do not fit in it (issue #483).
export function useTranscriptScroll({messagesLength, assistantCount, pending, loading, contentKey = '', footerKey}: {
    messagesLength: number;
    assistantCount: number;
    pending: boolean;
    loading: boolean;
    // Changes when content arrives in the transcript without a new message
    // (a notice under the last reply, a failed-turn card).
    contentKey?: string;
    // Changes when the footer is mounted again (it is gone while the init error shows).
    footerKey?: unknown;
}) {
    const historyRef = React.useRef<HTMLDivElement>(null);
    const nearBottomRef = React.useRef(true);
    const seenAssistantRef = React.useRef(assistantCount);
    const programmaticUntilRef = React.useRef(0);
    const emptyRef = React.useRef(messagesLength === 0);
    emptyRef.current = messagesLength === 0;
    const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);
    const [unreadCount, setUnreadCount] = React.useState(0);

    const prefersReducedMotion = (): boolean => (
        typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );

    const getScroller = (): HTMLElement | null => historyRef.current;

    // The window also ends as soon as a scroll event reaches `target`, so a
    // later scroll with no input behind it (find-in-page, a screen-reader
    // cursor) is not mistaken for one of our own writes.
    const programmaticTargetRef = React.useRef<number | null>(null);
    // A write that will not move the scroller fires no scroll event, so it must
    // not arm a window that nothing would ever close.
    const markProgrammatic = (scroller?: HTMLElement | null, target?: number) => {
        const resolved = target ?? (scroller ? scroller.scrollHeight - scroller.clientHeight : null);
        if (scroller && resolved !== null && Math.abs(scroller.scrollTop - resolved) <= 2) {
            programmaticUntilRef.current = 0;
            programmaticTargetRef.current = null;
            return;
        }
        programmaticUntilRef.current = Date.now() + PROGRAMMATIC_WINDOW_MS;
        programmaticTargetRef.current = resolved;
    };

    const isNearBottom = (): boolean => {
        const history = historyRef.current;
        if (!history) return true;
        return history.scrollHeight - history.scrollTop - history.clientHeight <= NEAR_BOTTOM_PX;
    };

    const settleAtBottom = () => {
        nearBottomRef.current = true;
        seenAssistantRef.current = assistantCount;
        setShowJumpToLatest(false);
        setUnreadCount(0);
    };

    const scrollToLatest = (behavior: ScrollBehavior = prefersReducedMotion() ? 'auto' : 'smooth') => {
        const scroller = getScroller();
        if (!scroller) return;
        markProgrammatic(scroller);
        if (typeof scroller.scrollTo === 'function') {
            scroller.scrollTo({top: scroller.scrollHeight, behavior});
        } else {
            scroller.scrollTop = scroller.scrollHeight;
        }
        settleAtBottom();
    };

    // Sending is explicit intent to follow the conversation. The new bubble is
    // not in the DOM yet, so clear the stale "new messages" state now and
    // scroll in a layout effect once React has committed it (issue #483).
    const followNextAppendRef = React.useRef(false);
    const followNextAppend = () => {
        followNextAppendRef.current = true;
        settleAtBottom();
    };

    React.useLayoutEffect(() => {
        if (!followNextAppendRef.current) return;
        followNextAppendRef.current = false;
        const scroller = getScroller();
        if (!scroller) return;
        markProgrammatic(scroller);
        scroller.scrollTop = scroller.scrollHeight;
    }, [messagesLength, pending]);

    // Keep the latest content visible only while the reader is already at the bottom.
    React.useEffect(() => {
        const scroller = getScroller();
        if (!scroller) return undefined;
        // "Following" follows the actual scroll position: any scroll that is not
        // one of our own writes and ends away from the bottom (wheel, keys, a
        // Tab to an older link, find-in-page, a screen reader) stops it.
        // Layout shifts clamp or move scrollTop without leaving the bottom, so
        // they still read as following (issue #483). Input that scrolls cancels
        // the programmatic window so it can interrupt a smooth scroll.
        const cancelProgrammatic = (e?: Event) => {
            // Only focus inside the transcript can scroll the reader away. Focus in
            // the chat's bars (composer, More, the pill) is already in view, and
            // cancelling on it would turn a load-time write's own late scroll event
            // into "the reader scrolled away".
            if (e?.type === 'focusin' && !historyRef.current?.contains(e.target as Node)) return;
            if (e?.type === 'keydown') {
                if (!SCROLL_KEYS.has((e as KeyboardEvent).key)) return;
                if ((e.target as HTMLElement).closest('textarea, input, select, button, [contenteditable="true"]')) return;
            }
            programmaticUntilRef.current = 0;
        };
        const handleScroll = () => {
            // An empty conversation has nothing to jump to, however short the log.
            const atBottom = emptyRef.current || isNearBottom();
            const ownScroll = Date.now() < programmaticUntilRef.current;
            const target = programmaticTargetRef.current;
            if (ownScroll && target !== null && Math.abs(scroller.scrollTop - target) <= 2) {
                programmaticUntilRef.current = 0;
            }
            const nearBottom = atBottom || (ownScroll && nearBottomRef.current);
            nearBottomRef.current = nearBottom;
            setShowJumpToLatest(!nearBottom);
            if (nearBottom) {
                seenAssistantRef.current = assistantCount;
                setUnreadCount(0);
            }
        };
        const inputEvents = ['wheel', 'touchmove', 'keydown', 'pointerdown', 'focusin'] as const;
        inputEvents.forEach((name) => scroller.addEventListener(name, cancelProgrammatic, {passive: true}));
        scroller.addEventListener('scroll', handleScroll, {passive: true});
        return () => {
            scroller.removeEventListener('scroll', handleScroll);
            inputEvents.forEach((name) => scroller.removeEventListener(name, cancelProgrammatic));
        };
    }, [assistantCount]);

    // The transcript's box changes size when the priorities block above the
    // chat opens or closes, or the composer grows. A reader who is following
    // the conversation stays at the end; someone reading older messages is
    // left where they are.
    React.useEffect(() => {
        const history = historyRef.current;
        if (!history || typeof ResizeObserver === 'undefined') return undefined;
        let lastHeight = history.clientHeight;
        const observer = new ResizeObserver(() => {
            const height = history.clientHeight;
            if (height === lastHeight) return;
            lastHeight = height;
            if (nearBottomRef.current && history.scrollHeight > 0) scrollToLatest('auto');
        });
        observer.observe(history);
        return () => observer.disconnect();
    }, [assistantCount]);

    // Initial history, optimistic turns, replies, and the pending indicator all append
    // content to the same viewport. Do not interrupt someone reading older messages.
    const wasLoadingRef = React.useRef(loading);
    React.useEffect(() => {
        // The first scroll after history loads jumps; a smooth scroll through a
        // long history is cut short as the content settles and strands the reader.
        const justLoaded = wasLoadingRef.current && !loading;
        wasLoadingRef.current = loading;
        // Empty history (visual review #472 round 5): never auto-scroll — the
        // empty state stays anchored at the top of the log so its lead is
        // visible on first open, even on the shortest sheet viewports.
        // Auto-scroll resumes once a conversation exists or content is added.
        if (messagesLength === 0) {
            // A fresh conversation starts out following its first reply.
            nearBottomRef.current = true;
            seenAssistantRef.current = assistantCount;
            setShowJumpToLatest(false);
            setUnreadCount(0);
            return;
        }
        if (loading || !nearBottomRef.current) {
            if (!nearBottomRef.current) {
                setShowJumpToLatest(true);
                // Only replies that landed while the reader was away are unread.
                const arrived = assistantCount - seenAssistantRef.current;
                if (arrived > 0) setUnreadCount((n) => n + arrived);
                seenAssistantRef.current = assistantCount;
            }
            return;
        }
        scrollToLatest(justLoaded ? 'auto' : undefined);
    }, [messagesLength, assistantCount, pending, loading]);

    // Content that arrives without a new message (the availability notice under
    // the last reply, a failed-turn card) is followed once, by a jump: a smooth
    // scroll started for the message itself would stop short of it.
    const contentKeyRef = React.useRef(contentKey);
    React.useEffect(() => {
        if (contentKeyRef.current === contentKey) return;
        contentKeyRef.current = contentKey;
        if (messagesLength === 0 || loading || !nearBottomRef.current) return;
        scrollToLatest('auto');
    }, [contentKey, messagesLength, loading]);

    // Visual viewport changes cover mobile keyboards and orientation changes. Preserve
    // the reader's position when they are browsing older messages.
    React.useEffect(() => {
        // Where the chat's bars do not fit, the panel body scrolls as a last
        // resort and the composer is kept whole: the body is scrolled by the
        // amount the composer band is cut off. A reader working in the
        // priorities block keeps the block instead.
        const findFooter = () => historyRef.current?.closest('section')?.querySelector<HTMLElement>('.chat-footer');
        const keepComposerInView = () => {
            const footer = findFooter();
            const panelBody = footer?.closest<HTMLElement>('.assistant-panel-body');
            if (!footer || !panelBody) return;
            if (!footer.contains(document.activeElement)) {
                const block = panelBody.querySelector<HTMLElement>('.priorities-sidebar');
                if (block && (block.contains(document.activeElement) || block.querySelector('.priorities-scroll'))) return;
            }
            // The composer band reaches past the footer into the panel's padding.
            const band = footer.querySelector<HTMLElement>('form') ?? footer;
            // An on-screen keyboard that does not resize the page still shrinks the visual viewport.
            const viewport = window.visualViewport;
            const visibleBottom = viewport ? viewport.offsetTop + viewport.height : Number.POSITIVE_INFINITY;
            const limit = Math.min(panelBody.getBoundingClientRect().bottom, visibleBottom);
            const cutOff = band.getBoundingClientRect().bottom - limit;
            if (cutOff > 0) panelBody.scrollTop += cutOff;
        };
        // A notice or a longer draft grows the footer without any resize of the window.
        const footerEl = findFooter();
        let footerObserver: ResizeObserver | null = null;
        if (footerEl && typeof ResizeObserver !== 'undefined') {
            footerObserver = new ResizeObserver(keepComposerInView);
            footerObserver.observe(footerEl);
        }
        const handleViewportResize = () => {
            keepComposerInView();
            // A reader who is following keeps following: the keyboard opening
            // shrinks the scroller before the end of the transcript can be
            // re-read, which would otherwise look like scrolling away.
            if (nearBottomRef.current) {
                scrollToLatest('auto');
                return;
            }
            const nearBottom = isNearBottom();
            nearBottomRef.current = nearBottom;
            setShowJumpToLatest(!nearBottom);
            if (nearBottom) scrollToLatest('auto');
        };
        window.addEventListener('resize', handleViewportResize);
        window.visualViewport?.addEventListener('resize', handleViewportResize);
        return () => {
            window.removeEventListener('resize', handleViewportResize);
            window.visualViewport?.removeEventListener('resize', handleViewportResize);
            footerObserver?.disconnect();
        };
    // `loading`: the transcript, and so the footer, is found only once the history has loaded.
    }, [assistantCount, footerKey, loading]);

    return {historyRef, nearBottomRef, showJumpToLatest, unreadCount, scrollToLatest, followNextAppend};
}
