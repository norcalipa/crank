// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Mounts the main-region priorities section into #priorities-main (issue
// #480). Imported by the `jobmatch` entry; non-blocking and free of chat and
// modal-isolation imports.

import * as React from 'react';
import {createRoot} from 'react-dom/client';
import PrioritiesSection from './PrioritiesSection';

export function mountPriorities(): void {
    const container = document.getElementById('priorities-main');
    if (!container) {
        return;
    }
    const authenticated = container.dataset.authenticated === 'true';
    createRoot(container).render(
        <PrioritiesSection variant="main" authenticated={authenticated}/>,
    );
}
