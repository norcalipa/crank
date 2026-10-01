// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Applied-changes summary with Undo (issue #480). Presentational and reusable;
// focus moves to the summary heading on mount (it mounts after Apply/Reset).

import * as React from 'react';
import {ChangeList} from './ReviewChanges';
import type {PreferenceChange} from './format';

export interface AppliedChangesProps {
    changes: PreferenceChange[];
    summary: string;
    canUndo: boolean;
    undoPending?: boolean;
    undoError?: string | null;
    undone?: boolean;
    labels?: Record<string, string>;
    onUndo: () => void;
    onDismiss: () => void;
    testId?: string;
}

export default function AppliedChanges({
    changes, summary, canUndo, undoPending = false, undoError = null, undone = false,
    labels, onUndo, onDismiss, testId = 'priorities-applied',
}: AppliedChangesProps) {
    const headingId = `priorities-applied-${React.useId()}`;
    const headingRef = React.useRef<HTMLHeadingElement>(null);
    React.useEffect(() => {
        headingRef.current?.focus();
    }, [undone]);
    return (
        <div className="alert alert-success pref-change-notice priorities-applied" data-testid={testId}
             role="group" aria-labelledby={headingId}>
            <h3 id={headingId} className="h6 priorities-heading" tabIndex={-1} ref={headingRef}>
                <i className={`fa-solid ${undone ? 'fa-rotate-left' : 'fa-circle-check'} me-1`} aria-hidden="true"></i>
                <span>{undone ? 'Change undone.' : summary}</span>
            </h3>
            {!undone && changes.length > 0 && <ChangeList changes={changes} label="Changed priorities" labels={labels}/>}
            {undoError && (
                <div className="pref-change-error" role="alert" data-testid="priorities-undo-error">
                    <i className="fa-solid fa-triangle-exclamation me-1" aria-hidden="true"></i>
                    {undoError}
                </div>
            )}
            <div className="chat-actions" role="group" aria-label="Applied change actions">
                {!undone && canUndo && (
                    <button type="button" className="btn btn-sm btn-outline-light"
                            onClick={onUndo} disabled={undoPending} aria-busy={undoPending}>
                        {undoPending ? 'Undoing…' : 'Undo'}
                    </button>
                )}
                <button type="button" className="btn btn-sm btn-outline-light" onClick={onDismiss}>
                    Done
                </button>
            </div>
        </div>
    );
}
