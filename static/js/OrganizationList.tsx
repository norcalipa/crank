// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createRoot} from "react-dom/client";
import OrganizationDetailsPopup from './OrganizationDetailsPopup';
import SuggestCompanyModal from './SuggestCompanyModal';

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
    canSuggestCompany?: boolean;
    isAuthenticated?: boolean;
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
    showSuggestModal: boolean;
}

class OrganizationList extends React.Component<OrganizationListProps, OrganizationListState> {
    constructor(props: OrganizationListProps) {
        super(props);
        const urlState = this.getUrlState();
        this.state = {
            organizations: props.organizations,
            filteredOrganizations: this.filterOrganizations(props.organizations, urlState.searchTerm, urlState.acceleratedVesting),
            fundingRoundChoices: {},
            rtoPolicyChoices: {},
            currentPage: urlState.currentPage,
            itemsPerPage: props.itemsPerPage || 15,
            acceleratedVesting: urlState.acceleratedVesting,
            searchTerm: urlState.searchTerm,
            selectedOrganization: null,
            showPopup: false,
            showSuggestModal: false
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

        window.addEventListener('popstate', this.handlePopState);
        this.normalizeCurrentPage();
    }

    componentWillUnmount() {
        window.removeEventListener('popstate', this.handlePopState);
    }

    getUrlState = () => {
        const params = new URLSearchParams(window.location.search);
        const requestedPage = Number(params.get('page'));
        return {
            currentPage: Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1,
            searchTerm: params.get('search') || '',
            acceleratedVesting: params.get('accelerated_vesting') === '1'
        };
    };

    filterOrganizations = (organizations: Organization[], searchTerm: string, acceleratedVesting: boolean) => {
        let filteredOrganizations = organizations;

        if (acceleratedVesting) {
            filteredOrganizations = filteredOrganizations.filter(org => org.accelerated_vesting);
        }

        if (searchTerm) {
            filteredOrganizations = filteredOrganizations.filter(org =>
                org.name.toLowerCase().includes(searchTerm.toLowerCase())
            );
        }

        return filteredOrganizations;
    };

    getPageCount = (resultCount: number) => Math.max(1, Math.ceil(resultCount / this.state.itemsPerPage));

    getPageUrl = (pageNumber: number) => {
        const params = new URLSearchParams(window.location.search);
        params.set('page', pageNumber.toString());
        const query = params.toString();
        return `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`;
    };

    updateUrl = (pageNumber: number, searchTerm: string, acceleratedVesting: boolean, replace = false) => {
        const params = new URLSearchParams(window.location.search);
        params.set('page', pageNumber.toString());
        if (searchTerm) {
            params.set('search', searchTerm);
        } else {
            params.delete('search');
        }
        if (acceleratedVesting) {
            params.set('accelerated_vesting', '1');
        } else {
            params.delete('accelerated_vesting');
        }
        const query = params.toString();
        const url = `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`;
        if (replace) {
            window.history.replaceState({}, '', url);
        } else {
            window.history.pushState({}, '', url);
        }
    };

    normalizeCurrentPage = () => {
        const pageCount = this.getPageCount(this.state.filteredOrganizations.length);
        const currentPage = Math.min(this.state.currentPage, pageCount);
        if (currentPage !== this.state.currentPage) {
            this.setState({currentPage});
            this.updateUrl(currentPage, this.state.searchTerm, this.state.acceleratedVesting, true);
        }
    };

    handlePopState = () => {
        const urlState = this.getUrlState();
        const filteredOrganizations = this.filterOrganizations(
            this.state.organizations,
            urlState.searchTerm,
            urlState.acceleratedVesting
        );
        const pageCount = Math.max(1, Math.ceil(filteredOrganizations.length / this.state.itemsPerPage));
        this.setState({
            currentPage: Math.min(urlState.currentPage, pageCount),
            searchTerm: urlState.searchTerm,
            acceleratedVesting: urlState.acceleratedVesting,
            filteredOrganizations
        });
    };

    handlePageChange = (pageNumber: number) => {
        const pageCount = this.getPageCount(this.state.filteredOrganizations.length);
        if (pageNumber < 1 || pageNumber > pageCount || pageNumber === this.state.currentPage) {
            return;
        }
        this.setState({currentPage: pageNumber});
        this.updateUrl(pageNumber, this.state.searchTerm, this.state.acceleratedVesting);
    };

    handleFilterChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const acceleratedVesting = event.target.checked;
        const filteredOrganizations = this.filterOrganizations(this.state.organizations, this.state.searchTerm, acceleratedVesting);
        this.setState({acceleratedVesting, filteredOrganizations, currentPage: 1});
        this.updateUrl(1, this.state.searchTerm, acceleratedVesting);
    };

    handleSearchChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const searchTerm = event.target.value;
        const filteredOrganizations = this.filterOrganizations(this.state.organizations, searchTerm, this.state.acceleratedVesting);
        this.setState({searchTerm, filteredOrganizations, currentPage: 1});
        this.updateUrl(1, searchTerm, this.state.acceleratedVesting);
    };

    handleClearFilters = () => {
        this.setState({searchTerm: '', acceleratedVesting: false, filteredOrganizations: this.state.organizations, currentPage: 1});
        this.updateUrl(1, '', false);
    };

    handleOrganizationClick = (organization: Organization) => {
        // Get organization details if not already fetched
        if (!organization.url || !organization.type) {
            fetch(`/api/organizations/${organization.id}/`)
                .then(response => response.json())
                .then(data => {
                    const updatedOrg = { ...organization, ...data };
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

    handleOpenSuggestModal = () => {
        this.setState({showSuggestModal: true});
    };

    handleCloseSuggestModal = () => {
        this.setState({showSuggestModal: false});
    };

    handleClosePopup = () => {
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

        const pageCount = this.getPageCount(filteredOrganizations.length);
        const displayedPage = Math.min(currentPage, pageCount);
        const indexOfLastItem = displayedPage * itemsPerPage;
        const indexOfFirstItem = indexOfLastItem - itemsPerPage;
        const currentOrganizations = filteredOrganizations.slice(indexOfFirstItem, indexOfLastItem);
        const pageNumbers = Array.from({length: pageCount}, (_, index) => index + 1);
        const firstResult = filteredOrganizations.length === 0 ? 0 : indexOfFirstItem + 1;
        const lastResult = Math.min(indexOfLastItem, filteredOrganizations.length);

        return (<div>
            <div className="mb-3">
                <label className="form-label" htmlFor="organization-search">Search organizations</label>
                <div className="input-group">
                    <input
                        id="organization-search"
                        type="text"
                        className="form-control"
                        placeholder="Search organizations"
                        value={searchTerm}
                        onChange={this.handleSearchChange}
                    />
                    {searchTerm && <button type="button" className="btn btn-outline-secondary" onClick={this.handleClearFilters} aria-label="Clear search">Clear search</button>}
                    <span className="input-group-text">
                        <input
                            type="checkbox"
                            className="form-check-input"
                            id="acceleratedVesting"
                            data-testid="accelerated-vesting-checkbox"
                            checked={acceleratedVesting}
                            onChange={this.handleFilterChange}
                        />
                        <label className="form-check-label" htmlFor="acceleratedVesting">&nbsp;Show only companies with first vesting in &lt; 1 year</label>
                    </span>
                </div>
                {this.props.isAuthenticated && (
                    <button type="button" className="btn btn-outline-primary btn-sm mt-2"
                            data-testid="suggest-company-btn" onClick={this.handleOpenSuggestModal}>
                        Suggest a company
                    </button>
                )}
            </div>
            <div className="mb-2" role="status" aria-live="polite">
                {`Showing ${firstResult}-${lastResult} of ${filteredOrganizations.length} organizations`}
                <span className="ms-2">{`Page ${displayedPage} of ${pageCount}`}</span>
            </div>
            <nav aria-label="Organization pagination">
                <ul className="pagination">
                    <li className={`page-item ${displayedPage === 1 ? 'disabled' : ''}`}>
                        <a className="page-link" href={this.getPageUrl(displayedPage - 1)} aria-label="Previous page"
                           aria-disabled={displayedPage === 1} rel={displayedPage > 1 ? 'prev' : undefined}
                           tabIndex={displayedPage === 1 ? -1 : undefined}
                           onClick={(event) => { event.preventDefault(); this.handlePageChange(displayedPage - 1); }}>Previous</a>
                    </li>
                    {pageNumbers.map(number => (
                        <li className={`page-item ${displayedPage === number ? 'active' : ''}`} key={number}>
                            <a className="page-link"
                               data-testid={`page-link-${number}`}
                               href={this.getPageUrl(number)}
                               aria-label={`Page ${number}`}
                               aria-current={displayedPage === number ? 'page' : undefined}
                               onClick={(event) => {
                                   event.preventDefault();
                                   this.handlePageChange(number);
                               }}>{number}</a>
                        </li>))}
                    <li className={`page-item ${displayedPage === pageCount ? 'disabled' : ''}`}>
                        <a className="page-link" href={this.getPageUrl(displayedPage + 1)} aria-label="Next page"
                           aria-disabled={displayedPage === pageCount} rel={displayedPage < pageCount ? 'next' : undefined}
                           tabIndex={displayedPage === pageCount ? -1 : undefined}
                           onClick={(event) => { event.preventDefault(); this.handlePageChange(displayedPage + 1); }}>Next</a>
                    </li>
                </ul>
            </nav>

            {filteredOrganizations.length === 0 ? (<div className="alert alert-secondary" role="alert">
                <h2 className="h5">No organizations found</h2>
                <p>There are no organizations that match your search or filters.</p>
                {(searchTerm || acceleratedVesting) && <button type="button" className="btn btn-secondary" onClick={this.handleClearFilters}>Clear search and filters</button>}
                {(this.props.canSuggestCompany || this.props.isAuthenticated) && <p className="mt-2 mb-0"><button type="button" className="btn btn-link p-0" onClick={this.handleOpenSuggestModal}>Suggest a company</button> for evaluation.</p>}
            </div>) : (<>
                <div className="organization-table-wrap" role="region" aria-label="Organization rankings" tabIndex={0}>
                    <table className="table organization-table">
                        <caption className="visually-hidden">Organizations ranked by the selected scoring algorithm</caption>
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
                            onKeyDown={(event) => {
                                if (event.key === 'Enter' || event.key === ' ') {
                                    event.preventDefault();
                                    this.handleOrganizationClick(org);
                                }
                            }}
                            tabIndex={0}
                            role="button"
                            aria-label={`View details for ${org.name}`}
                            className="organization-row"
                        >
                            <td>{org.ranking}</td>
                            <td><span className="organization-name">{org.name}</span></td>
                            <td>{org.avg_score.toFixed(2)}</td>
                            <td>{fundingRoundChoices[org.funding_round]}</td>
                            <td>{rtoPolicyChoices[org.rto_policy]}</td>
                            <td>{org.profile_completeness.toFixed(0)}%</td>
                        </tr>))}
                        </tbody>
                    </table>
                </div>
                <div className="organization-cards" aria-label="Organization ranking cards">
                    {currentOrganizations.map(org => (
                        <article
                            key={`card-${org.id}`}
                            className="card organization-card"
                            onClick={() => this.handleOrganizationClick(org)}
                            onKeyDown={(event) => {
                                if (event.key === 'Enter' || event.key === ' ') {
                                    event.preventDefault();
                                    this.handleOrganizationClick(org);
                                }
                            }}
                            tabIndex={0}
                            role="button"
                            aria-label={`View details for ${org.name}`}
                        >
                            <div className="card-body">
                                <h2 className="h5 organization-card-name">{org.name}</h2>
                                <div className="organization-card-score">
                                    <span className="organization-card-label">Rank / score</span>
                                    #{org.ranking} · {org.avg_score.toFixed(2)}
                                </div>
                                <div>
                                    <span className="organization-card-label">RTO policy</span>
                                    {rtoPolicyChoices[org.rto_policy]}
                                </div>
                                <div>
                                    <span className="organization-card-label">Funding round</span>
                                    {fundingRoundChoices[org.funding_round]}
                                </div>
                                <div>
                                    <span className="organization-card-label">Profile completeness</span>
                                    {org.profile_completeness.toFixed(0)}%
                                </div>
                            </div>
                        </article>
                    ))}
                </div>
            </>)}

            <OrganizationDetailsPopup
                organization={selectedOrganization}
                visible={showPopup}
                onClose={this.handleClosePopup}
                isAuthenticated={this.props.isAuthenticated}
            />
            <SuggestCompanyModal
                visible={this.state.showSuggestModal}
                onClose={this.handleCloseSuggestModal}
            />
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
                const configElement = document.getElementById('organization-list-config');
                const root = createRoot(container);
                root.render(<OrganizationList organizations={organizationsData}
                    canSuggestCompany={configElement?.getAttribute('data-can-suggest-company') === 'true'}
                    isAuthenticated={container.dataset.authenticated === 'true'}/>);
            }
        } catch (error) {
            console.error('Error parsing organization data:', error);
        }
    }
});
