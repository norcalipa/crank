// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Viewport-driven workspace mode (issue #472). `matchMedia` decides which
// placement the shell renders — docked at >=1280px, a drawer at
// 768–1279.98px, a full-screen sheet below 768px — while all geometry stays
// in popup.css keyed off the body mode classes. Falls back to 'sheet' when
// `matchMedia` is unavailable (jsdom) so the safest, fully-blocking
// placement is what non-viewport environments get.

import * as React from 'react';
import {WorkspaceMode} from './types';
import {setWorkspaceMode} from './store';

const DOCKED_QUERY = '(min-width: 1280px)';
const DRAWER_QUERY = '(min-width: 768px)';

export function currentMode(): WorkspaceMode {
    if (typeof window.matchMedia !== 'function') {
        return 'sheet';
    }
    if (window.matchMedia(DOCKED_QUERY).matches) {
        return 'docked';
    }
    if (window.matchMedia(DRAWER_QUERY).matches) {
        return 'drawer';
    }
    return 'sheet';
}

export function useWorkspaceLayout(): WorkspaceMode {
    const [mode, setMode] = React.useState<WorkspaceMode>(currentMode);

    React.useEffect(() => {
        if (typeof window.matchMedia !== 'function') {
            return undefined;
        }
        const docked = window.matchMedia(DOCKED_QUERY);
        const drawer = window.matchMedia(DRAWER_QUERY);
        const handleChange = (): void => {
            setMode(currentMode());
        };
        if (typeof docked.addEventListener === 'function') {
            docked.addEventListener('change', handleChange);
            drawer.addEventListener('change', handleChange);
            return () => {
                docked.removeEventListener('change', handleChange);
                drawer.removeEventListener('change', handleChange);
            };
        }
        // Legacy MediaQueryList API (older Safari).
        docked.addListener(handleChange);
        drawer.addListener(handleChange);
        return () => {
            docked.removeListener(handleChange);
            drawer.removeListener(handleChange);
        };
    }, []);

    // Mirror the mode into the shared snapshot so non-React readers (and the
    // shell's lock bookkeeping) see the same value.
    React.useEffect(() => {
        setWorkspaceMode(mode);
    }, [mode]);

    return mode;
}
