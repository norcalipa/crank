// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createRoot} from "react-dom/client";
import OrganizationDetailsPopup from './OrganizationDetailsPopup';

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

interface OrganizationListProps {
    organizations: Organization[];
    itemsPerPage?: number;
}

interface OrganizationListState {
    organizations: Organization[];
    filteredOrganizations: Organization[];
    fundingRoundChoices: Record<string, string>;
    rtoPolicyChoices: Record<string, string>;
    currentPage: number;
    itemsPerPage: number;
    acceleratedVesting: boolean;
    searchTerm: string;
    selectedOrganization: Organization | null;
    showPopup: boolean;
}

class OrganizationList extends React.Component<OrganizationListProps, OrganizationListState> {
    constructor(props: OrganizationListProps) {
        super(props);
        this.state = {
            organizations: props.organizations,
            filteredOrganizations: props.organizations,
            fundingRoundChoices: {},
            rtoPolicyChoices: {},
            currentPage: this.getCurrentPageFromQueryString(),
            itemsPerPage: props.itemsPerPage || 15,
            acceleratedVesting: false,
            searchTerm: '',
            selectedOrganization: null,
            showPopup: false
        };
    }

    componentDidMount() {
        fetch('/api/funding-round-choices/')
            .then(response => response.json())
            .then(data => {
                this.setState({fundingRoundChoices: data});
            })
            .catch((error) => {
                console.error('Error fetching funding round choices:', error);
            });

        fetch('/api/rto-policy-choices/')
            .then(response => response.json())
            .then(data => {
                this.setState({rtoPolicyChoices: data});
            })
            .catch((error) => {
                console.error('Error fetching RTO policy choices:', error)
            });
    }

    getCurrentPageFromQueryString() {
        const params = new URLSearchParams(window.location.search);
        return parseInt(params.get('page') || '1', 10);
    }

    handlePageChange = (pageNumber: number) => {
        this.setState({currentPage: pageNumber});
        const url = new URL(window.location.href);
        url.searchParams.set('page', pageNumber.toString());
        window.history.pushState({}, '', url.toString());
    };

    handleFilterChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const {checked} = event.target;
        this.setState({acceleratedVesting: checked}, this.applyFilters);
    };

    handleSearchChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const {value} = event.target;
        this.setState({searchTerm: value}, this.applyFilters);
    };

    applyFilters = () => {
        const {organizations, acceleratedVesting, searchTerm} = this.state;
        let filteredOrganizations = organizations;

        if (acceleratedVesting) {
            filteredOrganizations = filteredOrganizations.filter(org => org.accelerated_vesting);
        }

        if (searchTerm) {
            filteredOrganizations = filteredOrganizations.filter(org =>
                org.name.toLowerCase().includes(searchTerm.toLowerCase())
            );
        }

        this.setState({filteredOrganizations});
    };

    handleOrganizationClick = (organization: Organization) => {
        // Remove focus from any element to prevent blinking cursor
        if (document.activeElement instanceof HTMLElement) {
            document.activeElement.blur();
        }
        
        // Get organization details if not already fetched
        if (!organization.url || !organization.type) {
            fetch(`/api/organizations/${organization.id}/`)
                .then(response => response.json())
                .then(data => {
                    // Update the organization with additional details
                    const updatedOrg = { ...organization, ...data };
                    
                    // Find the organization in the filteredOrganizations array and update it
                    const updatedOrganizations = this.state.organizations.map(org => 
                        org.id === organization.id ? updatedOrg : org
                    );
                    
                    this.setState({ 
                        organizations: updatedOrganizations,
                        selectedOrganization: updatedOrg,
                        showPopup: true
                    });
                })
                .catch(error => {
                    console.error('Error fetching organization details:', error);
                    this.setState({ 
                        selectedOrganization: organization,
                        showPopup: true
                    });
                });
        } else {
            this.setState({ 
                selectedOrganization: organization,
                showPopup: true
            });
        }
    };
    
    handleClosePopup = () => {
        // Remove focus from any element to prevent blinking cursor
        if (document.activeElement instanceof HTMLElement) {
            document.activeElement.blur();
        }
        this.setState({ showPopup: false });
    };

    render() {
        const {
            filteredOrganizations,
            fundingRoundChoices,
            rtoPolicyChoices,
            currentPage,
            itemsPerPage,
            acceleratedVesting,
            searchTerm,
            selectedOrganization,
            showPopup
        } = this.state;

        const indexOfLastItem = currentPage * itemsPerPage;
        const indexOfFirstItem = indexOfLastItem - itemsPerPage;
        const currentOrganizations = filteredOrganizations.slice(indexOfFirstItem, indexOfLastItem);

        const pageNumbers = [];
        for (let i = 1; i <= Math.ceil(filteredOrganizations.length / itemsPerPage); i++) {
            pageNumbers.push(i);
        }

        return (<div>
            <>
                <div className="input-group mb-3">
                    <input
                        type="text"
                        className="form-control"
                        aria-label="Search"
                        placeholder="Search organizations"
                        value={searchTerm}
                        aria-describedby="inputGroup-sizing-default"
                        onChange={this.handleSearchChange}
                    />
                    <div className="input-group-append">
                            <span className="input-group-text">
                            <input
                                type="checkbox"
                                className="form-check-input"
                                id="acceleratedVesting"
                                data-testid="accelerated-vesting-checkbox"
                                checked={acceleratedVesting}
                                onChange={this.handleFilterChange}
                            />
                            <label
                                className="form-check-label"
                                htmlFor="acceleratedVesting">&nbsp;Show only companies with first vesting in &lt; 1 year</label>
                            </span>
                    </div>
                </div>
                <div className="pagination">
                    <ul className="pagination">
                        {pageNumbers.map(number => (
                            <li className={`page-item ${currentPage === number ? 'active' : ''}`} key={number}>
                                <a className="page-link"
                                   data-testid={`page-link-${number}`}
                                   href={`?page-${number}`}
                                   onClick={(e) => {
                                       e.preventDefault();
                                       this.handlePageChange(number);
                                   }}>{number}</a>
                            </li>))}
                    </ul>
                </div>

                {filteredOrganizations.length === 0 ? (<div className="alert alert-secondary" role="alert">
                    There are no organizations that match the selected filters.
                </div>) : (<div>
                    <table className="table organization-table">
                        <thead>
                        <tr>
                            <th>Rank</th>
                            <th>Name</th>
                            <th>Overall Score</th>
                            <th>Funding Round</th>
                            <th>RTO Policy</th>
                            <th>Profile Completeness</th>
                        </tr>
                        </thead>
                        <tbody>
                        {currentOrganizations.map(org => (<tr 
                            key={org.id}
                            onClick={() => this.handleOrganizationClick(org)}
                            style={{ 
                                cursor: 'pointer',
                                transition: 'background-color 0.2s ease'
                            }}
                            className="organization-row"
                        >
                            <td>{org.ranking}</td>
                            <td>
                                <span className="organization-name">
                                    {org.name}
                                </span>
                            </td>
                            <td>{org.avg_score.toFixed(2)}</td>
                            <td>{fundingRoundChoices[org.funding_round]}</td>
                            <td>{rtoPolicyChoices[org.rto_policy]}</td>
                            <td>{org.profile_completeness.toFixed(0)}%</td>
                        </tr>))}
                        </tbody>
                    </table>
                </div>)}
                
                <OrganizationDetailsPopup 
                    organization={selectedOrganization} 
                    visible={showPopup}
                    onClose={this.handleClosePopup}
                />
            </>
        </div>);
    }
}

export default OrganizationList;

document.addEventListener('DOMContentLoaded', () => {
    const organizationDataElement = document.getElementById('organization-data');
    if (organizationDataElement && organizationDataElement.textContent) {
        try {
            const organizationsData = JSON.parse(organizationDataElement.textContent);
            const container = document.getElementById('organization-list');
            if (container) {
                const root = createRoot(container);
                root.render(<OrganizationList organizations={organizationsData}/>);
            }
        } catch (error) {
            console.error('Error parsing organization data:', error);
        }
    }
});