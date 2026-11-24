// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// The single assistant entry point (issue #472). Every surface renders (or
// imitates) this button; it is a real <button> with aria-expanded and
// aria-controls pointing at the panel so keyboard and screen-reader users
// get the same disclosure semantics everywhere.

import * as React from 'react';
import {openAssistant} from './store';
import {AssistantVisibility} from './types';

interface AssistantLauncherProps {
    visibility: AssistantVisibility;
    // A page with no title or top-bar slot gets a 44px icon-only circle.
    floating?: boolean;
}

const AssistantLauncher = React.forwardRef<HTMLButtonElement, AssistantLauncherProps>(
    ({visibility, floating}, ref) => (
        <button
            ref={ref}
            type="button"
            className={`assistant-launcher${floating ? ' assistant-launcher--floating' : ''}`}
            aria-label="Assistant"
            data-testid="assistant-launcher"
            aria-expanded={visibility === 'open'}
            aria-controls="assistant-panel"
            onClick={() => openAssistant()}
        >
            <i className="fa-solid fa-comments" aria-hidden="true"></i>
            <span className="assistant-launcher-label">Assistant</span>
        </button>
    ),
);
AssistantLauncher.displayName = 'AssistantLauncher';

export default AssistantLauncher;
