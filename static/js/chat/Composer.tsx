// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

const MAX_COMPOSER_ROWS = 6;

export interface ComposerProps {
    textareaRef: React.RefObject<HTMLTextAreaElement>;
    value: string;
    pending: boolean;
    authenticated: boolean;
    textareaDisabled: boolean;
    sendDisabled: boolean;
    onChange: (value: string) => void;
    onSubmit: (e: React.FormEvent) => void;
    onEnter: () => void;
    onStop: () => void;
}

export function Composer({
    textareaRef, value, pending, authenticated, textareaDisabled, sendDisabled,
    onChange, onSubmit, onEnter, onStop,
}: ComposerProps) {
    // Auto-resize the composer textarea up to a bounded max rows.
    const adjustComposerHeight = React.useCallback(() => {
        const ta = textareaRef.current;
        if (!ta) return;
        ta.style.height = 'auto';
        if (!ta.value) {
            ta.style.overflowY = 'hidden';
            return;
        }
        const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 24;
        const maxHeight = lineHeight * MAX_COMPOSER_ROWS;
        const desired = Math.min(ta.scrollHeight, maxHeight);
        ta.style.height = `${desired}px`;
        ta.style.overflowY = ta.scrollHeight > maxHeight ? 'auto' : 'hidden';
    }, [textareaRef]);
    // Adjust the composer on mount and on every keystroke/content reset. This
    // only invokes the (stable) adjuster; it does *not* (re)register the passive
    // window/font listeners below, so typing does not recreate them each key.
    React.useEffect(() => {
        adjustComposerHeight();
    }, [adjustComposerHeight, value]);

    // Register the long-lived listeners exactly once: window/viewport resize plus
    // a one-shot document.fonts.ready hook so the height is re-measured once web
    // fonts finish loading (the initial measure uses a fallback line-height before
    // the real face paints). Because adjustComposerHeight is stable and the only
    // dependency, these listeners are never re-registered per keystroke.
    React.useEffect(() => {
        // Defensive/idempotent mount-time measure: Effect 1 already runs
        // adjustComposerHeight when the input state settles, but this guarantees
        // the height is correct before the font/resize listeners are registered
        // — the two effects are intentionally order-independent.
        adjustComposerHeight();
        let cancelled = false;
        if (document.fonts && typeof document.fonts.ready?.then === 'function') {
            const reflow = () => { if (!cancelled) adjustComposerHeight(); };
            void document.fonts.ready.then(reflow, reflow);
        }
        window.addEventListener('resize', adjustComposerHeight);
        window.visualViewport?.addEventListener('resize', adjustComposerHeight);
        return () => {
            cancelled = true;
            window.removeEventListener('resize', adjustComposerHeight);
            window.visualViewport?.removeEventListener('resize', adjustComposerHeight);
        };
    }, [adjustComposerHeight]);

    // IME candidate confirmation also reports Enter. Chromium sets isComposing
    // and keyCode 229; Safari fires compositionend BEFORE the confirming keydown,
    // so a flag that outlives compositionend by one task covers it (issue #483).
    const composingRef = React.useRef(false);
    const settleTimerRef = React.useRef<number | null>(null);
    React.useEffect(() => () => {
        if (settleTimerRef.current !== null) window.clearTimeout(settleTimerRef.current);
    }, []);

    const handleCompositionStart = () => {
        if (settleTimerRef.current !== null) {
            window.clearTimeout(settleTimerRef.current);
            settleTimerRef.current = null;
        }
        composingRef.current = true;
    };

    const handleCompositionEnd = () => {
        settleTimerRef.current = window.setTimeout(() => {
            composingRef.current = false;
            settleTimerRef.current = null;
        }, 0);
    };

    const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key !== 'Enter' || e.shiftKey) return;
        if (e.nativeEvent.isComposing || composingRef.current || e.keyCode === 229) return;
        e.preventDefault();
        onEnter();
    };

    return (
        <form onSubmit={onSubmit} aria-busy={pending}>
            {/* chat-composer-pending (round-2 critique): while the Stop
                control renders, compact Send to an icon-only 44px target on
                narrow screens so the row never clips the placeholder. */}
            <div className={`input-group${pending ? ' chat-composer-pending' : ''}`}>
                <textarea
                    ref={textareaRef}
                    className="form-control chat-focus"
                    placeholder="Type your message…"
                    aria-label="Message"
                    data-testid="assistant-composer"
                    aria-describedby={!authenticated ? 'job-search-signed-out-reason' : undefined}
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    onKeyDown={handleKeyDown}
                    onCompositionStart={handleCompositionStart}
                    onCompositionEnd={handleCompositionEnd}
                    disabled={textareaDisabled}
                    autoComplete="off"
                    rows={1}
                    style={{resize: 'none', overflowY: 'hidden'}}
                />
                <button type="submit" className="btn btn-primary chat-send chat-focus"
                        disabled={sendDisabled}
                        aria-label="Send message" aria-describedby={!authenticated ? 'job-search-signed-out-reason' : undefined}>
                    <i className="fa-solid fa-paper-plane" aria-hidden="true"></i>
                    <span>Send</span>
                </button>
                {pending && (
                    <button type="button" className="btn btn-outline-light chat-stop chat-focus" onClick={onStop}
                            aria-label="Stop waiting for response" data-testid="stop-button">
                        <i className="fa-solid fa-circle-stop" aria-hidden="true"></i>
                        <span>Stop</span>
                    </button>
                )}
            </div>
            <p className="chat-disclaimer mb-0" role="note">AI can be wrong. Check important details.</p>
        </form>
    );
}
