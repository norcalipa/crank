// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

export function useTranscriptScroll({messagesLength, pending, loading}: {
    messagesLength: number;
    pending: boolean;
    loading: boolean;
}) {
    const historyRef = React.useRef<HTMLDivElement>(null);
    const nearBottomRef = React.useRef(true);
    const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);

    const prefersReducedMotion = (): boolean => (
        typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );

    const isNearBottom = (element: HTMLDivElement): boolean => (
        element.scrollHeight - element.scrollTop - element.clientHeight <= 48
    );

    const scrollToLatest = (behavior: ScrollBehavior = prefersReducedMotion() ? 'auto' : 'smooth') => {
        const history = historyRef.current;
        if (!history) return;
        if (typeof history.scrollTo === 'function') {
            history.scrollTo({top: history.scrollHeight, behavior});
        } else {
            history.scrollTop = history.scrollHeight;
        }
        nearBottomRef.current = true;
        setShowJumpToLatest(false);
    };

    // Keep the latest content visible only while the reader is already at the bottom.
    React.useEffect(() => {
        const history = historyRef.current;
        if (!history) return;
        const handleScroll = () => {
            const nearBottom = isNearBottom(history);
            nearBottomRef.current = nearBottom;
            setShowJumpToLatest(!nearBottom);
        };
        history.addEventListener('scroll', handleScroll, {passive: true});
        return () => history.removeEventListener('scroll', handleScroll);
    }, []);

    // Initial history, optimistic turns, replies, and the pending indicator all append
    // content to the same viewport. Do not interrupt someone reading older messages.
    React.useEffect(() => {
        // Empty history (visual review #472 round 5): never auto-scroll — the
        // empty state stays anchored at the top of the log so its lead is
        // visible on first open, even on the shortest sheet viewports.
        // Auto-scroll resumes once a conversation exists or content is added.
        if (messagesLength === 0) {
            return;
        }
        if (loading || !nearBottomRef.current) {
            if (!nearBottomRef.current) setShowJumpToLatest(true);
            return;
        }
        scrollToLatest();
    }, [messagesLength, pending, loading]);

    // Visual viewport changes cover mobile keyboards and orientation changes. Preserve
    // the reader's position when they are browsing older messages.
    React.useEffect(() => {
        const handleViewportResize = () => {
            const history = historyRef.current;
            if (!history) return;
            const nearBottom = isNearBottom(history);
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
    }, []);

    return {historyRef, nearBottomRef, showJumpToLatest, scrollToLatest};
}
