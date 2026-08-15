// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

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
}

interface ProvenanceData {
    organization_id: number;
    organization_modified: string | null;
    organization_created: string | null;
    latest_observation: ProvenanceObservation | null;
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

const OrganizationDetailsPopup: React.FC<OrganizationDetailsPopupProps> = ({
    organization,
    visible,
    onClose,
    isAuthenticated = false
}) => {
    const [scores, setScores] = React.useState<ScoreDetail[]>([]);
    const [loading, setLoading] = React.useState(false);
    const [provenance, setProvenance] = React.useState<ProvenanceData | null>(null);
    const [provenanceLoading, setProvenanceLoading] = React.useState(false);
    const closeButtonRef = React.useRef<HTMLButtonElement>(null);

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

    // Add keyboard event listener for Escape key
    React.useEffect(() => {
        if (visible) {
            closeButtonRef.current?.focus();
        }
    }, [visible]);

    React.useEffect(() => {
        const handleKeyDown = (event: KeyboardEvent) => {
            if (visible && event.key === 'Escape') {
                // INTENTIONAL dialog focus management (issue #423): blurs any
                // focused element before closing so focus never lingers on a
                // now-hidden node and no "blinking cursor" artifact remains.
                // Unlike the removed OrganizationList workaround (a non-modal
                // list), this lives inside a real role="dialog" aria-modal
                // popup, so returning focus to the caller would be the also-
                // valid pattern; blur+onClose is the deliberate choice here,
                // guarded by instanceof HTMLElement. The same rationale applies
                // to the blur in handleCloseClick / handleOverlayClick below.
                if (document.activeElement instanceof HTMLElement) {
                    document.activeElement.blur();
                }
                onClose();
            }
        };

        document.addEventListener('keydown', handleKeyDown);

        return () => {
            document.removeEventListener('keydown', handleKeyDown);
        };
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
        // Remove focus from any element to prevent blinking cursor
        if (document.activeElement instanceof HTMLElement) {
            document.activeElement.blur();
        }
        onClose();
    };

    const handleOverlayClick = (e: React.MouseEvent) => {
        // Only close if clicking directly on the overlay, not its children
        if (e.target === e.currentTarget) {
            // Remove focus from any element to prevent blinking cursor
            if (document.activeElement instanceof HTMLElement) {
                document.activeElement.blur();
            }
            onClose();
        }
    };

    return (
        <div className="popup-overlay" data-testid="popup-overlay" onClick={handleOverlayClick}>
            <div className="popup-details card bg-dark" role="dialog" aria-modal="true"
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
                    <div className="row">
                        <div className="col-md-7">
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
                            <div className="row mb-3">
                                <div className="col-5 text-end fw-bold">Rank:</div>
                                <div className="col-7">{organization.ranking}</div>
                            </div>
                            <div className="row mb-3">
                                <div className="col-5 text-end fw-bold">Profile Completeness:</div>
                                <div className="col-7">{organization.profile_completeness.toFixed(0)}%</div>
                            </div>
                        </div>
                        <div className="col-md-5">
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
                                        </div>
                                    ) : (
                                        <p className="text-muted small mb-2" data-testid="no-observation">No crawl observations recorded. Data is curated from submitted reviews.</p>
                                    )}
                                    {isAuthenticated && (
                                        <div className="mt-2" data-testid="correction-action">
                                            <a href={`/api/company-requests/`}
                                               className="btn btn-sm btn-outline-light"
                                               data-testid="suggest-correction-link">
                                                Suggest a Correction
                                            </a>
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
        </div>
    );
};

export default OrganizationDetailsPopup;
