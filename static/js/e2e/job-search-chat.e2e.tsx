// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// E2E fixture entry point (not part of the production webpack build). Mounts the
// real JobSearchChat and OrganizationList components into the minimal fixture
// pages under e2e/fixtures/ so Playwright can assert the viewport matrix against
// the actual rendered UI. See issue #431.
import * as React from 'react';
import {createRoot} from 'react-dom/client';
import JobSearchChat from '../JobSearchChat';
import OrganizationList from '../OrganizationList';

document.addEventListener('DOMContentLoaded', () => {
    const chatContainer = document.getElementById('job-search-chat');
    if (chatContainer) {
        createRoot(chatContainer).render(<JobSearchChat />);
    }

    const orgContainer = document.getElementById('organization-list');
    const orgData = document.getElementById('organization-data');
    if (orgContainer && orgData && orgData.textContent) {
        try {
            const organizations = JSON.parse(orgData.textContent);
            createRoot(orgContainer).render(
                <OrganizationList
                    organizations={organizations}
                    canSuggestCompany={false}
                    isAuthenticated={true}
                />,
            );
        } catch (error) {
            console.error('Error parsing organization data:', error);
        }
    }
});
