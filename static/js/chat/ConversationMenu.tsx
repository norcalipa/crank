// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

export const NEW_CONVERSATION_COPY =
    'Start a new conversation? Your current history will be archived. Your saved priorities are not changed.';
export const DELETE_CONVERSATION_COPY =
    'Delete this conversation permanently? Its messages are removed and can’t be recovered. '
    + 'Your saved priorities and job matches are not changed. Export it first if you want a copy.';
export const ABOUT_HISTORY_COPY =
    'Your messages and preference updates are saved to your account. New conversation archives this history '
    + 'and starts a fresh one, Export conversation downloads a copy, and Delete conversation removes it '
    + 'permanently. None of these change your saved priorities or job matches.';

type ConfirmKind = 'new' | 'delete';

export interface ConversationMenuProps {
    title: React.ReactNode;
    hasConversation: boolean;
    hasMessages: boolean;
    pending: boolean;
    // Each action resolves to null on success or the failure copy to show.
    onExport: () => Promise<string | null>;
    onNewConversation: () => Promise<string | null>;
    onDeleteConversation: () => Promise<string | null>;
}

interface MenuItem {
    id: 'new' | 'export' | 'delete' | 'about';
    label: string;
    testId: string;
    disabled: boolean;
    danger?: boolean;
}

export function ConversationMenu({
    title, hasConversation, hasMessages, pending,
    onExport, onNewConversation, onDeleteConversation,
}: ConversationMenuProps) {
    const [open, setOpen] = React.useState(false);
    const [confirming, setConfirming] = React.useState<ConfirmKind | null>(null);
    const [working, setWorking] = React.useState(false);
    const [failure, setFailure] = React.useState<string | null>(null);
    const [aboutOpen, setAboutOpen] = React.useState(false);
    const [exportError, setExportError] = React.useState<string | null>(null);
    const [status, setStatus] = React.useState('');
    const moreRef = React.useRef<HTMLButtonElement>(null);
    const menuRef = React.useRef<HTMLDivElement>(null);
    const containerRef = React.useRef<HTMLDivElement>(null);
    const confirmHeadingRef = React.useRef<HTMLHeadingElement>(null);
    const aboutHeadingRef = React.useRef<HTMLHeadingElement>(null);
    const confirmButtonRef = React.useRef<HTMLButtonElement>(null);
    const exportRetryRef = React.useRef<HTMLButtonElement>(null);
    const focusFirstItemRef = React.useRef(true);

    const items: MenuItem[] = [
        {id: 'new', label: 'New conversation', testId: 'conversation-new', disabled: !hasConversation || pending},
        {id: 'export', label: 'Export conversation', testId: 'conversation-export', disabled: !hasConversation || !hasMessages},
        {id: 'delete', label: 'Delete conversation…', testId: 'conversation-delete', disabled: !hasConversation || pending, danger: true},
        {id: 'about', label: 'About saved history', testId: 'conversation-about-history', disabled: false},
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
        if (confirming) confirmHeadingRef.current?.focus();
    }, [confirming]);
    React.useEffect(() => {
        if (aboutOpen) aboutHeadingRef.current?.focus();
    }, [aboutOpen]);
    React.useEffect(() => {
        if (failure) confirmButtonRef.current?.focus();
    }, [failure]);
    React.useEffect(() => {
        if (exportError) exportRetryRef.current?.focus();
    }, [exportError]);

    // A turn that starts while the menu is open disables the destructive
    // items, so a pending confirmation for them is withdrawn too.
    React.useEffect(() => {
        if (pending && confirming && !working) setConfirming(null);
    }, [pending, confirming, working]);
    // The conversation going away (purge, account switch) withdraws the panels.
    React.useEffect(() => {
        if (!hasConversation && !working) {
            setConfirming(null);
            setOpen(false);
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

    const runExport = async () => {
        setExportError(null);
        setStatus('');
        const error = await onExport();
        if (error) {
            setExportError(error);
            return;
        }
        setStatus('Conversation exported');
        moreRef.current?.focus();
    };

    const handleItem = (item: MenuItem) => {
        setOpen(false);
        setExportError(null);
        setStatus('');
        if (item.id === 'export') {
            void runExport();
            return;
        }
        if (item.id === 'about') {
            setConfirming(null);
            setAboutOpen(true);
            return;
        }
        setAboutOpen(false);
        setFailure(null);
        setConfirming(item.id);
    };

    const cancelConfirm = () => {
        setConfirming(null);
        setFailure(null);
        moreRef.current?.focus();
    };

    const closeAbout = () => {
        setAboutOpen(false);
        moreRef.current?.focus();
    };

    const runConfirm = async () => {
        if (!confirming || working) return;
        setWorking(true);
        setFailure(null);
        const error = await (confirming === 'new' ? onNewConversation() : onDeleteConversation());
        setWorking(false);
        if (error) {
            setFailure(error);
            return;
        }
        setConfirming(null);
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
                                <button
                                    key={item.id}
                                    type="button"
                                    role="menuitem"
                                    tabIndex={-1}
                                    disabled={item.disabled}
                                    className={`chat-more-item chat-focus${item.danger ? ' chat-more-item-danger' : ''}`}
                                    data-testid={item.testId}
                                    onClick={() => handleItem(item)}
                                >
                                    {item.label}
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            </div>

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
                    <p className="chat-confirm-copy">{deleting ? DELETE_CONVERSATION_COPY : NEW_CONVERSATION_COPY}</p>
                    {failure && (
                        <p className="chat-confirm-error" role="alert">{failure}</p>
                    )}
                    <div className="chat-confirm-actions">
                        <button
                            type="button"
                            ref={confirmButtonRef}
                            className={`btn btn-sm ${deleting ? 'btn-danger' : 'btn-primary'} chat-focus`}
                            disabled={working}
                            onClick={() => void runConfirm()}
                            data-testid="confirm-action"
                        >
                            {working
                                ? 'Working…'
                                : failure
                                    ? 'Try again'
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
                </div>
            )}

            {aboutOpen && (
                <div className="chat-confirm-panel" role="note" data-testid="about-history-panel">
                    <h3 className="chat-confirm-heading" tabIndex={-1} ref={aboutHeadingRef}>About saved history</h3>
                    <p className="chat-confirm-copy">
                        {ABOUT_HISTORY_COPY}
                    </p>
                    <div className="chat-confirm-actions">
                        <button type="button" className="btn btn-sm btn-outline-light chat-focus" onClick={closeAbout}
                                data-testid="about-history-close">
                            Close
                        </button>
                    </div>
                </div>
            )}

            {exportError && (
                <div className="chat-confirm-panel chat-confirm-panel-danger" role="alert" data-testid="export-error">
                    <p className="chat-confirm-copy mb-1">{exportError}</p>
                    <div className="chat-confirm-actions">
                        <button type="button" ref={exportRetryRef} className="btn btn-sm btn-outline-light chat-focus"
                                onClick={() => void runExport()}>
                            Try again
                        </button>
                    </div>
                </div>
            )}

            <div className="visually-hidden" role="status" aria-live="polite" data-testid="conversation-status">{status}</div>
        </>
    );
}
