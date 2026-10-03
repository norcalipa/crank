// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

export type ScrollOwner = 'transcript' | 'panel';

const NEAR_BOTTOM_PX = 48;
// Our own scrollTo/scrollTop writes (a smooth scroll fires scroll events for a
// while) must not read as the reader scrolling away.
const PROGRAMMATIC_WINDOW_MS = 1000;
const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ']);

interface ScrollAnchor {
    element: HTMLElement;
    offset: number;
}

// Exactly one element scrolls the conversation. Normally that is the
// transcript; when the assistant panel is too short to give the transcript a
// usable height, the transcript grows to its content and the panel body is the
// single scroller instead of nesting a second scrollbar inside it (issue #483).
export function useTranscriptScroll({messagesLength, assistantCount, pending, loading, scrollOwner = 'transcript'}: {
    messagesLength: number;
    assistantCount: number;
    pending: boolean;
    loading: boolean;
    scrollOwner?: ScrollOwner;
}) {
    const historyRef = React.useRef<HTMLDivElement>(null);
    const nearBottomRef = React.useRef(true);
    const seenAssistantRef = React.useRef(assistantCount);
    const programmaticUntilRef = React.useRef(0);
    const anchorRef = React.useRef<ScrollAnchor | null>(null);
    const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);
    const [unreadCount, setUnreadCount] = React.useState(0);

    const prefersReducedMotion = (): boolean => (
        typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );

    const getScroller = (): HTMLElement | null => {
        const history = historyRef.current;
        if (!history) return null;
        if (scrollOwner === 'panel') return history.closest<HTMLElement>('.assistant-panel-body') ?? history;
        return history;
    };

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

    // The top edge of what the reader can see: the scroller's own top, or in
    // panel mode the bottom of the pinned header.
    const visibleTop = (scroller: HTMLElement): number => {
        const rect = scroller.getBoundingClientRect();
        const history = historyRef.current;
        if (scroller === history) return rect.top;
        const header = history?.closest('section')?.querySelector<HTMLElement>('.card-header');
        return Math.max(rect.top, header ? header.getBoundingClientRect().bottom : rect.top);
    };

    // Where the visible top will be once the panel has scrolled far enough for
    // the sticky header to stick: the scroller's top plus the header's height.
    // Before the first write the header may still sit unstuck lower down.
    const stuckTop = (scroller: HTMLElement): number => {
        const history = historyRef.current;
        if (scroller === history) return scroller.getBoundingClientRect().top;
        const header = history?.closest('section')?.querySelector<HTMLElement>('.card-header');
        return scroller.getBoundingClientRect().top + (header ? header.offsetHeight : 0);
    };

    // Remember the first message the reader can see, and how far below the
    // visible top it sits, so the position survives the scroll owner changing.
    const captureAnchor = () => {
        const history = historyRef.current;
        const scroller = getScroller();
        anchorRef.current = null;
        if (!history || !scroller || nearBottomRef.current) return;
        const top = visibleTop(scroller);
        for (const element of Array.from(history.querySelectorAll<HTMLElement>('article'))) {
            const rect = element.getBoundingClientRect();
            if (rect.bottom > top) {
                anchorRef.current = {element, offset: rect.top - top};
                return;
            }
        }
    };

    const isNearBottom = (): boolean => {
        const history = historyRef.current;
        const scroller = getScroller();
        if (!history || !scroller) return true;
        if (scroller === history) {
            return history.scrollHeight - history.scrollTop - history.clientHeight <= NEAR_BOTTOM_PX;
        }
        // Panel scroller: the reader is "at the bottom" once the end of the
        // transcript clears the pinned composer band that overlays the panel.
        const footer = history.closest('section')!.querySelector<HTMLElement>('.chat-footer')!;
        return history.getBoundingClientRect().bottom - footer.getBoundingClientRect().top <= NEAR_BOTTOM_PX;
    };

    const settleAtBottom = () => {
        nearBottomRef.current = true;
        seenAssistantRef.current = assistantCount;
        setShowJumpToLatest(false);
        setUnreadCount(0);
    };

    // A smooth scroll in the panel body is cut short when the pending indicator
    // is replaced by the reply, stranding the last turn behind the pinned
    // composer band, so panel mode jumps instead.
    const scrollToLatest = (behavior: ScrollBehavior = prefersReducedMotion() || scrollOwner === 'panel' ? 'auto' : 'smooth') => {
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
            // the pinned bands (composer, More, the pill) is already in view, and
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
            const atBottom = isNearBottom();
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
    }, [scrollOwner, assistantCount]);

    // Changing who scrolls changes what "the bottom" means. A reader who was
    // following the conversation stays at the end in either mode; one reading
    // older messages keeps their place: the message that was at the top of the
    // view (recorded by captureAnchor before the switch) is put back at the
    // same offset on the new scroller, and the jump pill stays up.
    const previousOwnerRef = React.useRef(scrollOwner);
    React.useLayoutEffect(() => {
        const previous = previousOwnerRef.current;
        previousOwnerRef.current = scrollOwner;
        if (previous === scrollOwner) return;
        if (nearBottomRef.current) {
            scrollToLatest('auto');
            return;
        }
        const anchor = anchorRef.current;
        anchorRef.current = null;
        const scroller = getScroller();
        if (anchor && scroller && anchor.element.isConnected) {
            markProgrammatic(null);
            scroller.scrollTop += anchor.element.getBoundingClientRect().top - stuckTop(scroller) - anchor.offset;
        }
        setShowJumpToLatest(true);
    }, [scrollOwner]);

    // The transcript's box changes size when banners or confirmations appear
    // above it. A reader who is following the conversation stays at the end;
    // someone reading older messages is left where they are.
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
    }, [scrollOwner, assistantCount]);

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

    // Visual viewport changes cover mobile keyboards and orientation changes. Preserve
    // the reader's position when they are browsing older messages.
    React.useEffect(() => {
        const handleViewportResize = () => {
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
        };
    }, [scrollOwner, assistantCount]);

    return {historyRef, nearBottomRef, showJumpToLatest, unreadCount, scrollToLatest, followNextAppend, captureAnchor};
}
