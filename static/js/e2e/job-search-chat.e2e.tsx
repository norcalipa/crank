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
import JobMatchPanel from '../JobMatchPanel';
import '../suggestCompany/mount';

// Job-match panel fixtures (issue #476): mount the real JobMatchPanel against
// stubbed /api/job-matches/* endpoints, selected via ?state=<key>. The status
// payloads mirror the canonical copy produced by crank/empty_state.py so the
// fixtures stay aligned with the backend contract.
const PANEL_STATUS_PAYLOADS: Record<string, Record<string, unknown>> = {
    healthy: {
        state: 'ok',
        title: 'Matches ready',
        message: 'You have job matches ready to review.',
        actions: [],
    },
    refreshing_with_results: {
        state: 'ok',
        title: 'Matches ready',
        message: 'You have job matches ready to review.',
        actions: [],
        refreshing: true,
    },
    partial_coverage: {
        state: 'partial_coverage',
        title: 'Coverage is limited right now',
        message: (
            'Your matches come from the job sources that are working, but not '
            + 'every source is returning listings, so some openings may be '
            + 'missing. Results refresh automatically.'
        ),
        actions: ['retry', 'explore_companies', 'suggest_company', 'help'],
        refreshing: true,
        coverage: {enabled_sources: 2, failing_sources: 1},
        inventory: {active_listings: 4, last_success_at: '2026-09-14T10:00:00Z', age_hours: 2.5},
    },
    no_matches: {
        state: 'no_matches',
        title: 'No matches for your current requirements',
        message: (
            'Jobs are available, but none meet your saved requirements yet. '
            + 'Your active requirements are listed below—chat with the '
            + 'assistant to adjust them, or explore companies while you decide.'
        ),
        actions: ['chat', 'explore_companies', 'suggest_company', 'help'],
        active_constraints: [
            'Minimum salary 150,000',
            'Work location: Remote',
            'Excluded companies: acme',
        ],
        inventory: {active_listings: 3, last_success_at: '2026-09-14T12:00:00Z', age_hours: 1.5},
        relaxation_preview: {
            field: 'work_location',
            label: 'Broadening work location (currently Remote)',
            added_count: 4,
        },
    },
    no_source: {
        state: 'no_source',
        title: 'No job sources configured',
        message: (
            "CRank hasn't been connected to any job sources yet, so job "
            + 'openings can\u2019t be confirmed right now. You can explore the '
            + 'company rankings in the meantime, or suggest a company for '
            + 'evaluation.'
        ),
        actions: ['explore_companies', 'suggest_company', 'help'],
    },
};

const PANEL_RANKED_JOBS = [
    {
        listing_id: 42,
        title: 'Senior Backend Engineer',
        employer_name: 'Acme Corp',
        organization_id: 7,
        organization_name: 'Acme Corp',
        canonical_url: 'https://jobs.example.test/42',
        location_text: 'San Francisco, CA',
        is_remote: true,
        score: 85.5,
        reasons: ['Remote', 'Compensation match', 'Score 4.2'],
    },
    {
        listing_id: 43,
        title: 'Staff Platform Engineer',
        employer_name: 'Globex',
        organization_id: 8,
        organization_name: 'Globex',
        canonical_url: 'https://jobs.example.test/43',
        location_text: 'Portland, OR',
        is_remote: true,
        score: 78.1,
        reasons: ['Remote', 'Industry: Software'],
    },
    {
        listing_id: 44,
        title: 'Product Engineer',
        employer_name: 'Initech',
        organization_id: 9,
        organization_name: 'Initech',
        canonical_url: 'https://jobs.example.test/44',
        location_text: 'Austin, TX (Hybrid)',
        is_remote: false,
        score: 71.3,
        reasons: ['Hybrid (\u22643 days)', 'Culture: collaborative'],
    },
];

const PANEL_RANKED_ORGS = [
    {
        organization_id: 7,
        name: 'Acme Corp',
        url: 'https://acme.example.test',
        funding_round: 'B',
        rto_policy: 'R',
        score: 82.0,
        reasons: ['Series B', 'Remote', 'Score 4.2'],
    },
];

function stubPanelApis(stateKey: string): void {
    const status = PANEL_STATUS_PAYLOADS[stateKey] || PANEL_STATUS_PAYLOADS.no_matches;
    const withResults = (
        stateKey === 'refreshing_with_results'
        || stateKey === 'partial_coverage'
        || stateKey === 'healthy'
    );
    window.fetch = ((input: RequestInfo | URL): Promise<Response> => {
        const url = typeof input === 'string' ? input : input.toString();
        const json = (payload: unknown) => Promise.resolve(new Response(
            JSON.stringify(payload),
            {status: 200, headers: {'Content-Type': 'application/json'}},
        ));
        if (url.includes('/api/job-matches/status/')) return json(status);
        if (url.includes('/api/job-matches/ranked/')) {
            return json({
                job_matches: withResults ? PANEL_RANKED_JOBS : [],
                organization_matches: withResults ? PANEL_RANKED_ORGS : [],
            });
        }
        if (url.includes('/api/job-matches/')) {
            return json({count: withResults ? 3 : 0, next: null, previous: null, results: []});
        }
        return json({});
    }) as typeof fetch;
}

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

    const panelContainer = document.getElementById('job-match-panel');
    if (panelContainer) {
        const stateKey = new URLSearchParams(window.location.search).get('state') || 'no_matches';
        stubPanelApis(stateKey);
        createRoot(panelContainer).render(<JobMatchPanel />);
    }
});
