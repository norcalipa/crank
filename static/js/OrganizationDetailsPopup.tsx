// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createPortal} from 'react-dom';
import {lockBackground, unlockBackground} from './modalIsolation';
import {openSuggestCompany} from './suggestCompany/controller';
import {openAssistant} from './workspace/store';

interface ScoreDetail {
    type__name: string;
    avg_score: number;
}

interface ProvenanceObservation {
    source_url: string;
    observed_domain: string;
    observed_at: string;
    extraction_version: string;
    status: string;
    is_verified?: boolean;
}

interface ProvenanceFieldEvidence {
    field_key: string;
    state: string;
    value: string;
    source_domain: string;
    observed_at: string;
    scope: Record<string, unknown>;
    last_checked_at: string | null;
    last_successful_fetch_at: string | null;
    last_changed_at: string | null;
    last_verified_at: string | null;
    stale: boolean;
}

interface ProvenanceData {
    organization_id: number;
    organization_modified: string | null;
    organization_created: string | null;
    latest_observation: ProvenanceObservation | null;
    fields?: ProvenanceFieldEvidence[];
    unverified_fields?: string[];
}

interface Organization {
    id: number;
    name: string;
    ranking: number;
    avg_score: number;
    funding_round: string;
    rto_policy: string;
    profile_completeness: number;
    accelerated_vesting: boolean;
    url?: string;
    type?: string;
    gives_ratings?: boolean;
    public?: boolean;
    avg_scores?: ScoreDetail[];
}

interface OrganizationDetailsPopupProps {
    organization: Organization | null;
    visible: boolean;
    onClose: () => void;
    isAuthenticated?: boolean;
    // Server-built login URL carrying a validated `next` back to this page
    // with `?company=<id>` (issue #465 AC-7). Contains
    // COMPANY_ID_PLACEHOLDER where the organization id goes; see
    // crank/views/index.py. Empty when the server emitted none, which
    // simply hides the signed-out CTA rather than inventing a URL here.
    signInUrlTemplate?: string;
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

/** The chat route scoped to one company; the /chat/ view validates the id. */
export function companyChatUrl(companyId: number): string {
    return `/chat/?company=${companyId}`;
}

// Display labels for accepted field-level evidence keys (issue #460).
const fieldKeyMap: Record<string, string> = {
    rto_policy: 'RTO Policy',
    funding_round: 'Funding Round',
    public_status: 'Public Status',
    accelerated_vesting: 'Accelerated Vesting',
    locations: 'Locations',
    company_name: 'Company Name',
    company_domain: 'Company Domain',
};

const fieldKeyLabel = (key: string): string => fieldKeyMap[key] || key;

// Render an evidence scope (e.g. {"countries": [...], "roles": [...]}) as a
// short human-readable suffix; empty scope renders nothing.
const formatScope = (scope: Record<string, unknown>): string => {
    const parts: string[] = [];
    for (const [key, value] of Object.entries(scope)) {
        if (Array.isArray(value) && value.length > 0) {
            parts.push(`${key}: ${value.join(', ')}`);
        }
    }
    return parts.length > 0 ? ` (${parts.join('; ')})` : '';
};

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

const OrganizationDetailsPopup: React.FC<OrganizationDetailsPopupProps> = ({
    organization,
    visible,
    onClose,
    isAuthenticated = false,
    signInUrlTemplate = ''
}) => {
    const [scores, setScores] = React.useState<ScoreDetail[]>([]);
    const [loading, setLoading] = React.useState(false);
    const [provenance, setProvenance] = React.useState<ProvenanceData | null>(null);
    const [provenanceLoading, setProvenanceLoading] = React.useState(false);
    const closeButtonRef = React.useRef<HTMLButtonElement>(null);
    const dialogRef = React.useRef<HTMLDivElement>(null);
    // Element that had focus when the dialog opened (the trigger). Restored on
    // close so keyboard and pointer users return to where they left off.
    const openerRef = React.useRef<HTMLElement | null>(null);

    React.useEffect(() => {
        if (organization && visible && !organization.avg_scores) {
            setLoading(true);
            fetch(`/api/organizations/${organization.id}/scores/`)
                .then(response => response.json())
                .then(data => {
                    setScores(data);
                    setLoading(false);
                })
                .catch(error => {
                    console.error('Error fetching organization scores:', error);
                    setLoading(false);
                });
        }
    }, [organization, visible]);

    React.useEffect(() => {
        if (organization && visible) {
            setProvenanceLoading(true);
            fetch(`/api/organizations/${organization.id}/provenance/`)
                .then(response => response.json())
                .then(data => {
                    setProvenance(data);
                    setProvenanceLoading(false);
                })
                .catch(error => {
                    console.error('Error fetching organization provenance:', error);
                    setProvenanceLoading(false);
                });
        } else {
            setProvenance(null);
        }
    }, [organization, visible]);

    // Restore focus to the opener on close (WAI-ARIA dialog pattern). If the
    // opener is no longer in the document, defensively blur the active element
    // so focus never lingers on a now-hidden node. Uses the same
    // instanceof HTMLElement guard as the rest of this component.
    const restoreFocusToOpener = () => {
        if (openerRef.current instanceof HTMLElement && openerRef.current.isConnected) {
            openerRef.current.focus();
        } else if (document.activeElement instanceof HTMLElement) {
            document.activeElement.blur();
        }
    };

    // Background isolation (issue #464): while the dialog is open, everything
    // outside it — the app shell, the page content and the modal-external
    // skip link — is inert/aria-hidden, and the actual document scroller
    // (the root element, which carries the viewport overflow under the
    // stylesheet's `html, body { overflow-x: hidden }` rule) is locked along
    // with body. Keyboard focus, assistive-technology virtual navigation,
    // programmatic focus and wheel/touch scrolling therefore cannot reach
    // the page behind the blocking dialog.
    //
    // The opener is captured BEFORE lockBackground() inerts the shell:
    // inerting the shell blurs the trigger (activeElement resets to <body>),
    // so capturing after the lock would record <body> and silently lose the
    // focus-restore target. On close the isolation is released BEFORE focus
    // returns to the opener: the trigger element lives in the (currently
    // inert) background, so restoring earlier would silently fail in real
    // browsers and break the #464 focus-restore contract.
    React.useLayoutEffect(() => {
        if (!visible) {
            return;
        }
        // Capture the opener BEFORE lockBackground(): inerting the background
        // resets document.activeElement to <body> when the (focused) trigger
        // becomes inert, so any capture after this point records <body> and
        // Escape/Close would restore focus nowhere (the deterministic
        // django-e2e focus-restore failure repaired for #465). Runs only on
        // the rising edge of `visible`, so a re-render while the dialog is
        // already open (e.g. the crank:auth-hydrated re-render) can never
        // clobber the original opener with the dialog's own Close button.
        openerRef.current = document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null;
        lockBackground();
        return () => {
            unlockBackground();
            restoreFocusToOpener();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [visible]);

    // Move focus into the dialog once it opens. A passive effect is safe here:
    // focusing the Close button inside the dialog is unaffected by the
    // background inert applied in the layout effect above.
    React.useEffect(() => {
        if (visible) {
            closeButtonRef.current?.focus();
        }
    }, [visible]);

    React.useEffect(() => {
        // Focusable-element selector for the WAI-ARIA focus trap (issue #464).
        const getFocusableElements = (): HTMLElement[] => {
            const dialog = dialogRef.current;
            if (!dialog) return [];
            return Array.from(dialog.querySelectorAll<HTMLElement>(
                'a[href], button:not([disabled]), input:not([disabled]), '
                + 'select:not([disabled]), textarea:not([disabled]), '
                + '[tabindex]:not([tabindex="-1"])'
            ));
        };

        const handleKeyDown = (event: KeyboardEvent) => {
            if (!visible) return;
            if (event.key === 'Escape') {
                // WAI-ARIA dialog pattern: return focus to the trigger element
                // that opened the dialog before closing (issue #430).
                restoreFocusToOpener();
                onClose();
                return;
            }
            if (event.key === 'Tab') {
                // WAI-ARIA focus trap (issue #464): cycle Tab/Shift+Tab among
                // the dialog's own focusable elements so keyboard focus can
                // never move behind the modal into the page background.
                const focusables = getFocusableElements();
                if (focusables.length === 0) return;
                const first = focusables[0];
                const last = focusables[focusables.length - 1];
                const active = document.activeElement;
                const insideDialog = active instanceof HTMLElement && dialogRef.current?.contains(active);
                if (event.shiftKey) {
                    if (!insideDialog || active === first) {
                        event.preventDefault();
                        last.focus();
                    }
                } else if (!insideDialog || active === last) {
                    event.preventDefault();
                    first.focus();
                }
            }
        };

        document.addEventListener('keydown', handleKeyDown);

        return () => {
            document.removeEventListener('keydown', handleKeyDown);
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [visible, onClose]);

    if (!organization || !visible) {
        return null;
    }

    // Map funding round codes to display names
    const fundingRoundMap: Record<string, string> = {
        'S': 'Seed',
        'A': 'Series A',
        'B': 'Series B',
        'C': 'Series C',
        'D': 'Series D',
        'E': 'Series E',
        'F': 'Series F',
        'X': 'Series G or Later',
        'O': 'Other Private',
        'P': 'Public'
    };

    // Map RTO policy codes to display names
    const rtoPolicyMap: Record<string, string> = {
        'R': 'Remote',
        'H': 'Hybrid',
        'O': 'In-Office'
    };

    // Map organization type codes to display names
    const typeMap: Record<string, string> = {
        'C': 'Company (for profit)',
        'N': 'Non-Profit Organization'
    };

    const displayScores = organization.avg_scores || scores;

    const handleCloseClick = (e: React.MouseEvent) => {
        e.stopPropagation();
        // Return focus to the trigger element before close (WAI-ARIA dialog
        // pattern). The close path also releases background isolation and
        // re-restores focus afterwards (see the isolation effect), so the
        // opener receives focus even while it is still inert here.
        restoreFocusToOpener();
        onClose();
    };

    // Issue #479: open the assistant in place with this company as context.
    // openAssistant runs on the next frame so the dialog's unlockBackground()
    // has already released the blocking-surface lock (the #472 yield rule
    // would otherwise close the sheet immediately). Modified clicks and pages
    // without the workspace host keep the plain /chat/?company= navigation.
    const handleAskClick = (e: React.MouseEvent, organizationId: number, organizationName: string) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey
            || !document.getElementById('assistant-workspace')) {
            return;
        }
        e.preventDefault();
        e.stopPropagation();
        onClose();
        window.requestAnimationFrame(() => {
            openAssistant({surface: 'company', organizationId, organizationName});
        });
    };

    const handleOverlayClick = (e: React.MouseEvent) => {
        // Only close if clicking directly on the overlay, not its children
        if (e.target === e.currentTarget) {
            // Return focus to the trigger element before close (WAI-ARIA dialog pattern).
            restoreFocusToOpener();
            onClose();
        }
    };

    // Render the dialog into a portal on <body>: the blocking dialog must not
    // live inside the (inert) background containers while it is open.
    // Fixed-position overlays are page-level by convention.
    return createPortal(
        <div className="popup-overlay" data-testid="popup-overlay" onClick={handleOverlayClick}>
            <div ref={dialogRef} className="popup-details card bg-dark" role="dialog" aria-modal="true"
                 aria-labelledby="organization-details-title">
                <div className="card-header bg-dark d-flex justify-content-between align-items-center">
                    <h2 id="organization-details-title">{organization.name}</h2>
                    <button
                        ref={closeButtonRef}
                        type="button"
                        className="btn-close btn-close-white"
                        aria-label="Close"
                        onClick={handleCloseClick}
                    ></button>
                </div>
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
                               onClick={(event) => handleAskClick(event, organization.id, organization.name)}
                               className="btn btn-sm btn-primary"
                               data-testid="company-chat-cta">
                                Ask the assistant about {organization.name}
                            </a>
                        ) : (
                            companySignInUrl(signInUrlTemplate, organization.id) && (
                                <a href={companySignInUrl(signInUrlTemplate, organization.id) as string}
                                   className="btn btn-sm btn-primary"
                                   data-testid="company-sign-in-cta">
                                    Sign in to ask about {organization.name}
                                </a>
                            )
                        )}
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
                                <div className="col-7">{fundingRoundMap[organization.funding_round]}</div>
                            </div>
                            <div className="row mb-3">
                                <div className="col-5 text-end fw-bold">RTO Policy:</div>
                                <div className="col-7">{rtoPolicyMap[organization.rto_policy]}</div>
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
                                    <div className="col-7">{organization.accelerated_vesting ? 'Yes' : 'No'}</div>
                                </div>
                            )}
                        </div>
                        <div className="popup-details-scores" data-testid="popup-details-score-card">
                            <div className="popup-details-score-row">
                                <span className="fw-bold">Rank:</span>
                                <span>{organization.ranking}</span>
                            </div>
                            <div className="popup-details-score-row">
                                <span className="fw-bold">Profile Completeness:</span>
                                <span>{organization.profile_completeness.toFixed(0)}%</span>
                            </div>
                            {loading ? (
                                <p>Loading scores...</p>
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
                                        ) : (
                                            <tr>
                                                <td className="w-75">Overall Score</td>
                                                <td className="text-end w-25">{organization.avg_score.toFixed(2)}</td>
                                            </tr>
                                        )}
                                    </tbody>
                                </table>
                            )}
                        </div>
                    </div>

                    {/* Data Provenance & Freshness Section */}
                    <hr className="my-3" />
                    <div className="row">
                        <div className="col-12">
                            <h3 className="h6 mb-2">Data Freshness & Sources</h3>
                            {provenanceLoading ? (
                                <p data-testid="provenance-loading">Loading provenance…</p>
                            ) : provenance ? (
                                <div data-testid="provenance-section">
                                    <div className="row mb-2">
                                        <div className="col-5 text-end fw-bold">Last Updated:</div>
                                        <div className="col-7" data-testid="last-updated">
                                            {provenance.organization_modified
                                                ? `${formatRelativeTime(provenance.organization_modified)} (${formatDate(provenance.organization_modified)})`
                                                : 'Unknown'}
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
                                                    {formatRelativeTime(provenance.latest_observation.observed_at)}
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
                                        <p className="text-muted small mb-2" data-testid="no-observation">No crawl observations recorded. Data is curated from submitted reviews.</p>
                                    )}
                                    {provenance.fields && provenance.fields.length > 0 && (
                                        <div className="mt-2" data-testid="field-evidence">
                                            {provenance.fields.map(fieldEvidence => (
                                                <div className="row mb-2" key={fieldEvidence.field_key}
                                                     data-testid={`field-evidence-${fieldEvidence.field_key}`}>
                                                    <div className="col-5 text-end fw-bold">
                                                        {fieldKeyLabel(fieldEvidence.field_key)}:
                                                    </div>
                                                    <div className="col-7">
                                                        <span data-testid={`field-value-${fieldEvidence.field_key}`}>
                                                            {fieldEvidence.value}
                                                        </span>
                                                        <span className="text-muted small">
                                                            {' '}— {fieldEvidence.source_domain || 'unknown source'},
                                                            observed {formatDate(fieldEvidence.observed_at)}
                                                            {formatScope(fieldEvidence.scope)}
                                                        </span>
                                                        {fieldEvidence.stale && (
                                                            <span className="badge bg-warning text-dark ms-1"
                                                                  data-testid={`field-stale-${fieldEvidence.field_key}`}>
                                                                Stale — last verified {formatDate(fieldEvidence.last_verified_at)}
                                                            </span>
                                                        )}
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                    {provenance.unverified_fields && provenance.unverified_fields.length > 0 && (
                                        <div className="mt-2" data-testid="unverified-fields">
                                            {provenance.unverified_fields.map(fieldKey => (
                                                <div className="row mb-2" key={fieldKey}
                                                     data-testid={`field-unverified-${fieldKey}`}>
                                                    <div className="col-5 text-end fw-bold">
                                                        {fieldKeyLabel(fieldKey)}:
                                                    </div>
                                                    <div className="col-7 text-muted">No accepted evidence</div>
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                    {isAuthenticated && (
                                        <div className="mt-2" data-testid="correction-action">
                                            <button type="button"
                                                    className="btn btn-sm btn-outline-light"
                                                    data-testid="suggest-correction-link"
                                                    onClick={() => {
                                                        // Only one blocking dialog is open at a time
                                                        // (issue #464/#471): opening the suggestion
                                                        // form closes this details dialog.
                                                        openSuggestCompany({
                                                            source: 'company_details',
                                                            companyName: organization.name,
                                                            organizationId: organization.id,
                                                        });
                                                        onClose();
                                                    }}>
                                                Suggest a Correction
                                            </button>
                                        </div>
                                    )}
                                </div>
                            ) : (
                                <p className="text-muted" data-testid="provenance-unavailable">Provenance data unavailable.</p>
                            )}
                        </div>
                    </div>
                </div>
            </div>
        </div>,
        document.body
    );
};

export default OrganizationDetailsPopup;
