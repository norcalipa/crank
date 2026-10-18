// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

import {CONFIRM_ARM_DELAY_MS} from './confirmArmDelay';
import {SESSION_ENDED_COPY} from '../session';

export const NEW_CONVERSATION_COPY =
    'Your current history will be archived. Your saved priorities are not changed.';
export const DELETE_CONVERSATION_COPY =
    'Its messages are removed and can’t be recovered. Your saved priorities and job matches are not changed.';
export const ABOUT_HISTORY_COPY =
    'Your messages and preference updates are saved to your account. New conversation archives this history '
    + 'and starts a fresh one, Export conversation downloads a copy, and Delete conversation removes it '
    + 'permanently. None of these change your saved priorities or job matches.';

type ConfirmKind = 'new' | 'delete';

// What an action reports back: null when it worked, the failure copy to show,
// or DROPPED when its answer belongs to a view that is gone (an account
// switch) and must be shown nowhere: no status, no error.
export const DROPPED = Symbol('dropped');
export type HistoryOutcome = string | null | typeof DROPPED;

export interface ConversationMenuProps {
    title: React.ReactNode;
    hasConversation: boolean;
    hasMessages: boolean;
    pending: boolean;
    onExport: () => Promise<HistoryOutcome>;
    onNewConversation: () => Promise<HistoryOutcome>;
    onDeleteConversation: () => Promise<HistoryOutcome>;
    // Reports whether a New / Delete confirmation is open, so the chat can make what lies beneath it inert.
    onConfirmationChange?: (open: boolean) => void;
    // Where "Sign in again" leads when a failure says the session has ended.
    signInUrl?: string;
}

interface MenuItem {
    id: 'new' | 'export' | 'delete' | 'about';
    label: string;
    testId: string;
    disabled: boolean;
    danger?: boolean;
    separatorBefore?: boolean;
}

// The ended-session copy names "Sign in again": make those words the way to do it, returning to this page.
const SIGN_IN_WORDS = 'Sign in again';

const InlineError = React.forwardRef<HTMLParagraphElement, {message: string; id?: string; signInUrl?: string}>(function InlineError({message, id, signInUrl}, ref) {
    const [before, after] = message === SESSION_ENDED_COPY && signInUrl !== undefined
        ? message.split(SIGN_IN_WORDS) : [message, null];
    return (
        <p className="chat-confirm-error" role="alert" id={id} ref={ref}>
            <i className="fa-solid fa-triangle-exclamation" aria-hidden="true"></i>
            <span>
                {before}
                {after !== null && (
                    <>
                        <a className="chat-confirm-signin chat-focus" href={`${signInUrl}?next=${encodeURIComponent(window.location.pathname)}`}
                           data-testid="confirm-sign-in">{SIGN_IN_WORDS}</a>
                        {after}
                    </>
                )}
            </span>
        </p>
    );
});

// Scroll the panel stack so `target` is fully inside it, then bring `context`
// (the failure text above it) into view only as far as that keeps the target
// visible. Nothing is pinned: at short heights the reader scrolls through the
// consequence to reach the button (issue #547).
function revealWithin(target: HTMLElement | null, context: HTMLElement | null) {
    const stack = target?.closest<HTMLElement>('.chat-panel-stack');
    if (!target || !stack) return;
    const stackRect = stack.getBoundingClientRect();
    const hidden = target.getBoundingClientRect().bottom - stackRect.bottom;
    if (hidden > 0) stack.scrollTop += hidden + 4;
    if (!context) return;
    const above = stackRect.top - context.getBoundingClientRect().top;
    const room = stackRect.bottom - target.getBoundingClientRect().bottom;
    if (above > 0 && room > 0) stack.scrollTop -= Math.min(above, room);
}

export function ConversationMenu({
    title, hasConversation, hasMessages, pending,
    onExport, onNewConversation, onDeleteConversation, onConfirmationChange, signInUrl,
}: ConversationMenuProps) {
    const [open, setOpen] = React.useState(false);
    const [confirming, setConfirming] = React.useState<ConfirmKind | null>(null);
    const [working, setWorking] = React.useState(false);
    const [exporting, setExporting] = React.useState(false);
    const exportingRef = React.useRef(false);
    const [failure, setFailure] = React.useState<string | null>(null);
    const [aboutOpen, setAboutOpen] = React.useState(false);
    const [exportError, setExportError] = React.useState<string | null>(null);
    const [status, setStatus] = React.useState('');
    const moreRef = React.useRef<HTMLButtonElement>(null);
    const menuRef = React.useRef<HTMLDivElement>(null);
    const containerRef = React.useRef<HTMLDivElement>(null);
    // The chat is told in the same update that opens or closes the confirmation, so what lies beneath it never
    // stays inert for a moment after the confirmation is gone.
    const changeConfirming = (next: ConfirmKind | null) => {
        setConfirming(next);
        onConfirmationChange?.(next !== null);
    };
    const confirmHeadingRef = React.useRef<HTMLHeadingElement>(null);
    const aboutHeadingRef = React.useRef<HTMLHeadingElement>(null);
    const confirmButtonRef = React.useRef<HTMLButtonElement>(null);
    const exportRetryRef = React.useRef<HTMLButtonElement>(null);
    const exportPanelRef = React.useRef<HTMLDivElement>(null);
    const failureRef = React.useRef<HTMLParagraphElement>(null);
    const focusFirstItemRef = React.useRef(true);
    // Handler time (performance.now) and event time (the opening click's timeStamp)
    // are compared only with their own kind: the first catches presses handled
    // close together, the second presses that were made, or began, early.
    const confirmOpenedAtRef = React.useRef(0);
    const confirmOpenedStampRef = React.useRef(0);
    const confirmPressStartRef = React.useRef<number | null>(null);

    const items: MenuItem[] = [
        {id: 'new', label: 'New conversation…', testId: 'conversation-new', disabled: !hasConversation || pending},
        {id: 'export', label: 'Export conversation', testId: 'conversation-export', disabled: !hasConversation || !hasMessages || exporting},
        {id: 'about', label: 'About saved history', testId: 'conversation-about-history', disabled: false},
        {
            id: 'delete', label: 'Delete conversation…', testId: 'conversation-delete',
            disabled: !hasConversation || pending, danger: true, separatorBefore: true,
        },
    ];

    const enabledButtons = (): HTMLButtonElement[] => Array.from(
        menuRef.current?.querySelectorAll<HTMLButtonElement>('button[role="menuitem"]:not(:disabled)') ?? [],
    );

    React.useEffect(() => {
        if (!open) return;
        const buttons = enabledButtons();
        (focusFirstItemRef.current ? buttons[0] : buttons[buttons.length - 1])?.focus();
    }, [open]);

    React.useEffect(() => {
        if (!open) return undefined;
        const handlePointer = (e: Event) => {
            if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
                setOpen(false);
            }
        };
        document.addEventListener('mousedown', handlePointer);
        document.addEventListener('touchstart', handlePointer);
        return () => {
            document.removeEventListener('mousedown', handlePointer);
            document.removeEventListener('touchstart', handlePointer);
        };
    }, [open]);

    React.useEffect(() => {
        if (!confirming) return;
        // The heading and the whole consequence sentence are what the reader sees first.
        const heading = confirmHeadingRef.current as HTMLHeadingElement;
        heading.focus({preventScroll: true});
        (heading.closest('.chat-panel-stack') as HTMLElement).scrollTop = 0;
    }, [confirming]);
    React.useEffect(() => {
        onConfirmationChange?.(confirming !== null);
    }, [confirming, onConfirmationChange]);
    React.useEffect(() => {
        if (aboutOpen) aboutHeadingRef.current?.focus();
    }, [aboutOpen]);
    React.useEffect(() => {
        if (!failure) return;
        // The focused retry must be visible, with the failure text above it when both fit.
        confirmButtonRef.current?.focus({preventScroll: true});
        revealWithin(confirmButtonRef.current, failureRef.current);
    }, [failure]);
    React.useEffect(() => {
        if (!exportError) return;
        exportRetryRef.current?.focus({preventScroll: true});
        revealWithin(exportRetryRef.current, exportPanelRef.current);
    }, [exportError]);

    // A turn that starts while the menu is open disables the destructive
    // items, so a pending confirmation for them is withdrawn too.
    React.useEffect(() => {
        if (pending && confirming && !working) setConfirming(null);
    }, [pending, confirming, working]);
    // The conversation going away (Delete, purge, account switch) withdraws the
    // panels, including an export failure and the export status: nothing is left
    // to retry or to announce for a conversation that is gone.
    React.useEffect(() => {
        if (!hasConversation && !working) {
            setConfirming(null);
            setOpen(false);
            setExportError(null);
            setStatus('');
        }
    }, [hasConversation, working]);

    const handleMoreKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            focusFirstItemRef.current = e.key === 'ArrowDown';
            setOpen(true);
        }
    };

    const handleMenuKeyDown = (e: React.KeyboardEvent) => {
        const buttons = enabledButtons();
        const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
        let next: HTMLButtonElement | undefined;
        switch (e.key) {
            case 'ArrowDown':
                next = buttons[(index + 1) % buttons.length];
                break;
            case 'ArrowUp':
                next = buttons[(index - 1 + buttons.length) % buttons.length];
                break;
            case 'Home':
                next = buttons[0];
                break;
            case 'End':
                next = buttons[buttons.length - 1];
                break;
            case 'Escape':
                e.preventDefault();
                setOpen(false);
                moreRef.current?.focus();
                return;
            case 'Tab':
                setOpen(false);
                return;
            default:
                return;
        }
        e.preventDefault();
        next?.focus();
    };

    // True when focus is on the page body (lost with a closed menu item or panel), or inside `closing`.
    const focusHasFallen = (closing?: Element | null) => {
        const active = document.activeElement;
        return !active || active === document.body || !!closing?.contains(active);
    };

    const runExport = async (keepFocus = false) => {
        // One request at a time: a second press while one is in flight starts nothing.
        if (exportingRef.current) return;
        exportingRef.current = true;
        setExporting(true);
        setExportError(null);
        setStatus('Exporting conversation…');
        let outcome: HistoryOutcome;
        try {
            outcome = await onExport();
        } finally {
            exportingRef.current = false;
            setExporting(false);
        }
        if (outcome === DROPPED) {
            setStatus('');
            // The menu item that held focus is gone; focus that fell to the page returns to More, focus the next reader placed stays.
            if (!keepFocus && focusHasFallen()) moreRef.current?.focus();
            return;
        }
        if (outcome) {
            setStatus('');
            setExportError(outcome);
            return;
        }
        setStatus('Conversation exported');
        if (!keepFocus) moreRef.current?.focus();
    };

    const handleItem = (item: MenuItem, e: React.MouseEvent) => {
        setOpen(false);
        setExportError(null);
        setStatus('');
        if (item.id === 'export') {
            void runExport();
            return;
        }
        if (item.id === 'about') {
            changeConfirming(null);
            setAboutOpen(true);
            return;
        }
        setAboutOpen(false);
        setFailure(null);
        confirmOpenedAtRef.current = performance.now();
        confirmOpenedStampRef.current = e.timeStamp;
        confirmPressStartRef.current = null;
        changeConfirming(item.id);
    };

    const cancelConfirm = () => {
        changeConfirming(null);
        setFailure(null);
        moreRef.current?.focus();
    };

    const closeAbout = () => {
        setAboutOpen(false);
        moreRef.current?.focus();
    };

    const runConfirm = async (e: React.MouseEvent) => {
        if (!confirming || working) return;
        // A click with detail > 0 is judged by the stored press start, or by its own
        // time when no pointerdown has reached the button since the confirmation
        // opened; a click with detail 0 (a key press on the focused button) only by
        // its own time. Only a new opening (handleItem) clears the stored press start:
        // not its own click, not pointerup, not Cancel. Firefox sends an accessibility
        // activation as a detail 1 click with no pointerdown, so after one early
        // pointer press such activations are ignored until the confirmation is opened again.
        const began = e.detail > 0 ? confirmPressStartRef.current ?? e.timeStamp : e.timeStamp;
        if (performance.now() - confirmOpenedAtRef.current < CONFIRM_ARM_DELAY_MS) return;
        if (began - confirmOpenedStampRef.current < CONFIRM_ARM_DELAY_MS) return;
        setWorking(true);
        setFailure(null);
        const outcome = await (confirming === 'new' ? onNewConversation() : onDeleteConversation());
        setWorking(false);
        if (outcome && outcome !== DROPPED) {
            setFailure(outcome);
            return;
        }
        // Focus lost with the closing confirmation returns to More; focus the next reader placed elsewhere stays.
        const focusLost = focusHasFallen(confirmHeadingRef.current?.parentElement);
        changeConfirming(null);
        if (outcome === DROPPED && focusLost) moreRef.current?.focus();
    };

    const deleting = confirming === 'delete';

    return (
        <>
            <div className="chat-header-row" ref={containerRef}>
                {title}
                <div className="chat-more">
                    <button
                        type="button"
                        ref={moreRef}
                        className="btn btn-sm btn-outline-light chat-more-button chat-focus"
                        aria-haspopup="menu"
                        aria-expanded={open}
                        aria-controls="conversation-menu"
                        data-testid="conversation-more"
                        onClick={() => {
                            focusFirstItemRef.current = true;
                            setOpen((o) => !o);
                        }}
                        onKeyDown={handleMoreKeyDown}
                    >
                        <i className="fa-solid fa-ellipsis" aria-hidden="true"></i>
                        <span>More</span>
                    </button>
                    {open && (
                        <div
                            id="conversation-menu"
                            ref={menuRef}
                            role="menu"
                            aria-label="Conversation options"
                            className="chat-more-menu"
                            data-testid="conversation-menu"
                            onKeyDown={handleMenuKeyDown}
                        >
                            {items.map((item) => (
                                <React.Fragment key={item.id}>
                                    {item.separatorBefore && <div role="separator" className="chat-more-separator" />}
                                    <button
                                        type="button"
                                        role="menuitem"
                                        tabIndex={-1}
                                        disabled={item.disabled}
                                        className={`chat-more-item chat-focus${item.danger ? ' chat-more-item-danger' : ''}`}
                                        data-testid={item.testId}
                                        onClick={(e) => handleItem(item, e)}
                                    >
                                        {item.label}
                                    </button>
                                </React.Fragment>
                            ))}
                        </div>
                    )}
                </div>
            </div>

            <div className="chat-panel-stack">
            {confirming && (
                <div
                    className={`chat-confirm-panel${deleting ? ' chat-confirm-panel-danger' : ''}`}
                    role="group"
                    aria-labelledby="chat-confirm-heading"
                    data-testid="confirm-panel"
                >
                    <h3 id="chat-confirm-heading" className="chat-confirm-heading" tabIndex={-1} ref={confirmHeadingRef}>
                        {deleting ? 'Delete conversation?' : 'Start a new conversation?'}
                    </h3>
                    <p id="chat-confirm-copy" className="chat-confirm-copy">
                        {deleting ? DELETE_CONVERSATION_COPY : NEW_CONVERSATION_COPY}
                    </p>
                    {failure && <InlineError message={failure} id="chat-confirm-failure" ref={failureRef} signInUrl={signInUrl}/>}
                    <div className="chat-confirm-dismiss">
                        <button
                            type="button"
                            ref={confirmButtonRef}
                            className={`btn btn-sm ${deleting ? 'btn-danger' : 'btn-primary'} chat-focus`}
                            disabled={working || exporting}
                            aria-describedby={failure ? 'chat-confirm-copy chat-confirm-failure' : 'chat-confirm-copy'}
                            onPointerDown={(e) => { confirmPressStartRef.current = e.timeStamp; }}
                            onClick={(e) => void runConfirm(e)}
                            data-testid="confirm-action"
                        >
                            {working
                                ? 'Working…'
                                : failure
                                    ? deleting ? 'Try deleting again' : 'Try again'
                                    : deleting ? 'Delete conversation' : 'Start new conversation'}
                        </button>
                        <button
                            type="button"
                            className="btn btn-sm btn-outline-light chat-focus"
                            disabled={working}
                            onClick={cancelConfirm}
                            data-testid="confirm-cancel"
                        >
                            Cancel
                        </button>
                    </div>
                    {deleting && (
                        <div className="chat-confirm-actions">
                            <button
                                type="button"
                                className="btn btn-sm btn-link chat-focus"
                                disabled={working || !hasMessages}
                                aria-disabled={exporting || undefined}
                                onClick={() => void runExport(true)}
                                data-testid="confirm-export-first"
                            >
                                {exporting ? 'Exporting…' : 'Export first'}
                            </button>
                        </div>
                    )}
                </div>
            )}

            {aboutOpen && (
                <div className="chat-confirm-panel" role="note" data-testid="about-history-panel">
                    <h3 className="chat-confirm-heading" tabIndex={-1} ref={aboutHeadingRef}>About saved history</h3>
                    <p className="chat-confirm-copy">
                        {ABOUT_HISTORY_COPY}
                    </p>
                    <div className="chat-confirm-dismiss">
                        <button type="button" className="btn btn-sm btn-outline-light chat-focus" onClick={closeAbout}
                                data-testid="about-history-close">
                            Close
                        </button>
                    </div>
                </div>
            )}

            {exportError && (
                <div ref={exportPanelRef} className="chat-confirm-panel chat-confirm-panel-danger"
                     data-testid="export-error">
                    <InlineError message={exportError} signInUrl={signInUrl}/>
                    <div className="chat-confirm-actions">
                        <button type="button" ref={exportRetryRef} className="btn btn-sm btn-outline-light chat-focus"
                                onClick={() => void runExport()}>
                            Try export again
                        </button>
                    </div>
                    <div className="chat-confirm-dismiss">
                        <button type="button" className="btn btn-sm btn-outline-light chat-focus"
                                data-testid="export-error-dismiss"
                                onClick={() => { setExportError(null); moreRef.current?.focus(); }}>
                            Dismiss
                        </button>
                    </div>
                </div>
            )}
            </div>

            <div className="visually-hidden" aria-live="polite" aria-atomic="true" data-testid="conversation-status">{status}</div>
        </>
    );
}
