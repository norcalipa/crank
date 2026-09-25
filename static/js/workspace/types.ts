// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Shared workspace contract (issue #472). Types and event-name constants
// only — no runtime behaviour and no React import, so later waves can depend
// on this module from any bundle without pulling the store in.

export type WorkspaceMode = 'docked' | 'drawer' | 'sheet';
export type AssistantVisibility = 'closed' | 'open' | 'minimized';

export interface WorkspaceContext {
    surface: 'rankings' | 'company' | 'jobs' | 'comparison' | 'help' | 'chat';
    organizationId?: number;
    organizationName?: string;
    jobId?: number;
    // At most 4 unique positive integers (contract-only until #490).
    comparisonIds?: number[];
    searchTerm?: string;
    page?: number;
}

export type AccountStatus = 'unknown' | 'anonymous' | 'authenticated';
export interface WorkspaceAccount {
    status: AccountStatus;
    key: string;
}

export interface WorkspaceSnapshot {
    visibility: AssistantVisibility;
    mode: WorkspaceMode;
    context: WorkspaceContext | null;
    // True once the lazy chat chunk has resolved.
    loaded: boolean;
    // Bumped exactly once per effective context change (issue #479).
    contextRevision: number;
    account: WorkspaceAccount;
    // Mirror of the chat's active server conversation; never persisted.
    conversationId: number | null;
}

// Per-tab sessionStorage record (issue #479).
export const WORKSPACE_SESSION_KEY = 'crank:workspace:v1';

export const WORKSPACE_OPEN_EVENT = 'crank:assistant-open';       // detail?: Partial<WorkspaceContext>
export const WORKSPACE_CONTEXT_EVENT = 'crank:workspace-context'; // detail: Partial<WorkspaceContext>
// Focus request (issue #469 review): a background surface asks that keyboard
// focus move to the already-open assistant panel (the "Focus assistant"
// affordance). Carries no detail.
export const WORKSPACE_FOCUS_EVENT = 'crank:assistant-focus';
