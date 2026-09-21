// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// The `jobmatch` webpack entry (issue #472): holds the `#job-match-panel`
// mount extracted out of JobMatchPanel.tsx so the component module is
// import-safe (no module-level side effects) and the workspace owns the
// only chat mount. `jobmatch` stays a separate entry because it renders
// main-region results and never blocks the page.

import * as React from 'react';
import {createRoot} from 'react-dom/client';
import JobMatchPanel from './JobMatchPanel';

document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('job-match-panel');
    if (container) {
        const root = createRoot(container);
        root.render(<JobMatchPanel/>);
    }
});
