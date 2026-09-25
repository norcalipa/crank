// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Mode-driven workspace placement (issue #472). Reads the shared store and
// the matchMedia hook, then renders the assistant as a docked region
// (>=1280px), a non-modal right-edge drawer (768–1279px) or a full-screen
// sheet (<768px). Owns the body mode classes, Escape handling, sheet-only
// modal semantics + lockBackground/unlockBackground, focus management and
// the main-region scroll save/restore.

import * as React from 'react';
import {lockBackground, subscribeBackgroundLock, unlockBackground} from '../modalIsolation';
import AssistantLauncher from './AssistantLauncher';
import AssistantPanel from './AssistantPanel';
import {closeAssistant, getWorkspaceSnapshot, openAssistant, subscribeWorkspace} from './store';
import {useWorkspaceLayout} from './useWorkspaceLayout';
import {WORKSPACE_FOCUS_EVENT, WorkspaceMode, WorkspaceSnapshot} from './types';

const MODE_CLASS: Record<WorkspaceMode, string> = {
    docked: 'assistant-docked',
    drawer: 'assistant-drawer',
    sheet: 'assistant-sheet',
};
const MODE_CLASSES = Object.values(MODE_CLASS);
// Open-state hook on the app shell (issue #479): host content reflows against
// its own container width whenever the panel occupies the viewport edge.
const OPEN_CLASS = 'assistant-open';

function useWorkspaceSnapshot(): WorkspaceSnapshot {
    const [snapshot, setSnapshot] = React.useState<WorkspaceSnapshot>(getWorkspaceSnapshot);
    React.useEffect(() => subscribeWorkspace(() => setSnapshot(getWorkspaceSnapshot())), []);
    return snapshot;
}

interface WorkspaceShellProps {
    // Server-rendered auth context from the pinned /chat/ workspace host
    // (issue #465 AC-9/10), forwarded to JobSearchChat. Absent everywhere else.
    authProps?: {
        isAuthenticated?: boolean;
        visitorState?: string;
        signInUrl?: string;
        signedOutMessage?: string;
        accountKey?: string;
    };
}

const WorkspaceShell: React.FC<WorkspaceShellProps> = ({authProps}) => {
    const snapshot = useWorkspaceSnapshot();
    const mode = useWorkspaceLayout();
    // Minimized is NOT open: the panel unmounts (the loaded flag in the store
    // keeps the chunk warm) and a labelled compact restore control takes its
    // place so the state is never an ambiguous icon-only affordance.
    const open = snapshot.visibility === 'open';
    const minimized = snapshot.visibility === 'minimized';
    const launcherRef = React.useRef<HTMLButtonElement>(null);
    const restoreRef = React.useRef<HTMLButtonElement>(null);
    const panelRef = React.useRef<HTMLElement>(null);
    // Saved window scroll position while the sheet owns the viewport.
    const savedScrollRef = React.useRef<number | null>(null);

    // Body mode class: exactly one of the three while open, none closed.
    React.useEffect(() => {
        for (const cls of [...MODE_CLASSES, OPEN_CLASS]) {
            document.body.classList.remove(cls);
        }
        if (open) {
            document.body.classList.add(MODE_CLASS[mode], OPEN_CLASS);
        }
        return () => {
            for (const cls of [...MODE_CLASSES, OPEN_CLASS]) {
                document.body.classList.remove(cls);
            }
        };
    }, [open, mode]);

    // Sheet-only background isolation. The lock/unlock pair is keyed on
    // (open && mode === 'sheet') so a mode change from sheet to
    // docked/drawer while open releases the lock exactly once, and a change
    // into sheet acquires it once.
    const sheetActive = open && mode === 'sheet';
    React.useEffect(() => {
        if (!sheetActive) {
            return undefined;
        }
        lockBackground();
        return () => {
            unlockBackground();
        };
    }, [sheetActive]);

    // One blocking surface at a time (issue #472 AC-6): the sheet holds one
    // modalIsolation lock while open. A second blocking surface acquiring a
    // lock is a silent no-op on the DOM (the refcount keeps the page locked
    // exactly once), so the shared lock COUNT is the signal: anything above
    // the sheet's own hold means a dialog opened on top, and the sheet
    // yields — its unlock is reference-counted, so the dialog's isolation
    // stays intact and the page never ends up half-inerted.
    React.useEffect(() => {
        if (!sheetActive) {
            return undefined;
        }
        return subscribeBackgroundLock((count) => {
            if (count > 1) {
                closeAssistant();
            }
        });
    }, [sheetActive]);

    // Escape closes drawer and sheet; docked stays put (it is in normal
    // flow, not an overlay).
    React.useEffect(() => {
        if (!open || mode === 'docked') {
            return undefined;
        }
        const handleKeyDown = (event: KeyboardEvent): void => {
            if (event.key === 'Escape') {
                event.preventDefault();
                closeAssistant();
            }
        };
        document.addEventListener('keydown', handleKeyDown);
        return () => document.removeEventListener('keydown', handleKeyDown);
    }, [open, mode]);

    // Sheet scroll save/restore: opening the sheet pins the document
    // (lockBackground sets overflow hidden), so the main region's position
    // is captured on open and restored on close.
    React.useEffect(() => {
        if (sheetActive) {
            savedScrollRef.current = window.scrollY;
            return () => {
                if (savedScrollRef.current !== null) {
                    window.scrollTo(0, savedScrollRef.current);
                    savedScrollRef.current = null;
                }
            };
        }
        return undefined;
    }, [sheetActive]);

    // Focus management: moving into the sheet on open, back to the launcher
    // on close. Drawer/docked leave focus alone (non-modal placements).
    const wasSheetActiveRef = React.useRef(false);
    React.useEffect(() => {
        if (sheetActive && !wasSheetActiveRef.current) {
            const target = panelRef.current?.querySelector<HTMLElement>(
                '[data-testid="assistant-back-to-results"], button, [href], textarea, [tabindex]:not([tabindex="-1"])',
            );
            target?.focus();
        }
        if (!sheetActive && wasSheetActiveRef.current) {
            // The launcher leaves the DOM while minimized, so the sheet's
            // return focus lands on the restore pill in that state.
            (launcherRef.current ?? restoreRef.current)?.focus();
        }
        wasSheetActiveRef.current = sheetActive;
    }, [sheetActive]);

    // Explicit focus request (issue #469 review): the "Focus assistant"
    // affordance must move keyboard focus to the assistant panel even when it
    // is already open, where drawer/docked have no open-transition focus. A
    // monotonic counter (rather than a boolean guard) means a request that
    // races the open transition is still honoured once the panel commits.
    const [focusRequest, setFocusRequest] = React.useState(0);
    React.useEffect(() => {
        const handleFocusRequest = (): void => setFocusRequest((n) => n + 1);
        window.addEventListener(WORKSPACE_FOCUS_EVENT, handleFocusRequest);
        return () => window.removeEventListener(WORKSPACE_FOCUS_EVENT, handleFocusRequest);
    }, []);
    React.useEffect(() => {
        if (!open || focusRequest === 0) {
            return;
        }
        // A pending request stays pending across a cold lazy load: only try
        // to focus once the chat chunk has committed (``snapshot.loaded``),
        // so a request that races the composer mount is retried then rather
        // than settling for a header control that is about to be replaced
        // (issue #469 review).
        if (!snapshot.loaded) {
            return;
        }
        // Prefer the composer (the assistant's primary input); fall back to the
        // first focusable control when it is not present (e.g. sheet mode or
        // a chat that has not finished loading). A disabled composer (pending
        // or gated turn) cannot take focus, so it is excluded and the request
        // falls back to an enabled control rather than being silently consumed
        // with focus landing nowhere (issue #469 review). querySelector honours
        // document order, so a single combined selector would catch the header
        // buttons ahead of the composer.
        const composer =
            panelRef.current?.querySelector<HTMLElement>(
                '[data-testid="assistant-composer"]:not(:disabled)',
            );
        const target =
            composer ??
            panelRef.current?.querySelector<HTMLElement>(
                '[data-testid="assistant-back-to-results"]:not(:disabled), textarea:not(:disabled), button:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
            );
        if (target) {
            target.focus();
            // Consume the request exactly once, and only after focus actually
            // landed on an enabled target: a later ordinary drawer/docked open
            // leaves focus alone, and a request that finds no enabled control
            // stays pending to be retried once one commits.
            setFocusRequest(0);
        }
    }, [open, focusRequest, snapshot.loaded]);

    return (
        <>
            {/* Minimized state (visual review #472 round 5): exactly one
                entry point — the restore pill. The launcher leaves the DOM
                so the two co-located controls never stack in the same
                corner with the launcher's focus ring painted underneath.
                The launcher also leaves the DOM while the panel is OPEN
                (issue #469 re-critique): a pressed floating launcher beside
                the already-open docked panel is a redundant second entry
                point, so it renders only while closed. */}
            {!open && (
                <div className="assistant-dock" data-testid="assistant-dock">
                    {!minimized && <AssistantLauncher ref={launcherRef} visibility={snapshot.visibility}/>}
                    {minimized && (
                        <button
                            ref={restoreRef}
                            type="button"
                            className="assistant-restore"
                            data-testid="assistant-restore"
                            aria-label="Reopen assistant"
                            onClick={() => openAssistant()}
                        >
                            <i className="fa-solid fa-comments" aria-hidden="true"></i>
                            <span className="assistant-restore-label">Reopen assistant</span>
                        </button>
                    )}
                </div>
            )}
            {open && (
                mode === 'sheet' ? (
                    <section
                        id="assistant-panel"
                        ref={panelRef}
                        className="assistant-panel assistant-panel--sheet"
                        role="dialog"
                        aria-modal="true"
                        aria-labelledby="assistant-panel-title"
                        data-testid="assistant-panel"
                    >
                        <AssistantPanel mode={mode} context={snapshot.context} authProps={authProps}/>
                    </section>
                ) : (
                    <section
                        id="assistant-panel"
                        ref={panelRef}
                        className={`assistant-panel assistant-panel--${mode}`}
                        aria-labelledby="assistant-panel-title"
                        data-testid="assistant-panel"
                    >
                        <AssistantPanel mode={mode} context={snapshot.context} authProps={authProps}/>
                    </section>
                )
            )}
        </>
    );
};

export default WorkspaceShell;
