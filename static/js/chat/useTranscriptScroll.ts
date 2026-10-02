// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

export type ScrollOwner = 'transcript' | 'panel';

const NEAR_BOTTOM_PX = 48;
const INTENT_WINDOW_MS = 1000;
const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ']);

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
    const lastIntentRef = React.useRef(0);
    const draggingRef = React.useRef(false);
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
        if (scroller) scroller.scrollTop = scroller.scrollHeight;
    }, [messagesLength, pending]);

    // Keep the latest content visible only while the reader is already at the bottom.
    React.useEffect(() => {
        const scroller = getScroller();
        if (!scroller) return undefined;
        // Only the reader can end "following". Layout shifts (a reply replacing
        // the pending indicator, a banner appearing) clamp or move scrollTop
        // without any input, and must not read as scrolling away (issue #483).
        // Typing in the composer (inside the panel scroller in panel mode) is not
        // scrolling; only keys that scroll, pressed outside form controls, count.
        const markIntent = (e?: Event) => {
            if (e?.type === 'keydown') {
                if (!SCROLL_KEYS.has((e as KeyboardEvent).key)) return;
                if ((e.target as HTMLElement).closest('textarea, input, select, button, [contenteditable="true"]')) return;
            }
            lastIntentRef.current = Date.now();
        };
        // A press on the scroller itself (not a button inside it) is a scrollbar drag.
        const pointerDown = (e: Event) => { draggingRef.current = e.target === scroller; };
        const pointerUp = () => {
            if (!draggingRef.current) return;
            draggingRef.current = false;
            markIntent();
        };
        const handleScroll = () => {
            const intentional = draggingRef.current || Date.now() - lastIntentRef.current < INTENT_WINDOW_MS;
            const nearBottom = isNearBottom() || (!intentional && nearBottomRef.current);
            nearBottomRef.current = nearBottom;
            setShowJumpToLatest(!nearBottom);
            if (nearBottom) {
                seenAssistantRef.current = assistantCount;
                setUnreadCount(0);
            }
        };
        const intentEvents = ['wheel', 'touchmove', 'keydown'] as const;
        intentEvents.forEach((name) => scroller.addEventListener(name, markIntent, {passive: true}));
        scroller.addEventListener('pointerdown', pointerDown, {passive: true});
        window.addEventListener('pointerup', pointerUp);
        scroller.addEventListener('scroll', handleScroll, {passive: true});
        return () => {
            scroller.removeEventListener('scroll', handleScroll);
            intentEvents.forEach((name) => scroller.removeEventListener(name, markIntent));
            scroller.removeEventListener('pointerdown', pointerDown);
            window.removeEventListener('pointerup', pointerUp);
        };
    }, [scrollOwner, assistantCount]);

    // Changing who scrolls changes what "the bottom" means. A reader who was
    // following the conversation stays at the end in either mode; one reading
    // older messages keeps their place and gets the jump pill.
    const previousOwnerRef = React.useRef(scrollOwner);
    React.useEffect(() => {
        const previous = previousOwnerRef.current;
        previousOwnerRef.current = scrollOwner;
        if (previous === scrollOwner) return;
        if (scrollOwner === 'panel') {
            if (nearBottomRef.current) {
                scrollToLatest('auto');
                return;
            }
            setShowJumpToLatest(true);
        } else if (nearBottomRef.current) {
            scrollToLatest('auto');
        }
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

    return {historyRef, nearBottomRef, showJumpToLatest, unreadCount, scrollToLatest, followNextAppend};
}
