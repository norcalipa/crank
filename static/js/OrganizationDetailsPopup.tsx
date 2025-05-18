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
    position: { top: number; left: number } | null;
    visible: boolean;
    onMouseEnter: () => void;
    onMouseLeave: () => void;
}

const OrganizationDetailsPopup: React.FC<OrganizationDetailsPopupProps> = ({ 
    organization, 
    position, 
    visible,
    onMouseEnter,
    onMouseLeave
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

    if (!organization || !visible || !position) {
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

    return (
        <div 
            className="popup-details card bg-dark"
            style={{
                top: `${position.top}px`,
                left: `${position.left}px`,
                width: '600px',
            }}
            onMouseEnter={onMouseEnter}
            onMouseLeave={onMouseLeave}
        >
            <div className="card-header bg-dark">
                <h2>{organization.name}</h2>
            </div>
            <div className="card-body">
                <div className="row">
                    <div className="col-md-5">
                        <div className="row mb-3">
                            <div className="col-4 text-end">URL:</div>
                            <div className="col-8">
                                {organization.url && <a href={organization.url} target="_blank" rel="noopener noreferrer">{organization.url}</a>}
                            </div>
                        </div>
                        <div className="row mb-3">
                            <div className="col-4 text-end">Type:</div>
                            <div className="col-8">{organization.type && typeMap[organization.type]}</div>
                        </div>
                        <div className="row mb-3">
                            <div className="col-4 text-end">Funding Round:</div>
                            <div className="col-8">{fundingRoundMap[organization.funding_round]}</div>
                        </div>
                        <div className="row mb-3">
                            <div className="col-4 text-end">RTO Policy:</div>
                            <div className="col-8">{rtoPolicyMap[organization.rto_policy]}</div>
                        </div>
                        {organization.gives_ratings !== undefined && (
                            <div className="row mb-3">
                                <div className="col-4 text-end">Gives Ratings:</div>
                                <div className="col-8">{organization.gives_ratings ? 'Yes' : 'No'}</div>
                            </div>
                        )}
                        {organization.accelerated_vesting !== undefined && (
                            <div className="row mb-3">
                                <div className="col-4 text-end">Accelerated Vesting:</div>
                                <div className="col-8">{organization.accelerated_vesting ? 'Yes' : 'No'}</div>
                            </div>
                        )}
                        <div className="row mb-3">
                            <div className="col-4 text-end">Rank:</div>
                            <div className="col-8">{organization.ranking}</div>
                        </div>
                        <div className="row mb-3">
                            <div className="col-4 text-end">Profile Completeness:</div>
                            <div className="col-8">{organization.profile_completeness.toFixed(0)}%</div>
                        </div>
                    </div>
                    <div className="col-md-7">
                        {loading ? (
                            <p>Loading scores...</p>
                        ) : (
                            <table className="table table-dark">
                                <tbody>
                                    {displayScores && displayScores.length > 0 ? (
                                        displayScores.map((score, index) => (
                                            <tr key={index}>
                                                <td>{score.type__name}</td>
                                                <td className="text-end">{score.avg_score.toFixed(2)}</td>
                                            </tr>
                                        ))
                                    ) : (
                                        <tr>
                                            <td>Overall Score</td>
                                            <td className="text-end">{organization.avg_score.toFixed(2)}</td>
                                        </tr>
                                    )}
                                </tbody>
                            </table>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
};

export default OrganizationDetailsPopup; 