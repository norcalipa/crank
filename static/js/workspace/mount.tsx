// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Workspace mount (issue #472). `templates/base.html` renders
// `#assistant-workspace` on every non-admin, non-auth page, and this module
// — part of the `main` entry, so it shares one modalIsolation reference
// count with the suggestion host and the details dialog — mounts the shell
// into it exactly once. Mounting is a no-op when the anchor is absent
// (pattern from static/js/OrganizationList.tsx).

import * as React from 'react';
import {createRoot} from 'react-dom/client';
import WorkspaceShell from './WorkspaceShell';
import {installWorkspaceBridge, openAssistant, setWorkspaceContext} from './store';
import {WorkspaceContext} from './types';

// Server-rendered auth context for the pinned /chat/ host (issue #465 AC-9/10,
// restored after the workspace rebase). The workspace hands these to
// JobSearchChat so its accountKey reconciliation runs before any effect reads
// a stored draft — the contract main's `#job-search-chat` div used to provide.
function authPropsFrom(host: HTMLElement): {
    isAuthenticated?: boolean;
    visitorState?: string;
    signInUrl?: string;
    signedOutMessage?: string;
    accountKey?: string;
} {
    if (host.dataset.assistantPinned !== 'true') {
        return {};
    }
    return {
        isAuthenticated: host.dataset.authenticated === 'true',
        visitorState: host.dataset.visitorState,
        signInUrl: host.dataset.signInUrl,
        signedOutMessage: host.dataset.signedOutMessage,
        accountKey: host.dataset.accountKey,
    };
}

// Derives the surface context from the request path so every page reports
// the same shape through setWorkspaceContext (issue #472 AC-2).
export function surfaceFromPath(pathname: string): WorkspaceContext['surface'] {
    if (pathname.startsWith('/chat/')) {
        return 'chat';
    }
    if (pathname.startsWith('/help/') || pathname.startsWith('/privacy/')) {
        return 'help';
    }
    if (/^\/organization\//.test(pathname)) {
        return 'company';
    }
    return 'rankings';
}

document.addEventListener('DOMContentLoaded', () => {
    const host = document.getElementById('assistant-workspace');
    if (!host) {
        return;
    }
    installWorkspaceBridge();
    setWorkspaceContext({surface: surfaceFromPath(window.location.pathname)});
    const root = createRoot(host);
    root.render(<WorkspaceShell authProps={authPropsFrom(host)}/>);
    // /chat/ is the pinned case of the shared workspace: the panel opens
    // docked (or as the viewport-appropriate placement) on load instead of
    // behind the launcher.
    if (host.dataset.assistantPinned === 'true') {
        openAssistant();
    }
});
