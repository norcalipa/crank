// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import EvidenceBadge from './evidence/EvidenceBadge';
import {EVIDENCE_CHANGED_MARKER, EVIDENCE_STATUS_META, MATCH_TERMS, REQUIREMENT_MARKS, formatEvidenceDate, fundingRoundLabel, rtoPolicyLabel} from './labels';
import {csrfFetch} from './priorities/csrf';
import {installPositionTracking, restoreResultPosition} from './workspace/position';
import {createLatestGuard} from './workspace/requests';
import {
    getWorkspaceSnapshot,
    setPrioritiesEditorOpen,
    setWorkspaceContext,
    subscribeWorkspace,
} from './workspace/store';

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

interface RequirementOutcome {
    path: string;
    status: 'match' | 'mismatch' | 'unknown';
    observed?: string | number | null;
    source_kind?: string | null;
    source_id?: string | number | null;
    // Read-time status of the backing fact (issue #473). Absent on listing
    // data and on payloads cached before the field existed.
    evidence_status?: RequirementEvidenceStatus | null;
}

interface RequirementEvidenceStatus {
    state: 'verified' | 'sourced' | 'stale' | 'profile' | 'superseded' | 'missing';
    last_verified_at: string | null;
    source_domain: string | null;
}

interface RevisionBlock {
    preference_revision?: number | null;
    ranking_version?: string;
    data_revision?: number | null;
    generated_at?: string | null;
    stale?: boolean;
    // Monotonic result-list generation (issue #475): a lower value than the
    // one on screen is a late response and never replaces it (issue #479).
    // Null for pre-generation users, which are never guarded.
    result_generation?: number | null;
}

// Opens the contextual correction form (issue #477) through the cross-bundle
// `crank:suggest-company` channel: this bundle has no host of its own.
function SuggestCorrectionButton({organizationId, companyName}: {organizationId: number; companyName: string}) {
    return (
        <button type="button" className="btn btn-link p-0 correction-action job-match-correction"
                data-testid={`suggest-correction-org-${organizationId}`}
                onClick={() => window.dispatchEvent(new CustomEvent('crank:suggest-company', {
                    detail: {kind: 'correction', source: 'job_results', organizationId, companyName},
                }))}>
            <i className="fa-solid fa-pen-to-square" aria-hidden="true"></i>
            Suggest a correction<span className="visually-hidden"> for {companyName}</span>
        </button>
    );
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
    fit_score?: number | null;
    company_score?: number | null;
    coverage?: number;
    requirements?: RequirementOutcome[];
    unsupported?: string[];
    revision?: RevisionBlock | null;
}

interface RankedOrgMatch {
    organization_id: number;
    name: string;
    url: string;
    funding_round: string;
    rto_policy: string;
    score: number;
    reasons: string[];
    fit_score?: number | null;
    company_score?: number | null;
    coverage?: number;
    requirements?: RequirementOutcome[];
    unsupported?: string[];
    revision?: RevisionBlock | null;
}

interface RankedMatchesPayload {
    job_matches: RankedJobMatch[];
    organization_matches: RankedOrgMatch[];
}

// Generations carried by the JOB entries of the payload that will be
// displayed. Organization entries never carry one (OrgMatchResult.revision()
// has no result_generation) and live job results carry null, so only the
// generations that are actually present guard anything.
function jobGenerations(payload: RankedMatchesPayload | null): number[] {
    return Array.from(new Set(
        (payload?.job_matches ?? [])
            .map((entry) => entry.revision?.result_generation)
            .filter((value): value is number => typeof value === 'number'),
    ));
}

// Generation of the list actually shown: the ranked job entries' own when
// present, else (no job entries at all) the `/api/job-matches/` page's.
// Null when the payload carries none (org-only, live results).
function displayedGeneration(payload: RankedMatchesPayload | null, matchGeneration: number | null): number | null {
    const values = jobGenerations(payload);
    if (values.length > 0) {
        return Math.max(...values);
    }
    return (payload?.job_matches?.length ?? 0) === 0 ? matchGeneration : null;
}

// A refresh may replace the displayed list unless it is provably older: a
// failed ranked fetch, job entries that disagree with each other or carry a
// lower generation, a zero-job payload whose matches page is older, or a
// ranked/matches endpoint mismatch keep the list on screen. Payloads that
// carry no generation (org-only, live) are unguarded.
function acceptsGeneration(
    shown: number | null,
    payload: RankedMatchesPayload | null,
    matchGeneration: number | null,
): boolean {
    if (shown === null) {
        return true;
    }
    if (payload === null) {
        return false;
    }
    const values = jobGenerations(payload);
    if (values.length > 1) {
        return false;
    }
    if (values.length === 1) {
        return values[0] >= shown && (matchGeneration === null || matchGeneration === values[0]);
    }
    return matchGeneration === null || matchGeneration >= shown;
}

type PanelPhase = 'loading' | 'error' | 'ready';

/**
 * Parse a JSON response, translating the raw ``res.json()`` DOMException
 * ("Failed to execute 'json' on 'Response'...") that a login redirect or HTML
 * error page produces into truthful, user-facing copy. Only the JSON-parse
 * failure is rewritten: status-code and network errors keep their readable
 * message so the retry path stays informative.
 */
async function parseJson<T>(res: Response): Promise<T> {
    try {
        return await res.json();
    } catch {
        throw new Error('We couldn’t load your job matches. Please try again.');
    }
}

const ACTION_LABELS: Record<string, string> = {
    suggest_company: 'Suggest a company',
    help: 'View help',
    retry: 'Refresh',
    chat: 'Chat with the assistant',
    complete_profile: 'Complete your profile',
    explore_companies: 'Explore company rankings',
    edit_priorities: 'Edit your priorities',
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

const REQUIREMENT_LABELS: Record<string, string> = {
    'compensation.minimum_salary': 'Minimum salary',
    'compensation.equity_minimum_percent': 'Equity',
    'compensation.require_public_company': 'Public company',
    'work_location.modes': 'Work mode',
    'work_location.countries': 'Country',
    'work_location.max_in_office_days': 'In-office days',
    'geography.regions': 'Region',
    'geography.remote_friendly': 'Remote-friendly',
    'industry': 'Industry',
    'funding_stage': 'Funding stage',
    'culture': 'Culture',
    'vesting.max_cliff_months': 'Cliff',
    'vesting.max_vesting_months': 'Vesting length',
    'vesting.prefer_accelerated': 'Accelerated vesting',
    // Criteria the engine registers UNSUPPORTED (crank.services.preferences
    // CRITERION_SUPPORT). These must never leak internal field names into the
    // UI, so each gets a human label even though it is only ever surfaced in
    // the unsupported notice.
    'compensation.minimum_total_compensation': 'Minimum total compensation',
    'compensation.equity_liquidity_required': 'Equity liquidity requirement',
    'compensation.acceptable_liquidity_events': 'Acceptable liquidity events',
    'compensation.basis': 'Salary basis',
    'compensation.period': 'Pay period',
    'work_location.require_onsite': 'On-site requirement',
    'work_location.office_days_exact': 'Exact in-office days',
    'roles.families': 'Role families',
    'roles.titles': 'Job titles',
    'roles.seniority': 'Seniority',
    'notes': 'Notes',
    'importance': 'Requirement importance',
    'scope.countries': 'Country scope',
    'scope.role_families': 'Role family scope',
};

function requirementLabel(path: string): string {
    return REQUIREMENT_LABELS[path] || path.split('.').pop() || path;
}

/** Three separately labelled figures: company score (/5), fit (/100), and
 * coverage (%) (AC-10). Round-3: explicit scales so each number reads on its
 * own, and a three-column grid with legible xs labels + sm semibold values. */
function ThreeFigures({fit, company, coverage, scope}: {fit: number | null | undefined; company: number | null | undefined; coverage: number | null | undefined; scope: string}) {
    const covText = coverage == null ? '—' : `${Math.round(coverage * 100)}%`;
    const fitText = fit == null ? '—' : `${fit.toFixed(1)} / 100`;
    const companyText = company == null ? '—' : `${company.toFixed(1)} / 5`;
    return (
        <div className="job-match-figures mt-1" role="list" aria-label="Match figures">
            <span role="listitem" className="job-match-figure">
                <span className="job-match-figure-label" title={MATCH_TERMS.companyScore.meaning}>{MATCH_TERMS.companyScore.label}</span>
                <strong className="job-match-figure-value" data-testid={`${scope}-company-score`}>{companyText}</strong>
            </span>
            <span role="listitem" className="job-match-figure">
                <span className="job-match-figure-label" title={MATCH_TERMS.fit.meaning}>{MATCH_TERMS.fit.label}</span>
                <strong className="job-match-figure-value" data-testid={`${scope}-fit-score`}>{fitText}</strong>
            </span>
            <span role="listitem" className="job-match-figure">
                <span className="job-match-figure-label" title={MATCH_TERMS.requirementCoverage.meaning}>{MATCH_TERMS.requirementCoverage.label}</span>
                <strong className="job-match-figure-value" data-testid={`${scope}-coverage`}>{covText}</strong>
            </span>
        </div>
    );
}

// Three semantically distinct chip states (issue #467 round-2): match is
// green/emerald, mismatch is rose, and unknown is amber. Unknown must never
// share the neutral gray metadata treatment — it is a distinct outcome, not
// missing data. Colors live in popup.css (scoped, contrast-checked) so the
// component keeps a single source of truth for the palette.
const REQUIREMENT_STATUS_META: Record<RequirementOutcome['status'], {marker: string; word: string; className: string}> = {
    match: {marker: REQUIREMENT_MARKS.match.marker, word: 'match', className: 'job-match-chip job-match-chip--match'},
    mismatch: {marker: REQUIREMENT_MARKS.mismatch.marker, word: 'mismatch', className: 'job-match-chip job-match-chip--mismatch'},
    unknown: {marker: REQUIREMENT_MARKS.unknown.marker, word: 'unknown', className: 'job-match-chip job-match-chip--unknown'},
};

const evidenceChanged = (req: RequirementOutcome): boolean =>
    req.evidence_status?.state === 'superseded' || req.evidence_status?.state === 'missing';

const lastVerifiedText = (iso: string | null, long = false): string =>
    iso ? `last verified ${formatEvidenceDate(iso, long)}` : 'never verified';

/** The evidence qualifier after a chip's label (issue #473): what backs the
 * outcome, in the shared vocabulary. `spoken` extends the chip's accessible
 * name; `unverified` switches the chip off the solid outcome style, so a
 * stale- or prose-backed match never looks like a verified one. */
function chipQualifier(req: RequirementOutcome): {node: React.ReactNode; spoken: string; unverified: boolean} | null {
    const evidence = req.evidence_status;
    if (!evidence) return null;
    if (evidence.state === 'stale') {
        return {
            node: <><EvidenceBadge status="stale"/> ({lastVerifiedText(evidence.last_verified_at)})</>,
            spoken: `${EVIDENCE_STATUS_META.stale.label}, ${lastVerifiedText(evidence.last_verified_at, true)}`,
            unverified: true,
        };
    }
    // A requirement the engine could not decide has no fact to qualify.
    if (req.status === 'unknown') return null;
    if (evidence.state === 'verified') {
        return {node: <EvidenceBadge status="verified"/>, spoken: EVIDENCE_STATUS_META.verified.label, unverified: false};
    }
    if (evidence.state === 'profile') {
        return {node: <EvidenceBadge status="profile"/>, spoken: EVIDENCE_STATUS_META.profile.label, unverified: true};
    }
    return {
        node: <span className="job-match-chip-qualifier-text" title={MATCH_TERMS.sourced.meaning}>{MATCH_TERMS.sourced.label}</span>,
        spoken: MATCH_TERMS.sourced.label,
        unverified: true,
    };
}

/** Per-requirement chips in three visually distinct, non-color-duplicated
 * states, each qualified by the read-time status of the fact behind it. */
function RequirementChips({requirements}: {requirements?: RequirementOutcome[]}) {
    if (!requirements || requirements.length === 0) {
        return null;
    }
    return (
        <div className="job-match-chips mt-1" role="list" aria-label="Requirement outcomes">
            {requirements.map((req, idx) => {
                const label = requirementLabel(req.path);
                if (evidenceChanged(req)) {
                    // The stored outcome rests on a fact that was replaced or
                    // removed, so it is shown as neither match nor mismatch.
                    return (
                        <span key={idx} role="listitem"
                              className="job-match-chip job-match-chip--changed me-1 mb-1 small fw-normal"
                              data-testid={`requirement-${req.path}`}
                              data-status={req.status}
                              data-evidence-state={req.evidence_status!.state}
                              aria-label={`${label}: evidence changed, refresh matches`}>
                            <span aria-hidden="true">{EVIDENCE_CHANGED_MARKER}</span> {label}
                            <span className="job-match-chip-sep" aria-hidden="true"> · </span>
                            <span className="job-match-chip-qualifier" title={MATCH_TERMS.changed.meaning}>
                                {MATCH_TERMS.changed.label} — refresh
                            </span>
                        </span>
                    );
                }
                const meta = REQUIREMENT_STATUS_META[req.status] || REQUIREMENT_STATUS_META.unknown;
                const qualifier = chipQualifier(req);
                return (
                    <span key={idx} role="listitem"
                          className={`${meta.className}${qualifier?.unverified ? ' job-match-chip--unverified' : ''} me-1 mb-1 small fw-normal`}
                          data-testid={`requirement-${req.path}`}
                          data-status={req.status}
                          data-evidence-state={req.evidence_status?.state}
                          aria-label={`${label}: ${meta.word}${qualifier ? `, ${qualifier.spoken}` : ''}`}>
                        <span aria-hidden="true">{meta.marker}</span> {label}
                        {qualifier && <>
                            <span className="job-match-chip-sep" aria-hidden="true"> · </span>
                            <span className="job-match-chip-qualifier">{qualifier.node}</span>
                        </>}
                    </span>
                );
            })}
        </div>
    );
}

const anyEvidenceChanged = (matches: {requirements?: RequirementOutcome[]}[]): boolean =>
    matches.some((match) => (match.requirements || []).some(evidenceChanged));

type RecheckState =
    | {kind: 'idle'}
    | {kind: 'running'; wasPaused: boolean}
    | {kind: 'paused'}
    | {kind: 'limited'; retryAfter: number | null}
    | {kind: 'failed'};
const RECHECK_IDLE: RecheckState = {kind: 'idle'};

interface RecheckAnswer {
    state: RecheckState;
    /** True when the answer can have changed what the read endpoints return. */
    reread: boolean;
    /** Spoken once the notice is gone, when nothing visible says what happened. */
    announcement: string;
}

const RECHECK_FAILED: RecheckAnswer = {state: {kind: 'failed'}, reread: false, announcement: ''};

/** Ask the server to re-check the stored matches and classify its answer
 * (issue #473): refreshed, nothing to refresh, paused by the operator,
 * asked again too soon, or failed. */
async function requestRecheck(): Promise<RecheckAnswer> {
    let response: Response;
    let body: {status?: unknown; retry_after?: unknown} | null = null;
    try {
        response = await csrfFetch('/api/job-matches/refresh/', {method: 'POST'});
        body = await response.json().catch(() => null);
    } catch {
        return RECHECK_FAILED;
    }
    if (response.status === 429) {
        const seconds = Number(body?.retry_after ?? response.headers.get('Retry-After'));
        return {
            state: {kind: 'limited', retryAfter: Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : null},
            reread: false, announcement: '',
        };
    }
    const status = response.ok ? body?.status : 'failed';
    if (status === 'disabled') {
        return {state: {kind: 'paused'}, reread: false, announcement: ''};
    }
    if (status === 'published' || status === 'current') {
        return {state: RECHECK_IDLE, reread: true, announcement: 'Matches re-checked.'};
    }
    if (status === 'not_needed') {
        return {state: RECHECK_IDLE, reread: true, announcement: 'Your matches were already up to date.'};
    }
    if (typeof status !== 'string' || status === 'failed') {
        return RECHECK_FAILED;
    }
    // Any other answer (the results were superseded while computing, or there
    // is nothing saved to compute from): show the state as it now is.
    return {state: RECHECK_IDLE, reread: true, announcement: ''};
}

const recheckSentence = (recheck: RecheckState): string => {
    switch (recheck.kind) {
        case 'running':
            return 'Re-checking your matches…';
        case 'paused':
            return 'Re-checking is paused right now, so results marked “Evidence changed” may be out of date.';
        case 'limited':
            return recheck.retryAfter
                ? `A re-check was just requested. Try again in about ${recheck.retryAfter} seconds.`
                : 'A re-check was just requested. Try again shortly.';
        case 'failed':
            return 'The re-check did not finish. Try again in a moment.';
        default:
            return 'Refresh to re-check them.';
    }
};

/** One consolidated notice when any shown outcome rests on evidence that has
 * since been replaced or removed (issue #473 AC9): the chips say which, and
 * this carries the >=44px refresh action. It stands in for the stale banner,
 * which the same change also raises. It stays mounted through a re-check so
 * its live region reports the outcome; while one runs the button is
 * `aria-disabled` (it keeps focus), and while re-checking is paused there is
 * no button, because pressing it could not succeed; the header's refresh
 * still asks again. */
function EvidenceChangedNotice({matches, recheck, onRefresh}: {
    matches: {requirements?: RequirementOutcome[]}[];
    recheck: RecheckState;
    onRefresh: (event: React.MouseEvent<HTMLElement>) => void;
}) {
    if (!anyEvidenceChanged(matches)) {
        return null;
    }
    const running = recheck.kind === 'running';
    return (
        <div className="alert alert-warning py-2 small mb-3" role="status" aria-live="polite" id="job-match-notice"
             data-testid="evidence-changed-notice" data-recheck={recheck.kind} aria-busy={running || undefined}>
            <div className="job-match-stale-banner">
                <Icon name="clock" className="job-match-stale-icon" />
                <span className="job-match-stale-message">
                    Some company evidence changed after these matches were computed.{' '}
                    {recheckSentence(recheck)}
                </span>
                {recheck.kind !== 'paused' && !(running && recheck.wasPaused) && (
                    <button type="button"
                            className={`btn btn-sm btn-outline-light job-match-stale-refresh${running ? ' job-match-refresh-busy' : ''}`}
                            onClick={onRefresh} aria-disabled={running || undefined}
                            data-testid="evidence-changed-refresh">
                        <Icon name={running ? 'spinner' : 'refresh-cw'} className="me-1" />
                        {running ? 'Re-checking…' : recheck.kind === 'failed' ? 'Try again' : 'Refresh matches'}
                    </button>
                )}
            </div>
        </div>
    );
}

const LEGEND_STATUSES = ['verified', 'stale', 'profile'] as const;
const LEGEND_MARKS = ['match', 'mismatch', 'unknown'] as const;

/** The job-card vocabulary in one place (issue #473): the same definitions
 * as "How ranking works" on the rankings page, which this page lacks. */
function MatchLegend() {
    return (
        <details className="how-ranking-works job-match-legend" data-testid="job-match-legend">
            <summary className="how-ranking-works-summary">What these labels mean</summary>
            <div className="how-ranking-works-content">
                <dl className="ranking-definitions mb-0">
                    {[MATCH_TERMS.companyScore, MATCH_TERMS.fit, MATCH_TERMS.requirementCoverage].map((term) => (
                        <React.Fragment key={term.label}>
                            <dt>{term.label}</dt>
                            <dd>{term.meaning}</dd>
                        </React.Fragment>
                    ))}
                    <dt>Requirement chips</dt>
                    <dd>
                        {/* Each mark is followed by its word; the mark alone is
                            decoration for assistive technology. */}
                        <ul className="evidence-legend list-unstyled mb-0" data-testid="requirement-mark-legend">
                            {LEGEND_MARKS.map((key) => (
                                <li key={key}>
                                    <span className="evidence-badge">
                                        <span className="evidence-badge-marker" aria-hidden="true">{REQUIREMENT_MARKS[key].marker}</span>
                                        {' '}{REQUIREMENT_MARKS[key].label}
                                    </span>: {REQUIREMENT_MARKS[key].meaning}
                                </li>
                            ))}
                        </ul>
                    </dd>
                    <dt>Evidence behind a requirement</dt>
                    <dd>
                        <ul className="evidence-legend list-unstyled mb-0">
                            {LEGEND_STATUSES.map((status) => (
                                <li key={status}><EvidenceBadge status={status}/>: {EVIDENCE_STATUS_META[status].meaning}</li>
                            ))}
                            <li><span className="evidence-badge">{MATCH_TERMS.sourced.label}</span>: {MATCH_TERMS.sourced.meaning}</li>
                            <li>
                                <span className="evidence-badge">
                                    <span className="evidence-badge-marker" aria-hidden="true">{EVIDENCE_CHANGED_MARKER}</span>
                                    {' '}{MATCH_TERMS.changed.label}
                                </span>: {MATCH_TERMS.changed.meaning}
                            </li>
                            <li><span className="evidence-badge">{MATCH_TERMS.unqualified.label}</span>: {MATCH_TERMS.unqualified.meaning}</li>
                        </ul>
                    </dd>
                </dl>
            </div>
        </details>
    );
}

/** A stale-result warning with a visible refresh action (issue #467 round-2).
 * Rendered ONCE above the result list — never repeated per card — so the
 * state reads as a single consolidated banner, not an icon-only action
 * duplicated on every listing. The "Refresh matches" control is a labelled
 * >=44px target. */
function StaleNotice({revision, onRefresh}: {revision?: RevisionBlock | null; onRefresh: () => void}) {
    if (!revision || !revision.stale) {
        return null;
    }
    return (
        <div className="alert alert-warning py-2 small mb-3" role="status" aria-live="polite" data-testid="stale-notice">
            <div className="job-match-stale-banner">
                <Icon name="clock" className="job-match-stale-icon" />
                <span className="job-match-stale-message">
                    These results are stale — refresh to recompute with your latest preferences.
                </span>
                <button type="button" className="btn btn-sm btn-outline-light job-match-stale-refresh" onClick={onRefresh}
                        aria-label="Refresh matches" data-testid="stale-refresh">
                    <Icon name="refresh-cw" className="me-1" />Refresh matches
                </button>
            </div>
        </div>
    );
}

/** Unsupported-criteria notice when the user set a criterion matching cannot
 * evaluate. Field paths are mapped to human labels (requirementLabel) so an
 * internal name like ``minimum_total_compensation`` never reaches the DOM. */
function UnsupportedNotice({unsupported}: {unsupported?: string[]}) {
    if (!unsupported || unsupported.length === 0) {
        return null;
    }
    const labels = unsupported.map(requirementLabel);
    const list = labels.length === 1
        ? labels[0]
        : `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`;
    return (
        <div className="alert alert-info py-2 small mb-3" role="status" aria-live="polite" data-testid="unsupported-notice">
            <div className="d-flex align-items-start gap-2">
                <Icon name="info" className="flex-shrink-0 mt-1" />
                <span className="flex-grow-1 text-start">
                    {list} could not be evaluated and {labels.length === 1 ? 'is' : 'are'} not included in these matches.
                </span>
            </div>
        </div>
    );
}

/** Results-generated timestamp (issue #467 round-2): a visible, readable
 * <time> line shown in both healthy and stale views so the user can see when
 * the matches were computed. */
function ResultTimestamp({revision}: {revision?: RevisionBlock | null}) {
    const iso = revision?.generated_at;
    if (!iso) {
        return null;
    }
    const d = new Date(iso);
    if (isNaN(d.getTime())) {
        return null;
    }
    return (
        <p className="text-muted small mb-3" data-testid="results-timestamp">
            Results generated at{' '}
            <time dateTime={iso}>{d.toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'})}</time>
        </p>
    );
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

// The preferences revision the user is looking at: the one this tab last saved
// (>= the stored rows' revision), else the stored rows' own while they are
// current. Stale rows with no known edit report nothing rather than a lagging value.
function currentPreferenceRevision(
    revision: {preference_revision?: number | null; stale?: boolean} | null,
): number | undefined {
    const stored = typeof revision?.preference_revision === 'number' ? revision.preference_revision : undefined;
    const saved = getWorkspaceSnapshot().prioritiesRevision;
    if (saved !== null && (stored === undefined || saved >= stored)) {
        return saved;
    }
    return revision?.stale === true ? undefined : stored;
}

const JobMatchPanel: React.FC<JobMatchPanelProps> = ({isAuthenticated = true, signInUrl = '/accounts/login/'}) => {
    const [phase, setPhase] = React.useState<PanelPhase>('loading');
    const [emptyState, setEmptyState] = React.useState<EmptyStatePayload | null>(null);
    const [matchCount, setMatchCount] = React.useState<number>(0);
    const [rankedMatches, setRankedMatches] = React.useState<RankedMatchesPayload | null>(null);
    const [errorMsg, setErrorMsg] = React.useState<string | null>(null);
    // Revision block of the persisted result list (/api/job-matches/), which is
    // the only surface that can flag a stale result (the ranked endpoint is
    // recomputed live and is never stale).
    const [storedRevision, setStoredRevision] = React.useState<RevisionBlock | null>(null);

    // Mirror the workspace assistant's open/closed state (issue #469
    // re-critique): while the panel is open the floating launcher leaves the
    // DOM, so the match-panel "chat" action relabels to a secondary
    // "Focus assistant" affordance instead of a redundant open action.
    const [assistantOpen, setAssistantOpen] = React.useState<boolean>(
        () => getWorkspaceSnapshot().visibility === 'open',
    );
    React.useEffect(() => subscribeWorkspace(() => {
        setAssistantOpen(getWorkspaceSnapshot().visibility === 'open');
    }), []);

    // Latest-request guard (issue #479): overlapping refreshes must never let
    // an older response overwrite a newer one, and a response whose
    // result_generation is lower than the one on screen is discarded.
    const guardRef = React.useRef(createLatestGuard());
    const shownGenerationRef = React.useRef<number | null>(null);

    // Account this panel fetches for. Starts at the server-rendered value and
    // is re-resolved from the next `crank:auth-hydrated` after a cross-tab
    // private-state purge, so a tab whose session another tab just ended shows
    // the signed-out state instead of a job-match error that can never clear.
    const [authed, setAuthed] = React.useState(isAuthenticated);
    React.useEffect(() => setAuthed(isAuthenticated), [isAuthenticated]);
    const awaitingHydrationRef = React.useRef(false);

    // `quiet` re-reads behind the list that is already shown (the evidence
    // re-check), instead of replacing it with the loading skeleton.
    const loadStatus = React.useCallback(async (quiet: boolean) => {
        if (!authed) return;
        const request = guardRef.current.begin();
        if (!quiet) {
            setPhase('loading');
        }
        setErrorMsg(null);
        try {
            const [statusRes, matchRes, rankedRes] = await Promise.all([
                fetch('/api/job-matches/status/', {signal: request.signal}),
                fetch('/api/job-matches/?page=1&page_size=1', {signal: request.signal}),
                fetch('/api/job-matches/ranked/?limit=10', {signal: request.signal}),
            ]);
            if (!statusRes.ok) {
                // Never expose raw implementation details (issue #467 round-2):
                // an HTTP status code reads as an internal error to a user, so
                // it is rewritten to actionable copy with a labelled retry.
                throw new Error('We couldn’t load your job matches. Please try again.');
            }
            const statusData: EmptyStatePayload = await parseJson(statusRes);
            let matchData: {count?: number; results?: RankedJobMatch[]} | null = null;
            if (matchRes.ok) {
                matchData = await parseJson<{count?: number; results?: RankedJobMatch[]}>(matchRes);
            }
            const rankedData: RankedMatchesPayload | null = rankedRes.ok
                ? await parseJson<RankedMatchesPayload>(rankedRes)
                : null;
            if (!request.isLatest()) {
                return;
            }
            const revision = matchData?.results?.[0]?.revision ?? null;
            if (!acceptsGeneration(shownGenerationRef.current, rankedData, revision?.result_generation ?? null)) {
                // Older, unverifiable or endpoint-mismatched result set:
                // keep what is shown.
                setPhase('ready');
                return;
            }
            const generation = displayedGeneration(rankedData, revision?.result_generation ?? null);
            setEmptyState(statusData);
            if (matchData) {
                setMatchCount(matchData.count || 0);
                setStoredRevision(revision);
            }
            if (generation !== null) {
                shownGenerationRef.current = generation;
            }
            setRankedMatches(rankedData);
            // Issue #484: the server compares these with its own revisions to
            // tell the assistant when the user's view is outdated. The revision
            // is the CURRENT preferences revision, not the one the stored rows
            // were computed for: those lag every edit while recompute is off.
            const reported: {resultGeneration?: number; preferenceRevision?: number} = {};
            if (generation !== null) {
                reported.resultGeneration = generation;
            }
            const preferenceRevision = currentPreferenceRevision(revision);
            if (preferenceRevision !== undefined) {
                reported.preferenceRevision = preferenceRevision;
            }
            if (Object.keys(reported).length > 0) {
                setWorkspaceContext(reported);
            }
            setPhase('ready');
        } catch (e) {
            if (!request.isLatest()) {
                return;
            }
            setErrorMsg(e instanceof Error ? e.message : 'Could not load job match status.');
            setPhase('error');
        }
    }, [authed]);
    const fetchStatus = React.useCallback(() => loadStatus(false), [loadStatus]);

    React.useEffect(() => {
        fetchStatus();
    }, [fetchStatus]);

    // A notice's Refresh unmounts with the list it reloads; focus goes to the
    // panel heading once the reload settles instead of dropping to <body>.
    const refocusRef = React.useRef(false);
    React.useEffect(() => {
        if (phase === 'loading' || !refocusRef.current) return;
        refocusRef.current = false;
        // Every phase renders the heading, so it is always there to take focus.
        document.getElementById('job-match-panel-title')!.focus();
    }, [phase]);
    const reloadFromNotice = React.useCallback(() => {
        refocusRef.current = true;
        void fetchStatus();
    }, [fetchStatus]);
    // Stored matches that cite replaced evidence are only resolved by a
    // recompute, so this asks for one and acts on the answer (issue #473).
    // The list stays on screen while it runs; only an answer that can have
    // changed the results is followed by a re-read.
    const [recheck, setRecheck] = React.useState<RecheckState>(RECHECK_IDLE);
    const [recheckAnnouncement, setRecheckAnnouncement] = React.useState('');
    const [recheckFocus, setRecheckFocus] = React.useState<{control: HTMLElement} | null>(null);
    const recheckRunningRef = React.useRef(false);
    const recheckMatches = React.useCallback(async (event: React.MouseEvent<HTMLElement>) => {
        // Single flight: a second press while one is running sends nothing.
        if (recheckRunningRef.current) return;
        recheckRunningRef.current = true;
        const control = event.currentTarget;
        setRecheck((current) => ({kind: 'running', wasPaused: current.kind === 'paused'}));
        setRecheckAnnouncement('');
        const answer = await requestRecheck();
        if (answer.reread) {
            await loadStatus(true);
        }
        recheckRunningRef.current = false;
        setRecheck(answer.state);
        setRecheckAnnouncement(answer.announcement);
        // The pressed control keeps focus unless the outcome removed it (the
        // notice clears, or its button goes while re-checking is paused);
        // focus then goes to the heading instead of dropping to <body>.
        setRecheckFocus({control});
    }, [loadStatus]);
    React.useEffect(() => {
        if (!recheckFocus) return;
        if (!recheckFocus.control.isConnected) {
            document.getElementById('job-match-panel-title')!.focus();
        }
    }, [recheckFocus]);
    // A read with no changed evidence closes the episode, so a later change
    // starts from the first sentence again.
    React.useEffect(() => {
        const shown = [...(rankedMatches?.job_matches || []), ...(rankedMatches?.organization_matches || [])];
        if (phase === 'ready' && !anyEvidenceChanged(shown)) {
            setRecheck((current) => (current.kind === 'running' ? current : RECHECK_IDLE));
        }
    }, [phase, rankedMatches]);

    // Issue #480: a priorities write anywhere on the page refetches matches once.
    const seenPrioritiesRevision = React.useRef(getWorkspaceSnapshot().prioritiesRevision);
    React.useEffect(() => subscribeWorkspace(() => {
        const revision = getWorkspaceSnapshot().prioritiesRevision;
        if (revision === null) {
            seenPrioritiesRevision.current = null;
        } else if (revision !== seenPrioritiesRevision.current) {
            seenPrioritiesRevision.current = revision;
            // Report the new revision now, not after the refetch, so a turn sent
            // in between is not judged against the pre-edit view.
            setWorkspaceContext({preferenceRevision: revision});
            void fetchStatus();
        }
    }), [fetchStatus]);

    React.useEffect(() => () => guardRef.current.cancel(), []);

    // Issue #479: report the jobs surface, track/restore the Back-navigation
    // scroll position, and drop everything on a private-state purge (sign-out
    // or account switch) so no match data outlives the account.
    React.useEffect(() => {
        setWorkspaceContext({surface: 'jobs'});
        return installPositionTracking(() => null);
    }, []);
    const restoredPositionRef = React.useRef(false);
    React.useEffect(() => {
        if (phase === 'ready' && !restoredPositionRef.current) {
            restoredPositionRef.current = true;
            restoreResultPosition(() => null);
        }
    }, [phase]);
    React.useEffect(() => {
        const handlePurged = () => {
            guardRef.current.cancel();
            shownGenerationRef.current = null;
            setEmptyState(null);
            setMatchCount(0);
            setRankedMatches(null);
            setStoredRevision(null);
            setPhase('loading');
            // Private re-fetches wait for the post-change hydration, which is
            // the first point the account the cookie belongs to is known.
            awaitingHydrationRef.current = true;
        };
        const handleHydrated = (event: Event) => {
            if (!awaitingHydrationRef.current) return;
            awaitingHydrationRef.current = false;
            const detail = (event as CustomEvent).detail as
                {authenticated?: boolean; unobserved?: boolean} | undefined;
            // A failed whoami says nothing about the account: keep the last
            // known one and retry the fetch.
            const next = detail && !detail.unobserved ? !!detail.authenticated : authed;
            if (next === authed) {
                void fetchStatus();
            } else {
                setAuthed(next);
            }
        };
        document.addEventListener('crank:private-state-purged', handlePurged);
        document.addEventListener('crank:auth-hydrated', handleHydrated);
        return () => {
            document.removeEventListener('crank:private-state-purged', handlePurged);
            document.removeEventListener('crank:auth-hydrated', handleHydrated);
        };
    }, [fetchStatus, authed]);

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
                // Explicit focus request (issue #469 review): when the panel
                // is already open this action degrades to "Focus assistant"
                // and must move keyboard focus to the assistant, not merely
                // re-notify its unchanged open state.
                window.dispatchEvent(new CustomEvent('crank:assistant-focus'));
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
            case 'edit_priorities':
                setPrioritiesEditorOpen('main');
                break;
            default:
                break;
        }
    }, [fetchStatus]);

    if (!authed) {
        return (
            <section className="card bg-dark mb-3" data-bs-theme="dark" data-testid="job-match-panel"
                     aria-labelledby="job-match-panel-title">
                <div className="card-header">
                    <h2 id="job-match-panel-title" className="job-match-panel-title mb-0" tabIndex={-1}>Your Job Matches</h2>
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
                    <h2 id="job-match-panel-title" className="job-match-panel-title mb-0" tabIndex={-1}>Your Job Matches</h2>
                </div>
                <div className="card-body">
                    <div role="status" aria-live="polite" data-testid="job-match-loading">
                        <div className="d-flex align-items-center gap-2 mb-3">
                            <Icon name="spinner" size={20} className="job-match-loading-spinner" />
                            <span className="job-match-loading-label">Loading your match status…</span>
                        </div>
                        <div className="job-match-skeleton" aria-hidden="true">
                            <div className="job-match-skeleton-row" />
                            <div className="job-match-skeleton-row" />
                            <div className="job-match-skeleton-row" />
                        </div>
                        <span className="visually-hidden">Loading your match status…</span>
                    </div>
                </div>
            </section>
        );
    }

    if (phase === 'error') {
        return (
            <section className="card bg-dark mb-3" data-bs-theme="dark" data-testid="job-match-panel"
                     aria-labelledby="job-match-panel-title">
                <div className="card-header">
                    <h2 id="job-match-panel-title" className="job-match-panel-title mb-0" tabIndex={-1}>Your Job Matches</h2>
                </div>
                <div className="card-body">
                    <div className="alert alert-danger" role="alert" data-testid="job-match-error">
                        {errorMsg || 'We couldn’t load your job matches. Please try again.'}
                        <div className="mt-2">
                            <button type="button" className="btn btn-sm btn-primary"
                                    onClick={fetchStatus} aria-label="Retry loading matches">
                                <Icon name="refresh-cw" className="me-1" />Try again
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
        // One consolidated stale flag and one generated-at timestamp for the
        // whole result set (a single computation stamps every row).
        const staleResults = [...jobs, ...orgs].some((m) => m.revision?.stale);
        const resultRevision = jobs[0]?.revision || orgs[0]?.revision || null;
        // An evidence change also makes the stored results stale; one notice
        // (the one naming the cause) is shown, never both.
        const evidenceChangedResults = anyEvidenceChanged([...jobs, ...orgs]);
        return (
            <section className="card bg-dark mb-3" data-bs-theme="dark" data-testid="job-match-panel"
                     aria-labelledby="job-match-panel-title">
                <div className="card-header d-flex justify-content-between align-items-center">
                    <h2 id="job-match-panel-title" className="job-match-panel-title mb-0" tabIndex={-1}
                        aria-describedby={evidenceChangedResults ? 'job-match-notice' : undefined}>Your Job Matches</h2>
                    {/* With the notice showing, this is the same re-check as
                        the notice's button, not a second kind of refresh. */}
                    <button type="button" className="btn btn-sm btn-outline-light"
                            onClick={evidenceChangedResults ? recheckMatches : fetchStatus}
                            aria-label="Refresh match status"
                            aria-disabled={recheck.kind === 'running' || undefined}
                            data-testid="job-match-refresh">
                        <Icon name="refresh-cw" />
                    </button>
                </div>
                <div className="card-body">
                    <span className="visually-hidden" role="status" aria-live="polite"
                          data-testid="recheck-announcement">{recheckAnnouncement}</span>
                    <ResultNotices emptyState={emptyState!} />
                    <UnsupportedNotice unsupported={jobs[0]?.unsupported || orgs[0]?.unsupported} />
                    <StaleNotice revision={{stale: staleResults && !evidenceChangedResults}} onRefresh={reloadFromNotice} />
                    <EvidenceChangedNotice matches={[...jobs, ...orgs]} recheck={recheck} onRefresh={recheckMatches} />
                    <MatchLegend />
                    <ResultTimestamp revision={resultRevision} />
                    {jobs.length > 0 && (
                        <div data-testid="ranked-job-matches" className="mb-3">
                            <h3 className="job-match-section-heading mb-2">Ranked Job Listings</h3>
                            {jobs.map((match) => (
                                <div key={match.listing_id} className="job-match-card border-bottom border-secondary pb-2 mb-2"
                                     data-testid={`ranked-job-${match.listing_id}`}>
                                    <div className="d-flex justify-content-between align-items-start gap-2">
                                        <div className="flex-grow-1 min-w-0">
                                            <a href={match.canonical_url} target="_blank" rel="noopener noreferrer"
                                               className="text-info fw-bold job-match-name"
                                               aria-label={`Open listing for ${match.title} (opens in a new tab)`}>
                                                {match.title}
                                            </a>
                                            <span className="text-muted d-block text-break">{match.employer_name}</span>
                                        </div>
                                    </div>
                                    {match.location_text && (
                                        <small className="text-muted d-block">
                                            <Icon name="map-pin" className="me-1" />{match.location_text}
                                            {match.is_remote && <span className="badge bg-secondary ms-1">Remote</span>}
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
                                    <ThreeFigures scope={`job-${match.listing_id}`} fit={match.fit_score ?? match.score} company={match.company_score} coverage={match.coverage} />
                                    <RequirementChips requirements={match.requirements} />
                                    {match.organization_id != null && (
                                        <SuggestCorrectionButton organizationId={match.organization_id}
                                                                 companyName={match.organization_name || match.employer_name} />
                                    )}
                                </div>
                            ))}
                        </div>
                    )}
                    {orgs.length > 0 && (
                        <div data-testid="ranked-org-matches">
                            <h3 className="job-match-section-heading mb-2">Ranked Organizations</h3>
                            {orgs.map((org) => (
                                <div key={org.organization_id} className="job-match-card border-bottom border-secondary pb-2 mb-2"
                                     data-testid={`ranked-org-${org.organization_id}`}>
                                    <div className="d-flex justify-content-between align-items-start gap-2">
                                        <div className="flex-grow-1 min-w-0">
                                            {org.url ? (
                                                <a href={org.url} target="_blank" rel="noopener noreferrer"
                                                   className="text-info fw-bold job-match-name"
                                                   aria-label={`Open ${org.name} (opens in a new tab)`}>
                                                    {org.name}
                                                </a>
                                            ) : (
                                                <span className="fw-bold">{org.name}</span>
                                            )}
                                            <div className="text-muted small text-break">
                                                {fundingRoundLabel(org.funding_round, 'Unknown')} · {rtoPolicyLabel(org.rto_policy, 'Unknown')}
                                            </div>
                                        </div>
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
                                    <ThreeFigures scope={`org-${org.organization_id}`} fit={org.fit_score ?? org.score} company={org.company_score} coverage={org.coverage} />
                                    <RequirementChips requirements={org.requirements} />
                                    <SuggestCorrectionButton organizationId={org.organization_id} companyName={org.name} />
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
                    <h2 id="job-match-panel-title" className="job-match-panel-title mb-0" tabIndex={-1}>Your Job Matches</h2>
                    <button type="button" className="btn btn-sm btn-outline-light"
                            onClick={fetchStatus} aria-label="Refresh match status"
                            data-testid="job-match-refresh">
                        <Icon name="refresh-cw" />
                    </button>
                </div>
                <div className="card-body">
                    <ResultNotices emptyState={emptyState!} />
                    <StaleNotice revision={storedRevision} onRefresh={reloadFromNotice} />
                    <ResultTimestamp revision={storedRevision} />
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

    return (
        <section className="card bg-dark mb-3" data-bs-theme="dark" data-testid="job-match-panel"
                 aria-labelledby="job-match-panel-title">
            <div className="card-header d-flex justify-content-between align-items-center">
                <h2 id="job-match-panel-title" className="job-match-panel-title mb-0" tabIndex={-1}>Your Job Matches</h2>
                <button type="button" className="btn btn-sm btn-outline-light"
                        onClick={fetchStatus} aria-label="Refresh match status"
                        data-testid="job-match-refresh">
                    <Icon name="refresh-cw" />
                </button>
            </div>
            <div className="card-body">
                <ResultNotices emptyState={state} />
                {/* Single-item message: plain block, no list-bullet marker or
                    leading icon indent (round-2 critique). */}
                <div role="status" aria-live="polite"
                     data-testid={`empty-state-${state.state}`}>
                    <h3 className="job-match-section-heading mb-1">{state.title}</h3>
                    <p className="text-muted small mb-0">{state.message}</p>
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
                {state.actions.length > 0 && (
                    <div className="job-match-actions" role="group"
                         aria-label="Recovery actions">
                        {state.actions.map((action, idx) => {
                            // While the assistant panel is open (issue #469
                            // re-critique) the "chat" action is no longer an
                            // open affordance — the launcher is gone from the
                            // DOM — so it degrades to a secondary
                            // "Focus assistant" action with a subtle
                            // outline/cyan-text treatment (still a >=44px
                            // target via the .job-match-actions .btn rule).
                            const focusAssistant = action === 'chat' && assistantOpen;
                            const label = focusAssistant
                                ? 'Focus assistant'
                                : (ACTION_LABELS[action] || action);
                            const className = focusAssistant
                                ? 'btn btn-outline-info job-match-focus-assistant'
                                : `btn ${idx === 0 ? 'btn-primary' : 'btn-outline-info'}`;
                            return (
                                <button key={action} type="button"
                                        className={className}
                                        onClick={() => handleAction(action)}
                                        data-testid={`action-${action}`}
                                        aria-label={label}>
                                    <Icon name={ACTION_ICONS[action] || 'arrow-right'} className="me-1" />
                                    {label}
                                </button>
                            );
                        })}
                    </div>
                )}
            </div>
        </section>
    );
};

export default JobMatchPanel;
