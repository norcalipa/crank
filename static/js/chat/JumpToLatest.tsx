// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

export function JumpToLatest({unreadCount, onJump}: {unreadCount: number; onJump: () => void}) {
    const noun = unreadCount === 1 ? 'message' : 'messages';
    return (
        <div className="chat-jump-row text-center">
            <button type="button" className="btn btn-sm btn-secondary rounded-pill chat-focus"
                    onClick={onJump}
                    aria-label={unreadCount > 0
                        ? `Jump to latest message, ${unreadCount} new ${noun}`
                        : 'Jump to latest message'}
                    data-testid="jump-to-latest">
                <i className="fa-solid fa-arrow-down me-1" aria-hidden="true"></i>
                Jump to latest
                {unreadCount > 0 && (
                    <span className="badge rounded-pill text-bg-primary ms-1">
                        {unreadCount} new
                    </span>
                )}
            </button>
        </div>
    );
}
