// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {EvidenceStatusKey, FUNDING_ROUND_LABELS, RTO_POLICY_LABELS, fieldKeyLabel} from '../labels';
import EvidenceBadge from '../evidence/EvidenceBadge';
import EvidenceDetails, {EvidenceSkeleton} from '../evidence/EvidenceDetails';
import {openAssistant, setPrioritiesEditorOpen} from '../workspace/store';
import {prioritiesSurface} from '../priorities/surface';
import FitSection from './FitSection';
import JobsSection from './JobsSection';
import {ScoreDetail, useCompanyData} from './useCompanyData';

export interface CompanyDetailsOrganization {
    id: number;
    name: string;
    // The rank fields come from the ranked list. A company opened by id that
    // is not in the current list (an `?company=` link, the assistant) has none
    // of them and says so instead of inventing a rank (issue #486).
    ranking?: number;
    avg_score?: number;
    funding_round: string;
    rto_policy: string;
    profile_completeness?: number;
    rating_dimensions_covered?: number;
    rating_dimensions_total?: number;
    accelerated_vesting: boolean;
    url?: string;
    type?: string;
    gives_ratings?: boolean;
    public?: boolean;
    avg_scores?: ScoreDetail[];
}

export type CorrectionSource = 'company_details' | 'company_evidence';

export interface CompanyDetailsProps {
    organization: CompanyDetailsOrganization;
    isAuthenticated?: boolean;
    // Server-built login URL carrying a validated `next` back to this page
    // with `?company=<id>` (issue #465 AC-7). Contains
    // COMPANY_ID_PLACEHOLDER where the organization id goes; see
    // crank/views/index.py. Empty when the server emitted none, which
    // simply hides the signed-out CTA rather than inventing a URL here.
    signInUrlTemplate?: string;
    // "Ask the assistant about <company>" was activated with a plain click on
    // a page that hosts the assistant. The default navigation is already
    // prevented; the container opens the assistant.
    onAsk: () => void;
    // One of the correction triggers was activated; `fieldKey` is set by the
    // per-field ones. The container opens the correction form.
    onSuggestCorrection: (source: CorrectionSource, fieldKey?: string) => void;
}


//: Must match crank.views.index.COMPANY_ID_PLACEHOLDER.
export const COMPANY_ID_PLACEHOLDER = '__COMPANY_ID__';

/**
 * Resolve the server's sign-in URL template for one organization.
 *
 * The only client-side contribution is the integer id: the path, the `next`
 * parameter and its encoding all come from `crank.auth.sign_in_url`, so the
 * open-redirect guard has already vetted the shape. Returns null when there
 * is no usable template.
 */
export function companySignInUrl(template: string | undefined, companyId: number): string | null {
    if (!template || !template.includes(COMPANY_ID_PLACEHOLDER)) return null;
    return template.split(COMPANY_ID_PLACEHOLDER).join(String(companyId));
}

// A name longer than this is not repeated in an action label: five wrapped
// lines of the same name make the button harder to read than the generic words.
export const ACTION_NAME_MAX = 40;

/** The words that stand for the company in an action label. */
export function actionTarget(name: string): string {
    return name.length > ACTION_NAME_MAX ? 'this company' : name;
}

/** The chat route scoped to one company; the /chat/ view validates the id. */
export function companyChatUrl(companyId: number): string {
    return `/chat/?company=${companyId}`;
}

const formatRelativeTime = (isoString: string | null): string => {
    if (!isoString) return 'Unknown';
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
    if (diffDays < 1) return 'Today';
    if (diffDays < 7) return `${diffDays} day${diffDays > 1 ? 's' : ''} ago`;
    if (diffDays < 30) {
        const weeks = Math.floor(diffDays / 7);
        return `${weeks} week${weeks > 1 ? 's' : ''} ago`;
    }
    if (diffDays < 365) {
        const months = Math.floor(diffDays / 30);
        return `${months} month${months > 1 ? 's' : ''} ago`;
    }
    const years = Math.floor(diffDays / 365);
    return `${years} year${years > 1 ? 's' : ''} ago`;
};

const formatDate = (isoString: string | null): string => {
    if (!isoString) return 'Unknown';
    return new Date(isoString).toLocaleDateString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric'
    });
};

/**
 * The content of the company details: the assistant handoff, the profile and
 * score card, and "Data Freshness & Sources". It is not a dialog — no portal,
 * no overlay, no background lock, no focus trap — so the surface that shows it
 * decides how it is presented (issue #486).
 */
const CompanyDetails: React.FC<CompanyDetailsProps> = ({
    organization,
    isAuthenticated = false,
    signInUrlTemplate = '',
    onAsk,
    onSuggestCorrection,
}) => {
    const {
        scores, scoresLoading: loading, provenance, provenanceLoading, retryProvenance, retryFailures,
        pendingCorrections,
    } = useCompanyData(organization.id, isAuthenticated, {skipScores: Boolean(organization.avg_scores)});
    const freshnessHeadingRef = React.useRef<HTMLHeadingElement>(null);
    const retryRef = React.useRef<HTMLButtonElement>(null);
    const jobsHeadingRef = React.useRef<HTMLHeadingElement>(null);
    const signInUrl = companySignInUrl(signInUrlTemplate, organization.id);

    const fitHeadingRef = React.useRef<HTMLHeadingElement>(null);
    // The heading the last jump moved focus to, while the reader has not moved
    // the page themselves since: a late answer that changes the height above it
    // must not carry it off the screen (never when the reader scrolled).
    const jump = React.useRef<{heading: HTMLElement; stop: () => void} | null>(null);
    const stopJump = React.useCallback(() => {
        jump.current?.stop();
        jump.current = null;
    }, []);
    React.useEffect(() => stopJump, [stopJump]);

    // "View jobs" / "See your fit": the heading takes focus and scrolls into view.
    const jumpTo = (heading: HTMLHeadingElement | null) => {
        if (!heading) return;
        stopJump();
        heading.focus({preventScroll: true});
        heading.scrollIntoView({block: 'start'});
        const moved = () => stopJump();
        const SCROLL_KEYS = ['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '];
        const onKey = (event: KeyboardEvent) => {
            if (SCROLL_KEYS.includes(event.key)) moved();
        };
        const events = ['wheel', 'touchmove', 'pointerdown'] as const;
        events.forEach(name => window.addEventListener(name, moved, {passive: true}));
        window.addEventListener('keydown', onKey);
        heading.addEventListener('blur', moved);
        jump.current = {
            heading,
            stop: () => {
                events.forEach(name => window.removeEventListener(name, moved));
                window.removeEventListener('keydown', onKey);
                heading.removeEventListener('blur', moved);
            },
        };
    };
    const viewJobs = () => jumpTo(jobsHeadingRef.current);
    const viewFit = () => jumpTo(fitHeadingRef.current);
    const keepJumpHeadingInView = React.useCallback(() => {
        const current = jump.current;
        if (current && document.activeElement === current.heading) current.heading.scrollIntoView({block: 'start'});
    }, []);

    // The editor lives in the assistant panel on this page; open both.
    const editPriorities = () => {
        setPrioritiesEditorOpen(prioritiesSurface());
        openAssistant();
    };

    // A retry the user started that fails again hands focus back to Retry (never on the first failure).
    React.useEffect(() => {
        if (retryFailures > 0) retryRef.current?.focus();
    }, [retryFailures]);

    const fieldCorrectionButton = (
        fieldKey: string, verb = 'Suggest a correction', joiner = 'to'
    ) => isAuthenticated && (
        <button type="button" className="btn btn-link p-0 suggest-correction-field correction-action"
                data-testid={`suggest-correction-field-${fieldKey}`}
                onClick={() => onSuggestCorrection('company_evidence', fieldKey)}>
            <i className="fa-solid fa-pen-to-square" aria-hidden="true"></i>
            {verb}<span className="visually-hidden"> {joiner} {fieldKeyLabel(fieldKey)}</span>
        </button>
    );

    const emptyAction = isAuthenticated ? (
        <button type="button" className="btn btn-sm btn-primary evidence-empty-action"
                data-testid="evidence-empty-action"
                onClick={() => onSuggestCorrection('company_details')}>
            Suggest a value
        </button>
    ) : (
        companySignInUrl(signInUrlTemplate, organization.id) && (
            <a href={companySignInUrl(signInUrlTemplate, organization.id) as string}
               className="btn btn-sm btn-primary evidence-empty-action" data-testid="evidence-empty-action">
                Sign in to suggest a value
            </a>
        )
    );

    // The grid shows profile columns; evidence rows are a separate record (#477),
    // so a field's evidence status describes the shown value only when the
    // accepted evidence value is that value. Otherwise the grid says so.
    const clip = (text: string, max = 60) => (text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text);
    const profileFieldBadges = (
        key: 'funding_round' | 'rto_policy' | 'accelerated_vesting', label: string, shownValue: string | undefined,
    ) => {
        if (!provenance) return null;
        const row = provenance.fields?.find(item => item.field_key === key);
        // The server decides agreement (strict whole-value readings), but it
        // computed it against the profile as of the provenance fetch. Honor it
        // only when that profile value is the one this grid renders; an open tab
        // whose list went stale then falls back to the neutral note.
        const sameProfileValue = shownValue !== undefined && provenance.displayed_values?.[key] === shownValue;
        const agreeing = row?.agrees_with_displayed === true && sameProfileValue ? row : undefined;
        const status: EvidenceStatusKey = agreeing ? (agreeing.status || (agreeing.stale ? 'stale' : 'verified')) : 'profile';
        const review = provenance.review_by_field?.[key] ?? null;
        return (
            <>
                {' '}
                <EvidenceBadge status={status} fieldLabel={label} lastVerifiedAt={agreeing?.last_verified_at}/>
                {review && <>{' '}<EvidenceBadge status={review}/></>}
                {row && !agreeing && (
                    <span className="d-block small text-muted" data-testid={`profile-differs-${key}`}>
                        Sourced value: “{clip(row.value)}” (see Field evidence)
                    </span>
                )}
            </>
        );
    };

    // Funding-round and RTO words are shared by every surface (issue #473).
    const fundingRoundMap = FUNDING_ROUND_LABELS;
    const rtoPolicyMap = RTO_POLICY_LABELS;

    // Map organization type codes to display names
    const typeMap: Record<string, string> = {
        'C': 'Company (for profit)',
        'N': 'Non-Profit Organization'
    };

    const displayScores = organization.avg_scores || scores;

    const ranked = organization.ranking !== undefined && organization.avg_score !== undefined;
    const ratingCoverage = organization.rating_dimensions_total !== undefined
        && organization.rating_dimensions_covered !== undefined
        ? {covered: organization.rating_dimensions_covered, total: organization.rating_dimensions_total}
        : null;

    // Modified clicks and pages without the workspace host keep the plain
    // /chat/?company= navigation; everything else is the container's to
    // handle in place (issue #479).
    const handleAskClick = (e: React.MouseEvent) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey
            || !document.getElementById('assistant-workspace')) {
            return;
        }
        e.preventDefault();
        e.stopPropagation();
        onAsk();
    };

    return (
        <div className="card-body">
            {/* The company handoff's initiating control (issue #465
                AC-7). Signed out, it carries the visitor through
                sign-in and back to this same dialog via the
                server-validated `next`; signed in, it starts the
                assistant conversation scoped to this company. Without
                it the handoff had only a receiving half —
                OrganizationList.openCompanyFromUrl() was unreachable
                through the UI. */}
            <div className="mb-3 popup-company-actions" data-testid="company-handoff-actions">
                {isAuthenticated ? (
                    <a href={companyChatUrl(organization.id)}
                       onClick={handleAskClick}
                       className="btn btn-primary"
                       data-testid="company-chat-cta">
                        Ask the assistant about {actionTarget(organization.name)}
                    </a>
                ) : (
                    companySignInUrl(signInUrlTemplate, organization.id) && (
                        <a href={companySignInUrl(signInUrlTemplate, organization.id) as string}
                           className="btn btn-primary"
                           data-testid="company-sign-in-cta">
                            Sign in to ask about {actionTarget(organization.name)}
                        </a>
                    )
                )}
                <div className="popup-company-jumps">
                    <button type="button" className="btn btn-outline-light" data-testid="company-see-fit"
                            onClick={viewFit}>
                        See your fit
                    </button>
                    <button type="button" className="btn btn-outline-light" data-testid="company-view-jobs"
                            onClick={viewJobs}>
                        View jobs
                    </button>
                </div>
            </div>
            <div className="popup-details-grid" data-testid="popup-details-grid">
                <div className="popup-details-profile">
                    <div className="row mb-3">
                        <div className="col-5 text-end fw-bold">URL:</div>
                        <div className="col-7">
                            {organization.url && <a href={organization.url} target="_blank" rel="noopener noreferrer">{organization.url}</a>}
                        </div>
                    </div>
                    <div className="row mb-3">
                        <div className="col-5 text-end fw-bold">Type:</div>
                        <div className="col-7">{organization.type && typeMap[organization.type]}</div>
                    </div>
                    <div className="row mb-3">
                        <div className="col-5 text-end fw-bold">Funding Round:</div>
                        <div className="col-7">{fundingRoundMap[organization.funding_round]}{profileFieldBadges('funding_round', 'Funding Round', fundingRoundMap[organization.funding_round])}</div>
                    </div>
                    <div className="row mb-3">
                        <div className="col-5 text-end fw-bold">RTO Policy:</div>
                        <div className="col-7">{rtoPolicyMap[organization.rto_policy]}{profileFieldBadges('rto_policy', 'RTO Policy', rtoPolicyMap[organization.rto_policy])}</div>
                    </div>
                    {organization.gives_ratings !== undefined && (
                        <div className="row mb-3">
                            <div className="col-5 text-end fw-bold">Gives Ratings:</div>
                            <div className="col-7">{organization.gives_ratings ? 'Yes' : 'No'}</div>
                        </div>
                    )}
                    {organization.accelerated_vesting !== undefined && (
                        <div className="row mb-3">
                            <div className="col-5 text-end fw-bold">Accelerated Vesting:</div>
                            <div className="col-7">
                                {organization.accelerated_vesting ? 'Yes' : 'No'}
                                {profileFieldBadges('accelerated_vesting', 'Accelerated Vesting', organization.accelerated_vesting ? 'Yes' : 'No')}
                            </div>
                        </div>
                    )}
                </div>
                <div className="popup-details-scores" data-testid="popup-details-score-card">
                    {ranked ? (
                        <>
                            <div className="popup-details-score-row">
                                <span className="fw-bold">Rank:</span>
                                <span>{organization.ranking}</span>
                            </div>
                            <div className="popup-details-score-row">
                                <span className="fw-bold">Rating coverage:</span>
                                <span data-testid="rating-coverage">
                                    {ratingCoverage
                                        ? `${ratingCoverage.covered} of ${ratingCoverage.total} rating dimensions`
                                        : `${(organization.profile_completeness ?? 0).toFixed(0)}% of rating dimensions`}
                                </span>
                            </div>
                        </>
                    ) : (
                        <p className="popup-details-not-ranked mb-2" data-testid="not-ranked">
                            Not ranked in this list
                        </p>
                    )}
                    {loading ? (
                        <table className="table table-dark" aria-busy="true" data-testid="scores-loading">
                            <tbody>
                                {Array.from({length: Math.max(1, ratingCoverage?.covered ?? 1)}, (_, i) => (
                                    <tr key={i}>
                                        <td className="w-75 text-body-secondary">{i === 0 ? 'Loading scores...' : '\u00a0'}</td>
                                        <td className="text-end w-25">{'\u00a0'}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    ) : (
                        <table className="table table-dark">
                            <tbody>
                                {displayScores && displayScores.length > 0 ? (
                                    displayScores.map((score, index) => (
                                        <tr key={index}>
                                            <td className="w-75">{score.type__name}</td>
                                            <td className="text-end w-25">{score.avg_score.toFixed(2)}</td>
                                        </tr>
                                    ))
                                ) : organization.avg_score !== undefined ? (
                                    <tr>
                                        <td className="w-75">Overall Score</td>
                                        <td className="text-end w-25">{organization.avg_score.toFixed(2)}</td>
                                    </tr>
                                ) : (
                                    <tr>
                                        <td className="w-75 text-muted" data-testid="no-scores">No scores recorded</td>
                                    </tr>
                                )}
                            </tbody>
                        </table>
                    )}
                </div>
            </div>

            <FitSection ref={fitHeadingRef} organizationId={organization.id} name={organization.name}
                        isAuthenticated={isAuthenticated} signInUrl={signInUrl} onEditPriorities={editPriorities}
                        onSettled={keepJumpHeadingInView}/>
            <JobsSection ref={jobsHeadingRef} organizationId={organization.id} name={organization.name}
                         isAuthenticated={isAuthenticated} signInUrl={signInUrl}/>

            {/* Data Provenance & Freshness Section */}
            <hr className="my-3" />
            <div className="row">
                <div className="col-12">
                    <h3 ref={freshnessHeadingRef} tabIndex={-1} className="h5 mt-3 mb-2 company-jump-heading">Data Freshness & Sources</h3>
                    {provenanceLoading ? (
                        <EvidenceSkeleton/>
                    ) : provenance ? (
                        <div data-testid="provenance-section">
                            <div className="row mb-2">
                                <div className="col-5 text-end fw-bold">Record last edited:</div>
                                <div className="col-7">
                                    <span data-testid="last-updated">
                                        {provenance.organization_modified
                                            ? `${formatRelativeTime(provenance.organization_modified)} (${formatDate(provenance.organization_modified)})`
                                            : 'Unknown'}
                                    </span>
                                    <span className="d-block small text-muted" data-testid="last-updated-note">
                                        Editing the record does not re-verify facts.
                                    </span>
                                </div>
                            </div>
                            <div className="row mb-2">
                                <div className="col-5 text-end fw-bold">Added to Catalog:</div>
                                <div className="col-7" data-testid="added-to-catalog">
                                    {formatDate(provenance.organization_created)}
                                </div>
                            </div>
                            {provenance.latest_observation ? (
                                <div data-testid="observation-details">
                                    <div className="row mb-2">
                                        <div className="col-5 text-end fw-bold">Observed Source:</div>
                                        <div className="col-7">
                                            {provenance.latest_observation.observed_domain || 'Unknown domain'}
                                        </div>
                                    </div>
                                    <div className="row mb-2">
                                        <div className="col-5 text-end fw-bold">Last Observed:</div>
                                        <div className="col-7">
                                            {formatRelativeTime(provenance.latest_observation.observed_at)}{' '}
                                            ({formatDate(provenance.latest_observation.observed_at)})
                                        </div>
                                    </div>
                                    <div className="row mb-2">
                                        <div className="col-5 text-end fw-bold">Extraction Version:</div>
                                        <div className="col-7">{provenance.latest_observation.extraction_version || 'Unknown'}</div>
                                    </div>
                                    <div className="row mb-2">
                                        <div className="col-5 text-end fw-bold">Observation Status:</div>
                                        <div className="col-7">{provenance.latest_observation.status || 'Unknown'}</div>
                                    </div>
                                    {provenance.latest_observation.is_verified === false && (
                                        <div className="row mb-2">
                                            <div className="col-5 text-end fw-bold">Verification:</div>
                                            <div className="col-7 text-muted" data-testid="observation-not-verified">
                                                Not accepted evidence — shown for inspection only.
                                            </div>
                                        </div>
                                    )}
                                </div>
                            ) : (
                                <div className="row mb-2">
                                    <p className="col-7 offset-5 text-muted small mb-0" data-testid="no-observation">
                                        No crawl observations recorded.
                                        {(provenance.fields?.length ?? 0) === 0 && ' Data is curated from submitted reviews.'}
                                    </p>
                                </div>
                            )}
                            <EvidenceDetails
                                evidence={provenance}
                                renderFieldAction={fieldCorrectionButton}
                                emptyAction={emptyAction}
                            />
                            {isAuthenticated && (provenance.fields?.length ?? 0) > 0 && (
                                <div className="mt-3 pt-3 border-top" data-testid="correction-action">
                                    <button type="button"
                                            className="btn btn-sm btn-outline-light"
                                            data-testid="suggest-correction-link"
                                            onClick={() => onSuggestCorrection('company_details')}>
                                        <i className="fa-solid fa-pen-to-square me-1" aria-hidden="true"></i>
                                        Choose a field to correct
                                    </button>
                                </div>
                            )}
                            {isAuthenticated && pendingCorrections.length > 0 && (
                                <div className="mt-3" data-testid="your-pending-corrections">
                                    <h4 className="h6 fw-semibold mb-2">Your pending suggestions</h4>
                                    <ul className="list-unstyled small mb-0">
                                        {pendingCorrections.map(item => (
                                            <li key={item.id} className="d-flex flex-wrap align-items-center gap-2 mb-1"
                                                data-testid={`your-pending-correction-${item.id}`}>
                                                <span className="fw-bold">{fieldKeyLabel(item.field_key)}:</span>
                                                <span>{item.proposed_value}</span>
                                                <EvidenceBadge status="pending"/>
                                            </li>
                                        ))}
                                    </ul>
                                </div>
                            )}
                        </div>
                    ) : (
                        <div className="alert alert-warning evidence-unavailable mb-0" role="alert" data-testid="provenance-unavailable">
                            <p className="mb-2">Evidence unavailable — try again</p>
                            <button type="button" ref={retryRef} className="btn btn-outline-light evidence-retry"
                                    data-testid="provenance-retry"
                                    onClick={() => {
                                        freshnessHeadingRef.current?.focus({preventScroll: true});
                                        retryProvenance();
                                    }}>
                                Retry
                            </button>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};

export default CompanyDetails;
