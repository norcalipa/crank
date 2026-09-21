// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

/**
 * JobMatchPanel displays the user's job-match status with distinct empty-state
 * copy and recovery actions, or a compact match summary when matches exist,
 * or ranked matches with reasons and links when preference-grounded matches
 * are available.
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
    refreshing?: boolean;
    coverage?: {enabled_sources: number; failing_sources: number};
    active_constraints?: string[];
    inventory?: {active_listings: number; last_success_at: string | null; age_hours: number | null};
    relaxation_preview?: {field: string; label: string; added_count: number} | null;
}

interface RankedJobMatch {
    listing_id: number;
    title: string;
    employer_name: string;
    organization_id: number | null;
    organization_name: string;
    canonical_url: string;
    location_text: string;
    is_remote: boolean;
    score: number;
    reasons: string[];
}

interface RankedOrgMatch {
    organization_id: number;
    name: string;
    url: string;
    funding_round: string;
    rto_policy: string;
    score: number;
    reasons: string[];
}

interface RankedMatchesPayload {
    job_matches: RankedJobMatch[];
    organization_matches: RankedOrgMatch[];
}

type PanelPhase = 'loading' | 'error' | 'ready';

const ACTION_LABELS: Record<string, string> = {
    suggest_company: 'Suggest a company',
    help: 'View help',
    retry: 'Refresh',
    chat: 'Chat with the assistant',
    complete_profile: 'Complete your profile',
    explore_companies: 'Explore company rankings',
};

const ACTION_ICONS: Record<string, string> = {
    suggest_company: 'briefcase',
    help: 'help-circle',
    retry: 'refresh-cw',
    chat: 'message-circle',
    complete_profile: 'user',
    explore_companies: 'bar-chart-2',
};

/**
 * Inline stroke icons (feather-style, MIT) rendered with currentColor so the
 * panel never depends on an external icon font being loaded. The round-2
 * visual critique found the header refresh control rendering as an empty
 * outlined square when the webfont was unavailable; inline SVG keeps the
 * glyph visible in every markup path while the accessible name stays on the
 * owning control.
 */
const ICON_PATHS: Record<string, React.ReactNode> = {
    'refresh-cw': (
        <>
            <polyline points="23 4 23 10 17 10" />
            <polyline points="1 20 1 14 7 14" />
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
        </>
    ),
    spinner: (
        <>
            <circle cx="12" cy="12" r="10" opacity=".25" />
            <path d="M12 2a10 10 0 0 1 10 10" />
        </>
    ),
    'alert-triangle': (
        <>
            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
            <line x1="12" y1="9" x2="12" y2="13" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
        </>
    ),
    database: (
        <>
            <ellipse cx="12" cy="5" rx="9" ry="3" />
            <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
            <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
        </>
    ),
    'pause-circle': (
        <>
            <circle cx="12" cy="12" r="10" />
            <line x1="10" y1="15" x2="10" y2="9" />
            <line x1="14" y1="15" x2="14" y2="9" />
        </>
    ),
    clock: (
        <>
            <circle cx="12" cy="12" r="10" />
            <polyline points="12 6 12 12 16 14" />
        </>
    ),
    inbox: (
        <>
            <polyline points="22 12 16 12 14 15 10 15 8 12 2 12" />
            <path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
        </>
    ),
    clipboard: (
        <>
            <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
            <rect x="8" y="2" width="8" height="4" rx="1" ry="1" />
        </>
    ),
    search: (
        <>
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </>
    ),
    layers: (
        <>
            <polygon points="12 2 2 7 12 12 22 7 12 2" />
            <polyline points="2 17 12 22 22 17" />
            <polyline points="2 12 12 17 22 12" />
        </>
    ),
    info: (
        <>
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="16" x2="12" y2="12" />
            <line x1="12" y1="8" x2="12.01" y2="8" />
        </>
    ),
    'check-circle': (
        <>
            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
            <polyline points="22 4 12 14.01 9 11.01" />
        </>
    ),
    'message-circle': (
        <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
    ),
    briefcase: (
        <>
            <rect x="2" y="7" width="20" height="14" rx="2" ry="2" />
            <path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
        </>
    ),
    'help-circle': (
        <>
            <circle cx="12" cy="12" r="10" />
            <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
        </>
    ),
    user: (
        <>
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
            <circle cx="12" cy="7" r="4" />
        </>
    ),
    'bar-chart-2': (
        <>
            <line x1="18" y1="20" x2="18" y2="10" />
            <line x1="12" y1="20" x2="12" y2="4" />
            <line x1="6" y1="20" x2="6" y2="14" />
        </>
    ),
    'map-pin': (
        <>
            <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" />
            <circle cx="12" cy="10" r="3" />
        </>
    ),
    package: (
        <>
            <line x1="16.5" y1="9.4" x2="7.5" y2="4.21" />
            <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
            <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
            <line x1="12" y1="22.08" x2="12" y2="12" />
        </>
    ),
    shield: (
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    ),
    zap: (
        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    ),
    'arrow-right': (
        <>
            <line x1="5" y1="12" x2="19" y2="12" />
            <polyline points="12 5 19 12 12 19" />
        </>
    ),
};

interface IconProps {
    name: string;
    size?: number;
    className?: string;
}

const Icon: React.FC<IconProps> = ({name, size = 16, className = ''}) => (
    <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size}
         viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
         strokeLinecap="round" strokeLinejoin="round"
         className={`crank-icon${name === 'spinner' ? ' crank-icon-spin' : ''}${className ? ` ${className}` : ''}`}
         aria-hidden="true" focusable="false" data-icon={name}>
        {ICON_PATHS[name] || ICON_PATHS.info}
    </svg>
);

const RTO_LABELS: Record<string, string> = {
    R: 'Remote',
    H: 'Hybrid',
    O: 'In-office',
};

const FUNDING_LABELS: Record<string, string> = {
    S: 'Seed',
    A: 'Series A',
    B: 'Series B',
    C: 'Series C',
    D: 'Series D',
    E: 'Series E',
    F: 'Series F',
    X: 'Series G+',
    O: 'Other Private',
    P: 'Public',
};

function fundingLabel(code: string): string {
    return FUNDING_LABELS[code] || code || 'Unknown';
}

function rtoLabel(code: string): string {
    return RTO_LABELS[code] || code || 'Unknown';
}

function inventoryText(inventory: NonNullable<EmptyStatePayload['inventory']>): string {
    const parts: string[] = [`${inventory.active_listings} active listing${inventory.active_listings === 1 ? '' : 's'} checked`];
    if (inventory.last_success_at) {
        const refreshed = new Date(inventory.last_success_at);
        if (!isNaN(refreshed.getTime())) {
            parts.push(`inventory last refreshed ${refreshed.toLocaleDateString()}`);
        } else if (inventory.age_hours !== null && inventory.age_hours !== undefined) {
            parts.push(`inventory last refreshed ${inventory.age_hours}h ago`);
        }
    }
    return parts.join(' · ');
}

/** Refresh/coverage notices rendered *alongside* results, never instead. */
function ResultNotices({emptyState}: {emptyState: EmptyStatePayload}) {
    return (
        <>
            {emptyState.refreshing && (
                <div className="alert alert-info py-2 small d-flex align-items-start gap-2 mb-2"
                     role="status" aria-live="polite" data-testid="refresh-notice">
                    <Icon name="spinner" className="flex-shrink-0 mt-1" />
                    <span className="flex-grow-1">
                        A refresh is in progress — these are your current results; new listings may appear shortly.
                    </span>
                </div>
            )}
            {emptyState.state === 'partial_coverage' && emptyState.coverage && (
                <div className="alert alert-warning py-2 small mb-2"
                     role="status" aria-live="polite" data-testid="coverage-notice">
                    <div className="d-flex align-items-start gap-2">
                        <Icon name="alert-triangle" className="flex-shrink-0 mt-1" />
                        <span className="flex-grow-1">
                            Coverage is limited: {emptyState.coverage.failing_sources} of
                            {' '}{emptyState.coverage.enabled_sources} job sources aren’t returning
                            listings right now, so some openings may be missing.
                        </span>
                    </div>
                    {emptyState.inventory && (
                        <div className="text-muted mt-1 ms-4">{inventoryText(emptyState.inventory)}</div>
                    )}
                </div>
            )}
        </>
    );
}

export interface JobMatchPanelProps {
    // Hydrated server-side (issue #465): /chat/ is now reachable by
    // anonymous visitors, and every /api/job-matches/* endpoint below is
    // @login_required, so an anonymous fetch would 302 to the login page
    // and this panel's res.json() would throw on the returned HTML. Gate
    // the fetch entirely instead of surfacing that as a generic error.
    isAuthenticated?: boolean;
    signInUrl?: string;
}

const JobMatchPanel: React.FC<JobMatchPanelProps> = ({isAuthenticated = true, signInUrl = '/accounts/login/'}) => {
    const [phase, setPhase] = React.useState<PanelPhase>('loading');
    const [emptyState, setEmptyState] = React.useState<EmptyStatePayload | null>(null);
    const [matchCount, setMatchCount] = React.useState<number>(0);
    const [rankedMatches, setRankedMatches] = React.useState<RankedMatchesPayload | null>(null);
    const [errorMsg, setErrorMsg] = React.useState<string | null>(null);

    const fetchStatus = React.useCallback(async () => {
        if (!isAuthenticated) return;
        setPhase('loading');
        setErrorMsg(null);
        try {
            const [statusRes, matchRes, rankedRes] = await Promise.all([
                fetch('/api/job-matches/status/'),
                fetch('/api/job-matches/?page=1&page_size=1'),
                fetch('/api/job-matches/ranked/?limit=10'),
            ]);
            if (!statusRes.ok) throw new Error(`Status ${statusRes.status}`);
            const statusData: EmptyStatePayload = await statusRes.json();
            setEmptyState(statusData);

            if (matchRes.ok) {
                const matchData = await matchRes.json();
                setMatchCount(matchData.count || 0);
            }
            if (rankedRes.ok) {
                const rankedData: RankedMatchesPayload = await rankedRes.json();
                setRankedMatches(rankedData);
            } else {
                setRankedMatches(null);
            }
            setPhase('ready');
        } catch (e) {
            setErrorMsg(e instanceof Error ? e.message : 'Could not load job match status.');
            setPhase('error');
        }
    }, [isAuthenticated]);

    React.useEffect(() => {
        fetchStatus();
    }, [fetchStatus]);

    const handleAction = React.useCallback((action: string) => {
        switch (action) {
            case 'retry':
                fetchStatus();
                break;
            case 'suggest_company': {
                window.dispatchEvent(new CustomEvent('crank:suggest-company'));
                break;
            }
            case 'help':
                window.location.href = '/help/';
                break;
            case 'chat': {
                window.dispatchEvent(new CustomEvent('crank:assistant-open', {
                    detail: {surface: 'jobs'},
                }));
                break;
            }
            case 'complete_profile': {
                window.dispatchEvent(new CustomEvent('crank:assistant-open', {
                    detail: {surface: 'jobs'},
                }));
                break;
            }
            case 'explore_companies': {
                window.location.href = '/';
                break;
            }
            default:
                break;
        }
    }, [fetchStatus]);

    if (!isAuthenticated) {
        return (
            <section className="card bg-dark mb-3" data-bs-theme="dark" data-testid="job-match-panel"
                     aria-labelledby="job-match-panel-title">
                <div className="card-header">
                    <h2 id="job-match-panel-title" className="h6 mb-0">Your Job Matches</h2>
                </div>
                <div className="card-body">
                    <p className="text-muted mb-2" data-testid="job-match-signed-out">
                        Sign in to see personalized job matches based on your saved preferences.
                    </p>
                    <a href={signInUrl} className="btn btn-primary btn-sm" data-testid="job-match-sign-in-cta">
                        Sign in to save your search
                    </a>
                </div>
            </section>
        );
    }

    if (phase === 'loading') {
        return (
            <section className="card bg-dark mb-3" data-bs-theme="dark" data-testid="job-match-panel"
                     aria-labelledby="job-match-panel-title">
                <div className="card-header">
                    <h2 id="job-match-panel-title" className="h6 mb-0">Your Job Matches</h2>
                </div>
                <div className="card-body">
                    <p className="text-muted mb-0" role="status" aria-live="polite"
                       data-testid="job-match-loading">
                        <Icon name="spinner" className="me-1" />
                        Loading your match status…
                    </p>
                </div>
            </section>
        );
    }

    if (phase === 'error') {
        return (
            <section className="card bg-dark mb-3" data-bs-theme="dark" data-testid="job-match-panel"
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
                                <Icon name="refresh-cw" className="me-1" />Retry
                            </button>
                        </div>
                    </div>
                </div>
            </section>
        );
    }

    // Ready phase: show ranked matches if available, else empty state or match count
    const hasRankedMatches = rankedMatches && (
        (rankedMatches.job_matches && rankedMatches.job_matches.length > 0) ||
        (rankedMatches.organization_matches && rankedMatches.organization_matches.length > 0)
    );
    const hasMatches = emptyState && emptyState.state === 'ok' && matchCount > 0;

    if (hasRankedMatches) {
        const jobs = rankedMatches!.job_matches || [];
        const orgs = rankedMatches!.organization_matches || [];
        return (
            <section className="card bg-dark mb-3" data-bs-theme="dark" data-testid="job-match-panel"
                     aria-labelledby="job-match-panel-title">
                <div className="card-header d-flex justify-content-between align-items-center">
                    <h2 id="job-match-panel-title" className="h6 mb-0">Your Job Matches</h2>
                    <button type="button" className="btn btn-sm btn-outline-light"
                            onClick={fetchStatus} aria-label="Refresh match status"
                            data-testid="job-match-refresh">
                        <Icon name="refresh-cw" />
                    </button>
                </div>
                <div className="card-body">
                    <ResultNotices emptyState={emptyState!} />
                    {jobs.length > 0 && (
                        <div data-testid="ranked-job-matches" className="mb-3">
                            <h3 className="h6 mb-2">Ranked Job Listings</h3>
                            {jobs.map((match) => (
                                <div key={match.listing_id} className="border-bottom border-secondary pb-2 mb-2"
                                     data-testid={`ranked-job-${match.listing_id}`}>
                                    <div className="d-flex justify-content-between align-items-start">
                                        <div className="flex-grow-1">
                                            <a href={match.canonical_url} target="_blank" rel="noopener noreferrer"
                                               className="text-info text-decoration-none fw-bold">
                                                {match.title}
                                            </a>
                                            <span className="text-muted ms-2">{match.employer_name}</span>
                                        </div>
                                        <span className="badge bg-primary" data-testid={`job-score-${match.listing_id}`}>
                                            {match.score.toFixed(1)}
                                        </span>
                                    </div>
                                    {match.location_text && (
                                        <small className="text-muted d-block">
                                            <Icon name="map-pin" className="me-1" />{match.location_text}
                                            {match.is_remote && <span className="badge bg-success ms-1">Remote</span>}
                                        </small>
                                    )}
                                    {match.reasons.length > 0 && (
                                        <div className="mt-1" data-testid={`job-reasons-${match.listing_id}`}>
                                            {match.reasons.map((reason, idx) => (
                                                <span key={idx} className="badge bg-secondary me-1 mb-1 small">
                                                    {reason}
                                                </span>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            ))}
                        </div>
                    )}
                    {orgs.length > 0 && (
                        <div data-testid="ranked-org-matches">
                            <h3 className="h6 mb-2">Ranked Organizations</h3>
                            {orgs.map((org) => (
                                <div key={org.organization_id} className="border-bottom border-secondary pb-2 mb-2"
                                     data-testid={`ranked-org-${org.organization_id}`}>
                                    <div className="d-flex justify-content-between align-items-start">
                                        <div className="flex-grow-1">
                                            {org.url ? (
                                                <a href={org.url} target="_blank" rel="noopener noreferrer"
                                                   className="text-info text-decoration-none fw-bold">
                                                    {org.name}
                                                </a>
                                            ) : (
                                                <span className="fw-bold">{org.name}</span>
                                            )}
                                            <span className="text-muted ms-2">{fundingLabel(org.funding_round)}</span>
                                            <span className="text-muted ms-1">· {rtoLabel(org.rto_policy)}</span>
                                        </div>
                                        <span className="badge bg-primary" data-testid={`org-score-${org.organization_id}`}>
                                            {org.score.toFixed(1)}
                                        </span>
                                    </div>
                                    {org.reasons.length > 0 && (
                                        <div className="mt-1" data-testid={`org-reasons-${org.organization_id}`}>
                                            {org.reasons.map((reason, idx) => (
                                                <span key={idx} className="badge bg-secondary me-1 mb-1 small">
                                                    {reason}
                                                </span>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </section>
        );
    }

    if (hasMatches) {
        return (
            <section className="card bg-dark mb-3" data-bs-theme="dark" data-testid="job-match-panel"
                     aria-labelledby="job-match-panel-title">
                <div className="card-header d-flex justify-content-between align-items-center">
                    <h2 id="job-match-panel-title" className="h6 mb-0">Your Job Matches</h2>
                    <button type="button" className="btn btn-sm btn-outline-light"
                            onClick={fetchStatus} aria-label="Refresh match status"
                            data-testid="job-match-refresh">
                        <Icon name="refresh-cw" />
                    </button>
                </div>
                <div className="card-body">
                    <ResultNotices emptyState={emptyState!} />
                    <p className="mb-0" role="status" aria-live="polite">
                        <Icon name="check-circle" className="text-success me-1" />
                        You have <strong>{matchCount}</strong> job match{matchCount === 1 ? '' : 'es'} ready to review.
                    </p>
                </div>
            </section>
        );
    }

    // Empty state
    const state = emptyState!;
    const stateIcons: Record<string, string> = {
        no_source: 'database',
        source_disabled: 'pause-circle',
        crawl_running: 'spinner',
        crawl_failed: 'alert-triangle',
        crawl_stale: 'clock',
        crawl_empty: 'inbox',
        no_preferences: 'clipboard',
        no_matches: 'search',
        partial_coverage: 'layers',
    };
    const icon = stateIcons[state.state] || 'info';

    return (
        <section className="card bg-dark mb-3" data-bs-theme="dark" data-testid="job-match-panel"
                 aria-labelledby="job-match-panel-title">
            <div className="card-header d-flex justify-content-between align-items-center">
                <h2 id="job-match-panel-title" className="h6 mb-0">Your Job Matches</h2>
                <button type="button" className="btn btn-sm btn-outline-light"
                        onClick={fetchStatus} aria-label="Refresh match status"
                        data-testid="job-match-refresh">
                    <Icon name="refresh-cw" />
                </button>
            </div>
            <div className="card-body">
                <ResultNotices emptyState={state} />
                <div className="d-flex align-items-start mb-2" role="status" aria-live="polite"
                     data-testid={`empty-state-${state.state}`}>
                    <Icon name={icon} size={20} className="me-3 mt-1 text-info" />
                    <div className="flex-grow-1">
                        <h3 className="h6 mb-1">{state.title}</h3>
                        <p className="text-muted mb-0">{state.message}</p>
                        {state.staff_detail && (
                            <p className="text-muted small mt-2 mb-0" data-testid="staff-detail">
                                <Icon name="shield" className="me-1" />
                                {state.staff_detail}
                            </p>
                        )}
                        {state.active_constraints && state.active_constraints.length > 0 && (
                            <div className="mt-2" data-testid="active-constraints">
                                <h4 className="small text-muted mb-1">Your active requirements</h4>
                                <div className="d-flex flex-wrap gap-2">
                                    {state.active_constraints.map((constraint, idx) => (
                                        <span key={idx} className="badge bg-secondary fw-normal px-2 py-1">
                                            {constraint}
                                        </span>
                                    ))}
                                </div>
                            </div>
                        )}
                        {state.inventory && (
                            <p className="text-muted small mt-2 mb-0" data-testid="inventory-facts">
                                <Icon name="package" className="me-1" />
                                {inventoryText(state.inventory)}
                            </p>
                        )}
                        {state.relaxation_preview && (
                            <div className="alert alert-info small mt-2 mb-0" role="status" aria-live="polite"
                                 data-testid="relaxation-preview">
                                <div className="d-flex align-items-start gap-2">
                                    <Icon name="zap" className="flex-shrink-0 mt-1" />
                                    <span className="flex-grow-1">
                                        {state.relaxation_preview.label} would surface
                                        {' '}<strong>{state.relaxation_preview.added_count}</strong> more
                                        listing{state.relaxation_preview.added_count === 1 ? '' : 's'}.
                                    </span>
                                </div>
                            </div>
                        )}
                    </div>
                </div>
                {state.actions.length > 0 && (
                    <div className="job-match-actions d-flex flex-wrap gap-2 mt-3" role="group"
                         aria-label="Recovery actions">
                        {state.actions.map((action, idx) => (
                            <button key={action} type="button"
                                    className={`btn ${idx === 0 ? 'btn-primary' : 'btn-outline-info'}`}
                                    onClick={() => handleAction(action)}
                                    data-testid={`action-${action}`}
                                    aria-label={ACTION_LABELS[action] || action}>
                                <Icon name={ACTION_ICONS[action] || 'arrow-right'} className="me-1" />
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

