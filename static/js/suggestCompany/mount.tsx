// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createRoot} from 'react-dom/client';
import SuggestCompanyHost from './SuggestCompanyHost';
import {installSuggestCompanyBridge} from './controller';

// Mounts the single shared "Suggest a company" controller for the whole app
// (issue #471). `templates/base.html` renders `#suggest-company-host` on
// every page (outside the isolated app-shell/main-content roots), and this
// bundle loads in <head> on every page, so exactly one host — and one
// `modalIsolation` reference count — exists per page. Mounting is a no-op
// when the host element is absent, mirroring the guarded-mount pattern in
// static/js/OrganizationList.tsx.
document.addEventListener('DOMContentLoaded', () => {
    const host = document.getElementById('suggest-company-host');
    if (!host) {
        return;
    }
    installSuggestCompanyBridge();
    const root = createRoot(host);
    root.render(<SuggestCompanyHost/>);
});
