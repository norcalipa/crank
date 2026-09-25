// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Assistant panel chrome (issue #472). Renders the header (title, context
// line, minimize/close), the sheet-mode "Back to results" control, and the
// lazily-loaded chat behind a Suspense boundary. The chat module is only
// imported on first open (React.lazy), and the `#job-search-chat` wrapper
// preserves the 33 popup.css rules and the Django e2e selectors that key
// off that id.

import * as React from 'react';
import {
    clearWorkspaceContext,
    closeAssistant,
    describeWorkspaceContext,
    markWorkspaceLoaded,
    minimizeAssistant,
} from './store';
import {WorkspaceContext, WorkspaceMode} from './types';

const LazyJobSearchChat = React.lazy(() => import('../JobSearchChat'));

function contextLine(context: WorkspaceContext | null): string {
    if (!context) {
        return '';
    }
    const entity = describeWorkspaceContext(context);
    if (entity) {
        return entity;
    }
    if (context.searchTerm) {
        return `Searching “${context.searchTerm}”`;
    }
    return '';
}

// Marks the chunk resolved once real chat content has committed; keeps
// `loaded` honest so minimize never re-imports and the e2e chunk-count
// assertion stays meaningful.
const LoadMarker: React.FC = () => {
    React.useEffect(() => {
        markWorkspaceLoaded();
    }, []);
    return null;
};

interface AssistantPanelProps {
    mode: WorkspaceMode;
    context: WorkspaceContext | null;
    // Server-rendered auth context from the pinned /chat/ workspace host
    // (issue #465 AC-9/10), forwarded verbatim to JobSearchChat.
    authProps?: {
        isAuthenticated?: boolean;
        visitorState?: string;
        signInUrl?: string;
        signedOutMessage?: string;
        accountKey?: string;
    };
}

const AssistantPanel: React.FC<AssistantPanelProps> = ({mode, context, authProps}) => {
    const line = contextLine(context);
    const entityLabel = describeWorkspaceContext(context);
    // Clearing removes the strip, so focus moves to the panel heading rather
    // than being lost with the button (issue #479 a11y).
    const handleClear = () => {
        clearWorkspaceContext();
        document.getElementById('assistant-panel-title')?.focus();
    };
    return (
        <div className="assistant-panel-inner">
            {/* Sheet mode: Back to results is the FIRST focusable control in
                the sheet (issue #472 a11y contract), so it renders ahead of
                the header in DOM order. */}
            {mode === 'sheet' && (
                <button type="button" className="btn btn-outline-light assistant-back-to-results"
                        onClick={closeAssistant} data-testid="assistant-back-to-results">
                    <i className="fa-solid fa-arrow-left" aria-hidden="true"></i> Back to results
                </button>
            )}
            <div className="assistant-panel-header">
                <div className="assistant-panel-heading">
                    <h2 className="assistant-panel-title" id="assistant-panel-title" tabIndex={-1}>
                        <i className="fa-solid fa-comments" aria-hidden="true"></i> Job Search Assistant
                    </h2>
                    {line && (
                        <div className={entityLabel ? 'assistant-context-strip' : undefined}
                             data-testid={entityLabel ? 'assistant-context-strip' : undefined}>
                            <p className="assistant-panel-context" data-testid="assistant-context-line">
                                {line}
                            </p>
                            {entityLabel && (
                                <button type="button" className="btn btn-sm btn-outline-light assistant-clear-context"
                                        onClick={handleClear} aria-label={`Clear context: ${entityLabel}`}
                                        data-testid="assistant-clear-context">
                                    Clear context
                                </button>
                            )}
                        </div>
                    )}
                </div>
                <div className="assistant-panel-controls">
                    <button type="button" className="btn btn-sm btn-outline-light assistant-panel-control"
                            onClick={minimizeAssistant} aria-label="Minimize assistant"
                            data-testid="assistant-minimize">
                        <i className="fa-solid fa-window-minimize" aria-hidden="true"></i>
                        <span className="assistant-panel-control-label" aria-hidden="true">Minimize</span>
                    </button>
                    <button type="button" className="btn btn-sm btn-outline-light assistant-panel-control assistant-panel-control--close"
                            onClick={closeAssistant} aria-label="Close assistant"
                            data-testid="assistant-close">
                        <i className="fa-solid fa-xmark" aria-hidden="true"></i>
                        <span className="assistant-panel-control-label" aria-hidden="true">Close</span>
                    </button>
                </div>
            </div>
            <div className="assistant-panel-body">
                <React.Suspense
                    fallback={(
                        <div className="assistant-loading" role="status" aria-live="polite"
                             data-testid="assistant-loading">
                            <span className="assistant-loading-line assistant-loading-line--header" aria-hidden="true"></span>
                            <span className="assistant-loading-line assistant-loading-line--message" aria-hidden="true"></span>
                            <span className="assistant-loading-line assistant-loading-line--message short" aria-hidden="true"></span>
                            <span className="assistant-loading-line assistant-loading-line--composer" aria-hidden="true"></span>
                            <span className="visually-hidden">Loading the assistant…</span>
                        </div>
                    )}
                >
                    <LoadMarker/>
                    {/* Wrapper id preserves the popup.css rules and Django e2e
                        selectors that key off #job-search-chat (issue #472 AC-11). */}
                    <div id="job-search-chat">
                        <LazyJobSearchChat {...(authProps ?? {})} workspaceMode={mode}/>
                    </div>
                </React.Suspense>
            </div>
        </div>
    );
};

export default AssistantPanel;
