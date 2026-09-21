// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import * as React from 'react';
import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import WorkspaceShell from './WorkspaceShell';
import {
    closeAssistant,
    openAssistant,
    resetWorkspaceForTests,
} from './store';
import * as modalIsolation from '../modalIsolation';

jest.mock('../JobSearchChat', () => ({
    __esModule: true,
    default: () => <section data-testid="job-search-chat">chat</section>,
}));

let mockMode: 'docked' | 'drawer' | 'sheet' = 'sheet';
jest.mock('./useWorkspaceLayout', () => ({
    useWorkspaceLayout: () => mockMode,
}));

beforeEach(() => {
    resetWorkspaceForTests();
    document.body.className = '';
    mockMode = 'sheet';
    // The shell observer needs the background roots to exist.
    document.body.innerHTML = '<div class="app-shell"></div><main class="app-content"></main>';
});

function renderShell() {
    // RTL appends its container to document.body by default, so the
    // MutationObserver on .app-shell and the focus assertions see one tree.
    return render(<WorkspaceShell/>);
}

describe('WorkspaceShell', () => {
    test('closed renders only the launcher', () => {
        renderShell();
        expect(screen.getByTestId('assistant-launcher')).toBeInTheDocument();
        expect(screen.queryByTestId('assistant-panel')).not.toBeInTheDocument();
    });

    test('sheet open calls lockBackground once; close calls unlockBackground once', async () => {
        const lock = jest.spyOn(modalIsolation, 'lockBackground');
        const unlock = jest.spyOn(modalIsolation, 'unlockBackground');
        renderShell();
        act(() => openAssistant());
        await waitFor(() => expect(screen.getByTestId('assistant-panel')).toBeInTheDocument());
        expect(lock).toHaveBeenCalledTimes(1);
        act(() => closeAssistant());
        await waitFor(() => expect(screen.queryByTestId('assistant-panel')).not.toBeInTheDocument());
        expect(unlock).toHaveBeenCalledTimes(1);
        lock.mockRestore();
        unlock.mockRestore();
    });

    test('a mode change from sheet to docked while open releases the lock exactly once', async () => {
        const unlock = jest.spyOn(modalIsolation, 'unlockBackground');
        const {rerender} = renderShell();
        act(() => openAssistant());
        await waitFor(() => expect(screen.getByTestId('assistant-panel')).toBeInTheDocument());
        mockMode = 'docked';
        rerender(<WorkspaceShell/>);
        await waitFor(() => expect(unlock).toHaveBeenCalledTimes(1));
        expect(document.body.classList.contains('assistant-docked')).toBe(true);
        expect(document.body.classList.contains('assistant-sheet')).toBe(false);
        unlock.mockRestore();
    });

    test('sheet sets role=dialog + aria-modal; drawer and docked set neither', async () => {
        renderShell();
        act(() => openAssistant());
        await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
        expect(screen.getByRole('dialog')).toHaveAttribute('aria-modal', 'true');
        act(() => closeAssistant());

        mockMode = 'drawer';
        act(() => openAssistant());
        await waitFor(() => expect(screen.getByTestId('assistant-panel')).toBeInTheDocument());
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
        expect(screen.getByTestId('assistant-panel')).not.toHaveAttribute('aria-modal');
    });

    test('Escape closes drawer and sheet but not docked', async () => {
        renderShell();
        act(() => openAssistant());
        await waitFor(() => expect(screen.getByTestId('assistant-panel')).toBeInTheDocument());
        fireEvent.keyDown(document, {key: 'Escape'});
        await waitFor(() => expect(screen.queryByTestId('assistant-panel')).not.toBeInTheDocument());

        mockMode = 'docked';
        act(() => openAssistant());
        await waitFor(() => expect(screen.getByTestId('assistant-panel')).toBeInTheDocument());
        fireEvent.keyDown(document, {key: 'Escape'});
        expect(screen.getByTestId('assistant-panel')).toBeInTheDocument();
    });

    test('body class is exactly one of the three mode classes at a time', async () => {
        renderShell();
        expect(document.body.className).toBe('');
        act(() => openAssistant());
        await waitFor(() => expect(document.body.classList.contains('assistant-sheet')).toBe(true));
        expect(document.body.classList.contains('assistant-docked')).toBe(false);
        expect(document.body.classList.contains('assistant-drawer')).toBe(false);
        act(() => closeAssistant());
        await waitFor(() => expect(document.body.className).toBe(''));
    });

    test('the main region scroll position is restored after a sheet close', async () => {
        const scrollTo = jest.fn();
        Object.defineProperty(window, 'scrollTo', {value: scrollTo, writable: true, configurable: true});
        Object.defineProperty(window, 'scrollY', {value: 240, writable: true, configurable: true});
        renderShell();
        act(() => openAssistant());
        await waitFor(() => expect(screen.getByTestId('assistant-panel')).toBeInTheDocument());
        act(() => closeAssistant());
        await waitFor(() => expect(scrollTo).toHaveBeenCalledWith(0, 240));
    });

    test('focus moves into the sheet on open and returns to the launcher on close', async () => {
        renderShell();
        act(() => openAssistant());
        await waitFor(() => expect(screen.getByTestId('assistant-back-to-results')).toBeInTheDocument());
        await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('assistant-back-to-results')));
        act(() => closeAssistant());
        await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('assistant-launcher')));
    });

    test('another blocking dialog acquiring a lock closes the sheet, and keeps its isolation', async () => {
        renderShell();
        act(() => openAssistant());
        await waitFor(() => expect(screen.getByTestId('assistant-panel')).toBeInTheDocument());
        // A second blocking surface (details/suggest dialog) locks over the
        // sheet: the sheet yields and its unlock is reference-counted, so the
        // dialog's isolation survives the sheet closing.
        act(() => {
            modalIsolation.lockBackground();
        });
        await waitFor(() => expect(screen.queryByTestId('assistant-panel')).not.toBeInTheDocument());
        const shell = document.querySelector('.app-shell') as HTMLElement;
        expect(shell.hasAttribute('inert')).toBe(true);
        act(() => {
            modalIsolation.unlockBackground();
        });
        expect(shell.hasAttribute('inert')).toBe(false);
    });

    test('the launcher click opens the assistant', async () => {
        renderShell();
        fireEvent.click(screen.getByTestId('assistant-launcher'));
        await waitFor(() => expect(screen.getByTestId('assistant-panel')).toBeInTheDocument());
    });

    test('the launcher is a button with aria-expanded and aria-controls', () => {
        renderShell();
        const launcher = screen.getByTestId('assistant-launcher');
        expect(launcher.tagName).toBe('BUTTON');
        expect(launcher).toHaveAttribute('aria-expanded', 'false');
        expect(launcher).toHaveAttribute('aria-controls', 'assistant-panel');
    });

    test('minimized swaps the panel for a labelled compact restore control', async () => {
        renderShell();
        act(() => openAssistant());
        await waitFor(() => expect(screen.getByTestId('assistant-panel')).toBeInTheDocument());
        fireEvent.click(screen.getByTestId('assistant-minimize'));
        // The panel unmounts and an unambiguous text-cued chip replaces it
        // (visual review #472 round 1, item 7: icon-only was ambiguous).
        await waitFor(() => expect(screen.queryByTestId('assistant-panel')).not.toBeInTheDocument());
        const restore = screen.getByTestId('assistant-restore');
        expect(restore).toHaveTextContent('Reopen assistant');
        expect(document.body.classList.contains('assistant-sheet')).toBe(false);
        fireEvent.click(restore);
        await waitFor(() => expect(screen.getByTestId('assistant-panel')).toBeInTheDocument());
        expect(screen.queryByTestId('assistant-restore')).not.toBeInTheDocument();
    });
});
