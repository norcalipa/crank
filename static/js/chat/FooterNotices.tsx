// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

import {AssistantStatusNotice, statusNoticeShows} from './notices';
import type {AssistantStatus} from './types';

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
 * last send. Until the reader asks for more they are one compact row: the error
 * in up to two lines (it is one `role="alert"` element holding its whole text in
 * both modes; the clamp is CSS), its Retry, and, when a status notice is folded,
 * that notice's own title above the text. Details exists only when something is
 * hidden: a status notice, or an error the row cuts at this width (measured after
 * layout). Expanded, the details scroll above a toggle that never moves: the
 * toggle is the last row of the area, in the same DOM place in both states. */
export function FooterNotices({status, statusChecking, onStatusRetry, error, errorType, retrying, pending, onRetry}: FooterNoticesProps) {
    const listId = `chat-notices-${React.useId()}`;
    const [expanded, setExpanded] = React.useState(false);
    const [cut, setCut] = React.useState(false);
    // A new error starts collapsed and unmeasured.
    const [seenError, setSeenError] = React.useState(error);
    if (seenError !== error) {
        setSeenError(error);
        setExpanded(false);
        setCut(false);
    }
    const statusShown = status !== null && statusNoticeShows(status);
    const textRef = React.useRef<HTMLSpanElement>(null);
    // Measured while the row is compact, and again whenever the text's box changes.
    // Showing the toggle only narrows the text further, so a cut text stays cut and
    // the measurement cannot loop; dropping it only widens the text.
    React.useLayoutEffect(() => {
        const el = textRef.current;
        if (!el || expanded) return undefined;
        const measure = () => setCut(el.scrollHeight > el.clientHeight + 1);
        measure();
        if (typeof ResizeObserver === 'undefined') return undefined;
        const observer = new ResizeObserver(measure);
        observer.observe(el);
        return () => observer.disconnect();
    }, [error, expanded, retrying, statusShown]);
    const showToggle = error !== null && (statusShown || cut || expanded);
    const compact = error !== null && !expanded;
    return (
        <div id={listId} data-testid="chat-footer-notices"
             className={`chat-footer-notices${showToggle ? ' is-collapsible' : ''}${compact ? ' is-collapsed' : ''}${retrying ? ' has-retry' : ''}${error !== null && statusShown ? ' has-status' : ''}`}>
            <div className="chat-notices-body">
                {error && (
                    <div className="alert alert-danger chat-error-alert d-flex justify-content-between align-items-center"
                         role="alert" data-testid="chat-error" data-error-type={errorType || undefined}>
                        <span ref={textRef} className="chat-error-text flex-grow-1 me-2">{error}</span>
                        {retrying && (
                            <button type="button"
                                    className="btn btn-sm btn-outline-danger ms-2 flex-shrink-0 text-nowrap chat-focus"
                                    onClick={onRetry} disabled={pending} data-testid="retry-button">
                                Retry
                            </button>
                        )}
                    </div>
                )}
                {status && (
                    <AssistantStatusNotice status={status} checking={statusChecking} onRetry={onStatusRetry}/>
                )}
            </div>
            {showToggle && (
                <button type="button" className="btn btn-sm btn-outline-light chat-notices-toggle chat-focus"
                        aria-expanded={expanded} aria-controls={listId} data-testid="chat-notices-details"
                        onClick={() => setExpanded((open) => !open)}>
                    {expanded ? 'Hide details' : 'Details'}
                </button>
            )}
        </div>
    );
}
