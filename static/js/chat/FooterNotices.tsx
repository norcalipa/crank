// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

import {AssistantStatusNotice, statusNoticeShows} from './notices';
import type {AssistantStatus} from './types';

// An error longer than this is shown as one line behind a Details button.
export const LONG_ERROR_CHARS = 120;

export interface FooterNoticesProps {
    status: AssistantStatus | null;
    statusChecking: boolean;
    onStatusRetry: () => void;
    error: string | null;
    errorType: string | null;
    retrying: boolean;
    pending: boolean;
    onRetry: () => void;
}

/** The notices above the composer: the availability status and the error of the
 * last send. A status notice plus an error, an error longer than LONG_ERROR_CHARS,
 * or the error of a failed turn (the transcript already shows that turn's card
 * with its own Retry) would take the composer's room, so they collapse to one
 * line (the error with an ellipsis, its Retry, and a Details button). The error is one
 * `role="alert"` element holding its whole text in both modes; the ellipsis is CSS. */
export function FooterNotices({status, statusChecking, onStatusRetry, error, errorType, retrying, pending, onRetry}: FooterNoticesProps) {
    const listId = `chat-notices-${React.useId()}`;
    const [expanded, setExpanded] = React.useState(false);
    // A new error starts collapsed.
    const [seenError, setSeenError] = React.useState(error);
    if (seenError !== error) {
        setSeenError(error);
        setExpanded(false);
    }
    const statusShown = status !== null && statusNoticeShows(status);
    const collapsible = error !== null && (statusShown || retrying || error.length > LONG_ERROR_CHARS);
    const collapsed = collapsible && !expanded;
    return (
        <div id={listId} data-testid="chat-footer-notices"
             className={`chat-footer-notices${collapsible ? ' is-collapsible' : ''}${collapsed ? ' is-collapsed' : ''}`}>
            {status && (
                <AssistantStatusNotice status={status} checking={statusChecking} onRetry={onStatusRetry}/>
            )}

            {error && (
                <div className="chat-error-row">
                    <div className="alert alert-danger chat-error-alert d-flex justify-content-between align-items-center"
                         role="alert" data-testid="chat-error" data-error-type={errorType || undefined}>
                        <span className="chat-error-text flex-grow-1 me-2">{error}</span>
                        {retrying && (
                            <button type="button"
                                    className="btn btn-sm btn-outline-danger ms-2 flex-shrink-0 text-nowrap chat-focus"
                                    onClick={onRetry} disabled={pending} data-testid="retry-button">
                                Retry
                            </button>
                        )}
                    </div>
                    {collapsible && (
                        <button type="button" className="btn btn-sm btn-outline-light chat-notices-toggle chat-focus"
                                aria-expanded={expanded} aria-controls={listId} data-testid="chat-notices-details"
                                onClick={() => setExpanded((open) => !open)}>
                            {expanded ? 'Hide details' : 'Details'}
                        </button>
                    )}
                </div>
            )}
        </div>
    );
}
