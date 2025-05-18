// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

interface ScoreDetail {
    type__name: string;
    avg_score: number;
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
}

const OrganizationDetailsPopup: React.FC<OrganizationDetailsPopupProps> = ({ 
    organization, 
    visible,
    onClose
}) => {
    const [scores, setScores] = React.useState<ScoreDetail[]>([]);
    const [loading, setLoading] = React.useState(false);

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
    
    // Add keyboard event listener for Escape key
    React.useEffect(() => {
        const handleKeyDown = (event: KeyboardEvent) => {
            if (visible && event.key === 'Escape') {
                // Remove focus from any element to prevent blinking cursor
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
            <div className="popup-details card bg-dark">
                <div className="card-header bg-dark d-flex justify-content-between align-items-center">
                    <h2>{organization.name}</h2>
                    <button 
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
                </div>
            </div>
        </div>
    );
};

export default OrganizationDetailsPopup; 