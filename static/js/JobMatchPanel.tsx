// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

/**
 * JobMatchPanel displays the user's job-match status with distinct empty-state
 * copy and recovery actions, or a compact match summary when matches exist.
 *
 * The state model is shared with the chat backend via /api/job-matches/status/
 * so wording is consistent across surfaces.
 */

interface EmptyStatePayload {
    state: string;
    title: string;
    message: string;
    actions: string[];
    staff_detail?: string;
}

interface MatchSummary {
    count: number;
}

type PanelPhase = 'loading' | 'error' | 'ready';

function getCookie(name: string): string {
    const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match[2]) : '';
}

const ACTION_LABELS: Record<string, string> = {
    suggest_company: 'Suggest a company',
    help: 'View help',
    retry: 'Refresh',
    chat: 'Chat with the assistant',
    complete_profile: 'Complete your profile',
};

const ACTION_ICONS: Record<string, string> = {
    suggest_company: 'fa-solid fa-building',
    help: 'fa-solid fa-circle-question',
    retry: 'fa-solid fa-rotate',
    chat: 'fa-solid fa-comments',
    complete_profile: 'fa-solid fa-user-pen',
};

const JobMatchPanel: React.FC = () => {
    const [phase, setPhase] = React.useState<PanelPhase>('loading');
    const [emptyState, setEmptyState] = React.useState<EmptyStatePayload | null>(null);
    const [matchCount, setMatchCount] = React.useState<number>(0);
    const [errorMsg, setErrorMsg] = React.useState<string | null>(null);

    const fetchStatus = React.useCallback(async () => {
        setPhase('loading');
        setErrorMsg(null);
        try {
            const [statusRes, matchRes] = await Promise.all([
                fetch('/api/job-matches/status/'),
                fetch('/api/job-matches/?page=1&page_size=1'),
            ]);
            if (!statusRes.ok) throw new Error(`Status ${statusRes.status}`);
            const statusData: EmptyStatePayload = await statusRes.json();
            setEmptyState(statusData);

            if (matchRes.ok) {
                const matchData = await matchRes.json();
                setMatchCount(matchData.count || 0);
            }
            setPhase('ready');
        } catch (e) {
            setErrorMsg(e instanceof Error ? e.message : 'Could not load job match status.');
            setPhase('error');
        }
    }, []);

    React.useEffect(() => {
        fetchStatus();
    }, [fetchStatus]);

    const handleAction = React.useCallback((action: string) => {
        switch (action) {
            case 'retry':
                fetchStatus();
                break;
            case 'suggest_company': {
                // Dispatch a custom event that the SuggestCompanyModal can listen for,
                // or navigate to the home page where the modal lives.
                window.dispatchEvent(new CustomEvent('crank:suggest-company'));
                break;
            }
            case 'help':
                window.location.href = '/help/';
                break;
            case 'chat': {
                const input = document.querySelector<HTMLInputElement>('input[aria-label="Message"]');
                if (input) {
                    input.focus();
                    input.scrollIntoView({behavior: 'smooth', block: 'center'});
                }
                break;
            }
            case 'complete_profile': {
                const input = document.querySelector<HTMLInputElement>('input[aria-label="Message"]');
                if (input) {
                    input.focus();
                    input.scrollIntoView({behavior: 'smooth', block: 'center'});
                }
                break;
            }
            default:
                // Unknown action; no-op.
                break;
        }
    }, [fetchStatus]);

    if (phase === 'loading') {
        return (
            <section className="card bg-dark mb-3" data-testid="job-match-panel"
                     aria-labelledby="job-match-panel-title">
                <div className="card-header">
                    <h2 id="job-match-panel-title" className="h6 mb-0">Your Job Matches</h2>
                </div>
                <div className="card-body">
                    <p className="text-muted mb-0" role="status" aria-live="polite"
                       data-testid="job-match-loading">
                        <i className="fa-solid fa-spinner fa-spin me-1"></i>
                        Loading your match status…
                    </p>
                </div>
            </section>
        );
    }

    if (phase === 'error') {
        return (
            <section className="card bg-dark mb-3" data-testid="job-match-panel"
                     aria-labelledby="job-match-panel-title">
                <div className="card-header">
                    <h2 id="job-match-panel-title" className="h6 mb-0">Your Job Matches</h2>
                </div>
                <div className="card-body">
                    <div className="alert alert-danger" role="alert" data-testid="job-match-error">
                        {errorMsg || 'Could not load job match status.'}
                        <div className="mt-2">
                            <button type="button" className="btn btn-sm btn-primary"
                                    onClick={fetchStatus} aria-label="Retry loading match status">
                                <i className="fa-solid fa-rotate me-1"></i>Retry
                            </button>
                        </div>
                    </div>
                </div>
            </section>
        );
    }

    // Ready phase: show empty state or match summary
    const hasMatches = emptyState && emptyState.state === 'ok' && matchCount > 0;

    if (hasMatches) {
        return (
            <section className="card bg-dark mb-3" data-testid="job-match-panel"
                     aria-labelledby="job-match-panel-title">
                <div className="card-header d-flex justify-content-between align-items-center">
                    <h2 id="job-match-panel-title" className="h6 mb-0">Your Job Matches</h2>
                    <button type="button" className="btn btn-sm btn-outline-light"
                            onClick={fetchStatus} aria-label="Refresh match status"
                            data-testid="job-match-refresh">
                        <i className="fa-solid fa-rotate"></i>
                    </button>
                </div>
                <div className="card-body">
                    <p className="mb-0" role="status" aria-live="polite">
                        <i className="fa-solid fa-circle-check text-success me-1"></i>
                        You have <strong>{matchCount}</strong> job match{matchCount === 1 ? '' : 'es'} ready to review.
                    </p>
                </div>
            </section>
        );
    }

    // Empty state
    const state = emptyState!;
    const stateIcons: Record<string, string> = {
        no_source: 'fa-solid fa-database',
        source_disabled: 'fa-solid fa-pause-circle',
        crawl_running: 'fa-solid fa-spinner fa-spin',
        crawl_failed: 'fa-solid fa-triangle-exclamation',
        crawl_stale: 'fa-solid fa-clock',
        crawl_empty: 'fa-solid fa-inbox',
        no_preferences: 'fa-solid fa-clipboard-list',
        no_matches: 'fa-solid fa-magnifying-glass',
    };
    const icon = stateIcons[state.state] || 'fa-solid fa-circle-info';

    return (
        <section className="card bg-dark mb-3" data-testid="job-match-panel"
                 aria-labelledby="job-match-panel-title">
            <div className="card-header d-flex justify-content-between align-items-center">
                <h2 id="job-match-panel-title" className="h6 mb-0">Your Job Matches</h2>
                <button type="button" className="btn btn-sm btn-outline-light"
                        onClick={fetchStatus} aria-label="Refresh match status"
                        data-testid="job-match-refresh">
                    <i className="fa-solid fa-rotate"></i>
                </button>
            </div>
            <div className="card-body">
                <div className="d-flex align-items-start mb-2" data-testid={`empty-state-${state.state}`}>
                    <i className={`${icon} fa-lg me-3 mt-1 text-info`} aria-hidden="true"></i>
                    <div className="flex-grow-1">
                        <h3 className="h6 mb-1">{state.title}</h3>
                        <p className="text-muted mb-0">{state.message}</p>
                        {state.staff_detail && (
                            <p className="text-muted small mt-2 mb-0" data-testid="staff-detail">
                                <i className="fa-solid fa-shield-halved me-1"></i>
                                {state.staff_detail}
                            </p>
                        )}
                    </div>
                </div>
                {state.actions.length > 0 && (
                    <div className="d-flex flex-wrap gap-2 mt-3" role="group" aria-label="Recovery actions">
                        {state.actions.map((action) => (
                            <button key={action} type="button"
                                    className="btn btn-sm btn-outline-info"
                                    onClick={() => handleAction(action)}
                                    data-testid={`action-${action}`}
                                    aria-label={ACTION_LABELS[action] || action}>
                                <i className={`${ACTION_ICONS[action] || 'fa-solid fa-arrow-right'} me-1`}></i>
                                {ACTION_LABELS[action] || action}
                            </button>
                        ))}
                    </div>
                )}
            </div>
        </section>
    );
};

export default JobMatchPanel;

document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('job-match-panel');
    if (container) {
        const root = require('react-dom/client').createRoot(container);
        root.render(<JobMatchPanel/>);
    }
});
