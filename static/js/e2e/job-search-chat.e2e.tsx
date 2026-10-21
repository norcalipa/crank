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
        inventory: {active_listings: 4, last_success_at: '2026-09-14T10:00:00Z', age_hours: 2.5},
    },
    refreshing_with_results: {
        state: 'ok',
        title: 'Matches ready',
        message: 'You have job matches ready to review.',
        actions: [],
        refreshing: true,
        inventory: {active_listings: 4, last_success_at: '2026-09-14T10:00:00Z', age_hours: 2.5},
    },
    partial_coverage: {
        state: 'partial_coverage',
        title: 'Coverage is limited right now',
        message: (
            'Your matches come from the job sources that are working, but not '
            + 'every source is returning listings, so some openings may be '
            + 'missing.'
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
        reasons: ['Remote', 'Compensation match'],
        fit_score: 85.5,
        company_score: 4.2,
        coverage: 0.67,
        requirements: [
            {path: 'compensation.minimum_salary', status: 'match', observed: 150000},
            {path: 'work_location.modes', status: 'match', observed: 'remote'},
            {path: 'industry', status: 'unknown', observed: null},
            {path: 'work_location.max_in_office_days', status: 'mismatch', observed: '3 in-office days'},
        ],
        unsupported: ['compensation.minimum_total_compensation'],
        revision: {stale: false, generated_at: '2026-09-22T08:00:00Z', preference_revision: 4, ranking_version: '1.0.0', data_revision: null},
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
        fit_score: 78.1,
        company_score: 3.8,
        coverage: 1.0,
        requirements: [
            {path: 'work_location.modes', status: 'match', observed: 'remote'},
            {path: 'industry', status: 'match', observed: 'Software'},
        ],
        unsupported: [],
        revision: {stale: false, generated_at: '2026-09-22T08:00:00Z'},
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
        score: 100.0,
        reasons: ['Series B', 'Remote'],
        fit_score: 100.0,
        priorities_met: {met: 2, counted: 2},
        company_score: 4.1,
        coverage: 1.0,
        requirements: [
            {path: 'funding_stage', status: 'match', observed: 'B', counted: true},
            {path: 'work_location.modes', status: 'match', observed: 'remote', counted: true},
            {path: 'compensation.minimum_salary', status: 'unknown', observed: null, counted: false},
        ],
        unsupported: [],
        revision: {stale: false, generated_at: '2026-09-22T08:00:00Z'},
    },
];

// Issue #584: the ranked list follows the "Priorities met" figure. Two
// companies tie on 1 of 2; the one with the higher average rating goes first.
// The minimum salary is checked for each job, so the list note names it once.
const PANEL_TIED_ORGS = [
    PANEL_RANKED_ORGS[0],
    {
        organization_id: 8,
        name: 'Birch Labs',
        url: 'https://birch.example.test',
        funding_round: 'B',
        rto_policy: 'O',
        score: 50.0,
        reasons: ['Series B'],
        fit_score: 50.0,
        priorities_met: {met: 1, counted: 2},
        company_score: 4.6,
        coverage: 1.0,
        requirements: [
            {path: 'funding_stage', status: 'match', observed: 'B', counted: true},
            {path: 'work_location.modes', status: 'mismatch', observed: 'in-office', counted: true},
            {path: 'compensation.minimum_salary', status: 'unknown', observed: null, counted: false},
        ],
        unsupported: [],
        revision: {stale: false, generated_at: '2026-09-22T08:00:00Z'},
    },
    {
        organization_id: 9,
        name: 'Cedar Works',
        url: 'https://cedar.example.test',
        funding_round: 'B',
        rto_policy: 'O',
        score: 50.0,
        reasons: ['Remote'],
        fit_score: 50.0,
        priorities_met: {met: 1, counted: 2},
        company_score: 3.2,
        coverage: 1.0,
        requirements: [
            {path: 'funding_stage', status: 'unknown', observed: null, counted: true},
            {path: 'work_location.modes', status: 'match', observed: 'remote', counted: true},
            {path: 'compensation.minimum_salary', status: 'unknown', observed: null, counted: false},
        ],
        unsupported: [],
        revision: {stale: false, generated_at: '2026-09-22T08:00:00Z'},
    },
];

// A reader whose priorities are all checked for each job: no company has a figure.
const PANEL_PER_JOB_REQUIREMENTS = [{path: 'industry', status: 'unknown', observed: null, counted: false}];
const PANEL_PER_JOB_ORGS = [
    {...PANEL_RANKED_ORGS[0], score: 0.0, fit_score: 0.0, priorities_met: null, coverage: 0.0, requirements: PANEL_PER_JOB_REQUIREMENTS},
    {...PANEL_TIED_ORGS[1], score: 0.0, fit_score: 0.0, priorities_met: null, coverage: 0.0, requirements: PANEL_PER_JOB_REQUIREMENTS},
];

function stubPanelApis(stateKey: string): void {
    if (stateKey === 'loading') {
        // Never resolves: the panel stays in its loading skeleton for screenshots.
        window.fetch = (() => new Promise(() => {})) as typeof fetch;
        return;
    }
    if (stateKey === 'error') {
        window.fetch = ((_input: RequestInfo | URL): Promise<Response> => Promise.resolve(
            new Response('{"error": "service unavailable"}', {
                status: 500,
                headers: {'Content-Type': 'application/json'},
            }),
        )) as typeof fetch;
        return;
    }
    const status = stateKey === 'stale'
        ? PANEL_STATUS_PAYLOADS.healthy
        : stateKey === 'org_ties' || stateKey === 'org_per_job_only'
            ? PANEL_STATUS_PAYLOADS.healthy
            : (PANEL_STATUS_PAYLOADS[stateKey] || PANEL_STATUS_PAYLOADS.no_matches);
    const withResults = (
        stateKey === 'refreshing_with_results'
        || stateKey === 'partial_coverage'
        || stateKey === 'healthy'
        || stateKey === 'stale'
        || stateKey === 'org_ties'
        || stateKey === 'org_per_job_only'
    );
    const isStale = stateKey === 'stale';
    window.fetch = ((input: RequestInfo | URL): Promise<Response> => {
        const url = typeof input === 'string' ? input : input.toString();
        const json = (payload: unknown) => Promise.resolve(new Response(
            JSON.stringify(payload),
            {status: 200, headers: {'Content-Type': 'application/json'}},
        ));
        if (url.includes('/api/job-matches/status/')) return json(status);
        if (url.includes('/api/job-matches/ranked/')) {
            const jobs = withResults ? PANEL_RANKED_JOBS.map((j) => (
                isStale
                    ? {...j, revision: {...(j.revision || {}), stale: true}}
                    : j
            )) : [];
            return json({
                job_matches: jobs,
                organization_matches: !withResults ? [] : stateKey === 'org_ties' ? PANEL_TIED_ORGS
                    : stateKey === 'org_per_job_only' ? PANEL_PER_JOB_ORGS : PANEL_RANKED_ORGS,
            });
        }
        // Nothing to poll in the fixture (issue #485): the panel never asks twice.
        if (url.includes('/api/job-matches/revision/')) {
            return json({result_generation: null, poll_after_seconds: null});
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
            const presetsData = document.getElementById('ranking-presets');
            const orgConfig = document.getElementById('organization-list-config');
            createRoot(orgContainer).render(
                <OrganizationList
                    organizations={organizations}
                    rankingPresets={presetsData?.textContent ? JSON.parse(presetsData.textContent) : undefined}
                    currentAlgorithmId={Number(orgConfig?.getAttribute('data-current-algorithm-id')) || null}
                    algorithmUrlTemplate={orgConfig?.getAttribute('data-algorithm-url-template') || undefined}
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
