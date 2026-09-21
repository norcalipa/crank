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
import {WorkspaceMode, WorkspaceSnapshot} from './types';

const MODE_CLASS: Record<WorkspaceMode, string> = {
    docked: 'assistant-docked',
    drawer: 'assistant-drawer',
    sheet: 'assistant-sheet',
};
const MODE_CLASSES = Object.values(MODE_CLASS);

function useWorkspaceSnapshot(): WorkspaceSnapshot {
    const [snapshot, setSnapshot] = React.useState<WorkspaceSnapshot>(getWorkspaceSnapshot);
    React.useEffect(() => subscribeWorkspace(() => setSnapshot(getWorkspaceSnapshot())), []);
    return snapshot;
}

const WorkspaceShell: React.FC = () => {
    const snapshot = useWorkspaceSnapshot();
    const mode = useWorkspaceLayout();
    // Minimized is NOT open: the panel unmounts (the loaded flag in the store
    // keeps the chunk warm) and a labelled compact restore control takes its
    // place so the state is never an ambiguous icon-only affordance.
    const open = snapshot.visibility === 'open';
    const minimized = snapshot.visibility === 'minimized';
    const launcherRef = React.useRef<HTMLButtonElement>(null);
    const panelRef = React.useRef<HTMLElement>(null);
    // Saved window scroll position while the sheet owns the viewport.
    const savedScrollRef = React.useRef<number | null>(null);

    // Body mode class: exactly one of the three while open, none closed.
    React.useEffect(() => {
        for (const cls of MODE_CLASSES) {
            document.body.classList.remove(cls);
        }
        if (open) {
            document.body.classList.add(MODE_CLASS[mode]);
        }
        return () => {
            for (const cls of MODE_CLASSES) {
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
            launcherRef.current?.focus();
        }
        wasSheetActiveRef.current = sheetActive;
    }, [sheetActive]);

    return (
        <>
            <AssistantLauncher ref={launcherRef} visibility={snapshot.visibility}/>
            {minimized && (
                <button
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
                        <AssistantPanel mode={mode} context={snapshot.context}/>
                    </section>
                ) : (
                    <section
                        id="assistant-panel"
                        ref={panelRef}
                        className={`assistant-panel assistant-panel--${mode}`}
                        aria-labelledby="assistant-panel-title"
                        data-testid="assistant-panel"
                    >
                        <AssistantPanel mode={mode} context={snapshot.context}/>
                    </section>
                )
            )}
        </>
    );
};

export default WorkspaceShell;
