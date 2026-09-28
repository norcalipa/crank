// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createRoot} from "react-dom/client";
import OrganizationDetailsPopup from './OrganizationDetailsPopup';
import {closeSuggestCompany, openSuggestCompany} from './suggestCompany/controller';

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

interface RankingPreset {
    id: number;
    name: string;
}

interface OrganizationListProps {
    organizations: Organization[];
    itemsPerPage?: number;
    canSuggestCompany?: boolean;
    isAuthenticated?: boolean;
    // Server-built, server-validated login URL template for the company
    // details dialog's sign-in CTA (issue #465 AC-7); see
    // crank/views/index.py.
    signInUrlTemplate?: string;
    // Ranking presets (issue #478): rendered server-side as non-user-specific
    // JSON. When absent (fixture pages, older cached shells) the preset
    // select is omitted.
    rankingPresets?: RankingPreset[];
    currentAlgorithmId?: number | null;
    algorithmUrlTemplate?: string;
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
    choicesStatus: 'loading' | 'ready' | 'error';
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
            choicesStatus: 'loading'
        };
    }

    // Monotonically increasing generation for blocking-dialog intents (issue
    // #464). Every dialog open/close/select action claims the next value so
    // an in-flight organization-details fetch can detect, when its response
    // arrives, that a newer intent has superseded it.
    private modalGeneration = 0;

    // Funding-round and RTO labels come from two choice endpoints. The status
    // drives the loading/error cue next to the result count; on failure the
    // raw codes are shown as fallback labels and Retry refetches both.
    loadChoices = () => {
        this.setState({choicesStatus: 'loading'});
        const load = (url: string, key: 'fundingRoundChoices' | 'rtoPolicyChoices', label: string) => fetch(url)
            .then(response => {
                if (response.ok === false) {
                    throw new Error(`HTTP ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                this.setState({[key]: data} as Pick<OrganizationListState, typeof key>);
            })
            .catch((error) => {
                console.error(`Error fetching ${label}:`, error);
                throw error;
            });
        Promise.all([
            load('/api/funding-round-choices/', 'fundingRoundChoices', 'funding round choices'),
            load('/api/rto-policy-choices/', 'rtoPolicyChoices', 'RTO policy choices')
        ])
            .then(() => this.setState({choicesStatus: 'ready'}))
            .catch(() => this.setState({choicesStatus: 'error'}));
    };

    componentDidMount() {
        this.loadChoices();

        window.addEventListener('popstate', this.handlePopState);
        this.normalizeCurrentPage();
        this.openCompanyFromUrl();
    }

    componentWillUnmount() {
        window.removeEventListener('popstate', this.handlePopState);
    }

    // A user sent to sign-in from a company's details dialog returns with
    // that company id in the URL (issue #465 AC-7): open the same dialog on
    // load so the round trip through sign-in feels seamless. A missing or
    // unknown id is silently ignored — never a console error — the page
    // still renders the normal ranked list.
    openCompanyFromUrl = () => {
        const params = new URLSearchParams(window.location.search);
        const raw = params.get('company');
        if (raw === null) return;
        const companyId = Number(raw);
        if (!Number.isInteger(companyId)) return;
        const organization = this.props.organizations.find((org) => org.id === companyId);
        if (organization) {
            this.handleOrganizationClick(organization);
        }
    };

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

    handleRemoveSearch = () => {
        const filteredOrganizations = this.filterOrganizations(this.state.organizations, '', this.state.acceleratedVesting);
        this.setState({searchTerm: '', filteredOrganizations, currentPage: 1});
        this.updateUrl(1, '', this.state.acceleratedVesting);
    };

    handleRemoveAcceleratedVesting = () => {
        const filteredOrganizations = this.filterOrganizations(this.state.organizations, this.state.searchTerm, false);
        this.setState({acceleratedVesting: false, filteredOrganizations, currentPage: 1});
        this.updateUrl(1, this.state.searchTerm, false);
    };

    // Switching preset navigates to the preset's page keeping only the
    // compatible filters; page and company are dropped so results start at
    // page 1 (issue #478).
    getPresetUrl = (presetId: number) => {
        const template = this.props.algorithmUrlTemplate || '/algo/__ALGORITHM_ID__/';
        const params = new URLSearchParams();
        if (this.state.searchTerm) {
            params.set('search', this.state.searchTerm);
        }
        if (this.state.acceleratedVesting) {
            params.set('accelerated_vesting', '1');
        }
        const query = params.toString();
        return `${template.replace('__ALGORITHM_ID__', String(presetId))}${query ? `?${query}` : ''}`;
    };

    handlePresetChange = (event: React.ChangeEvent<HTMLSelectElement>) => {
        const presetId = Number(event.target.value);
        if (!Number.isInteger(presetId) || presetId === this.props.currentAlgorithmId) {
            return;
        }
        window.location.assign(this.getPresetUrl(presetId));
    };

    handleOrganizationClick = (organization: Organization) => {
        // Claim this open intent synchronously (issue #464): every later
        // open/close action bumps the generation, and the details fetch below
        // re-checks it on resolve, so a stale response can never reopen or
        // replace a newer dialog or a different company selection.
        const generation = ++this.modalGeneration;
        // Get organization details if not already fetched. Opening the details
        // dialog closes the suggest modal: only one blocking dialog may be
        // active at a time (issue #464).
        if (!organization.url || !organization.type) {
            fetch(`/api/organizations/${organization.id}/`)
                .then(response => response.json())
                .then(data => {
                    // Stale-response guard (issue #464): the dialog may have
                    // been closed, a different company selected, or a newer
                    // request issued while this fetch was in flight. Ignore
                    // the late response in all of those cases.
                    if (!this.isCurrentDetailsIntent(generation, organization.id)) {
                        return;
                    }
                    const updatedOrg = { ...organization, ...data };
                    const updatedOrganizations = this.state.organizations.map(org =>
                        org.id === organization.id ? updatedOrg : org
                    );
                    // Opening the details dialog closes the suggest modal:
                    // only one blocking dialog may be active at a time
                    // (issue #464), now enforced across the shared controller
                    // (issue #471).
                    closeSuggestCompany();
                    this.setState({
                        organizations: updatedOrganizations,
                        selectedOrganization: updatedOrg,
                        showPopup: true
                    });
                })
                .catch(error => {
                    if (!this.isCurrentDetailsIntent(generation, organization.id)) {
                        return;
                    }
                    console.error('Error fetching organization details:', error);
                    closeSuggestCompany();
                    this.setState({
                        selectedOrganization: organization,
                        showPopup: true
                    });
                });
        } else {
            closeSuggestCompany();
            this.setState({
                selectedOrganization: organization,
                showPopup: true
            });
        }
    };

    // A details response may only open the dialog while its captured intent
    // is still the latest one (issue #464 stale-response race): no newer
    // open/close/select has superseded it (generation check) and no other
    // company's dialog is the currently active intent (selection check).
    // Without this guard, a slow response could reopen a dialog the user
    // closed, clobber a suggest form the user is typing into, or replace a
    // newer selection when responses resolve out of order.
    isCurrentDetailsIntent = (generation: number, organizationId: number): boolean => {
        if (generation !== this.modalGeneration) {
            return false;
        }
        return !(
            this.state.showPopup
            && this.state.selectedOrganization !== null
            && this.state.selectedOrganization.id !== organizationId
        );
    };

    handleOpenSuggestModal = (source: 'rankings' | 'rankings_empty') => {
        // Opening the suggest modal closes the details dialog: only one
        // blocking dialog may be active at a time (issue #464). The
        // generation bump also invalidates any in-flight details fetch so
        // its late response cannot reopen the details dialog over this
        // modal or destroy the user's form input.
        ++this.modalGeneration;
        this.setState({showPopup: false, selectedOrganization: null});
        openSuggestCompany({
            source,
            searchTerm: this.state.searchTerm,
            companyName: source === 'rankings_empty' && this.state.searchTerm ? this.state.searchTerm : undefined,
            page: this.state.currentPage
        });
    };

    handleClosePopup = () => {
        // Closing the details dialog invalidates any in-flight details fetch
        // (issue #464): a response that arrives after this close must not
        // reopen the dialog.
        ++this.modalGeneration;
        this.setState({ showPopup: false });
    };

    renderChips = () => {
        const {searchTerm, acceleratedVesting} = this.state;
        if (!searchTerm && !acceleratedVesting) {
            return null;
        }
        return (<ul className="filter-chips" aria-label="Active filters">
            {searchTerm && <li><button type="button" className="filter-chip" data-testid="filter-chip-search"
                                       aria-label={`Remove filter: search "${searchTerm}"`} title={`Search: ${searchTerm}`}
                                       onClick={this.handleRemoveSearch}>
                <span className="filter-chip-text">Search: {searchTerm}</span> <span aria-hidden="true">×</span>
            </button></li>}
            {acceleratedVesting && <li><button type="button" className="filter-chip" data-testid="filter-chip-accelerated-vesting"
                                               aria-label="Remove filter: first vesting in under 1 year"
                                               onClick={this.handleRemoveAcceleratedVesting}>
                <span className="filter-chip-text">First vesting &lt; 1 year</span> <span aria-hidden="true">×</span>
            </button></li>}
        </ul>);
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
            showPopup,
            choicesStatus
        } = this.state;

        const pageCount = this.getPageCount(filteredOrganizations.length);
        const displayedPage = Math.min(currentPage, pageCount);
        const indexOfLastItem = displayedPage * itemsPerPage;
        const indexOfFirstItem = indexOfLastItem - itemsPerPage;
        const currentOrganizations = filteredOrganizations.slice(indexOfFirstItem, indexOfLastItem);
        const pageNumbers = Array.from({length: pageCount}, (_, index) => index + 1);
        const firstResult = filteredOrganizations.length === 0 ? 0 : indexOfFirstItem + 1;
        const lastResult = Math.min(indexOfLastItem, filteredOrganizations.length);

        const presets = this.props.rankingPresets || [];
        const currentPreset = presets.find(preset => preset.id === this.props.currentAlgorithmId);
        const scoreLabel = currentPreset ? `Company score (${currentPreset.name})` : 'Company score';
        const isEmpty = filteredOrganizations.length === 0;

        // Raw codes only appear as the explained error fallback; while the
        // labels load a placeholder keeps the cell from flashing "H"/"R".
        const choiceLabel = (choices: Record<string, string>, code: string) =>
            choices[code] ?? (choicesStatus === 'loading' ? '—' : code);

        const renderPager = (position: 'top' | 'bottom') => (
<nav aria-label={`Organization pagination${position === 'bottom' ? ' (bottom)' : ''}`}>
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
                               data-testid={`page-link-${number}${position === 'bottom' ? '-bottom' : ''}`}
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
        );

        return (<div>
            <div className="rankings-toolbar" data-testid="rankings-toolbar">
                <div className="rankings-toolbar-search">
                    <label className="form-label" htmlFor="organization-search">Search organizations</label>
                    <div className="organization-search">
                        <input
                            id="organization-search"
                            type="text"
                            className="form-control"
                            placeholder="Search organizations"
                            value={searchTerm}
                            onChange={this.handleSearchChange}
                        />
                        {searchTerm && <button type="button" className="btn btn-outline-secondary" onClick={this.handleClearFilters} aria-label="Clear search">Clear search</button>}
                    </div>
                </div>
                {this.renderChips()}
                {presets.length > 0 && (
                    <div className="rankings-toolbar-preset">
                        <label className="form-label" htmlFor="ranking-preset">Ranking preset</label>
                        <select id="ranking-preset" className="form-select" data-testid="ranking-preset-select"
                                value={this.props.currentAlgorithmId ?? ''} onChange={this.handlePresetChange}>
                            {presets.map(preset => <option key={preset.id} value={preset.id}>{preset.name}</option>)}
                        </select>
                    </div>
                )}
                <div className="organization-filter">
                    <input
                        type="checkbox"
                        className="form-check-input"
                        id="acceleratedVesting"
                        data-testid="accelerated-vesting-checkbox"
                        checked={acceleratedVesting}
                        onChange={this.handleFilterChange}
                    />
                    <label className="form-check-label" htmlFor="acceleratedVesting">Show only companies with first vesting in &lt; 1 year</label>
                </div>
                <div className="rankings-toolbar-count" role="status" aria-live="polite">
                    <div className="organization-results-count">{isEmpty ? 'Showing 0 organizations' : `Showing ${firstResult}-${lastResult} of ${filteredOrganizations.length} organizations`}</div>
                    {choicesStatus === 'loading' && <div className="choices-status text-muted" data-testid="choices-status-loading"><span className="spinner-border spinner-border-sm" aria-hidden="true"></span><span>Loading funding round and RTO labels…</span></div>}
                    {choicesStatus === 'error' && <div className="choices-status choices-status-error alert alert-danger py-1 px-2 mt-1 mb-0" data-testid="choices-status-error">
                        <span>Couldn't load funding round and RTO labels, so raw codes are shown instead.</span>{' '}
                        <button type="button" className="btn btn-outline-danger btn-sm" data-testid="choices-retry" onClick={this.loadChoices}>Retry</button>
                    </div>}
                    {pageCount > 1 && <div className="organization-page-count">{`Page ${displayedPage} of ${pageCount}`}</div>}
                </div>
                {this.props.isAuthenticated && !isEmpty && (
                    <button type="button" className="btn btn-outline-primary btn-sm"
                            data-testid="suggest-company-btn"
                            onClick={() => this.handleOpenSuggestModal('rankings')}>
                        Suggest a company
                    </button>
                )}
            </div>
            {pageCount > 1 && renderPager('top')}

            {filteredOrganizations.length === 0 ? (<div className="alert alert-secondary organization-empty-state" role="alert">
                <h2 className="h5">No organizations found</h2>
                <p>There are no organizations that match your search or filters.</p>
                {(searchTerm || acceleratedVesting) && <button type="button" className="btn btn-primary" onClick={this.handleClearFilters}>Clear search and filters</button>}
                {(this.props.canSuggestCompany || this.props.isAuthenticated) && <p className="mt-2 mb-0"><button type="button" className="btn btn-link p-0 suggest-company-empty" data-testid="suggest-company-empty-btn" onClick={() => this.handleOpenSuggestModal('rankings_empty')}>Suggest a company</button> for evaluation.</p>}
            </div>) : (<>
                <div className="organization-results">
                <div className="organization-table-wrap" role="region" aria-label="Organization rankings" tabIndex={0}>
                    <table className="table organization-table">
                        <caption className="visually-hidden">Organizations ranked by the selected scoring algorithm</caption>
                        <thead>
                        <tr>
                            <th className="col-rank">Rank</th>
                            <th className="col-name">Name</th>
                            <th className="col-score">{scoreLabel}</th>
                            <th className="col-funding">Funding Round</th>
                            <th className="col-rto">RTO Policy</th>
                            <th className="col-profile">Profile Completeness</th>
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
                            <td className="col-rank">{org.ranking}</td>
                            <td className="col-name"><span className="organization-name" title={org.name}>{org.name}</span></td>
                            <td className="col-score">{org.avg_score.toFixed(2)}</td>
                            <td className="col-funding">{choiceLabel(fundingRoundChoices, org.funding_round)}</td>
                            <td className="col-rto">{choiceLabel(rtoPolicyChoices, org.rto_policy)}</td>
                            <td className="col-profile">{org.profile_completeness.toFixed(0)}%</td>
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
                                <h2 className="h5 organization-card-name" title={org.name}>{org.name}</h2>
                                <div className="organization-card-score">
                                    <span className="organization-card-label">{scoreLabel}</span>
                                    #{org.ranking} · {org.avg_score.toFixed(2)}
                                </div>
                                <div>
                                    <span className="organization-card-label">RTO policy</span>
                                    {choiceLabel(rtoPolicyChoices, org.rto_policy)}
                                </div>
                                <div>
                                    <span className="organization-card-label">Funding round</span>
                                    {choiceLabel(fundingRoundChoices, org.funding_round)}
                                </div>
                                <div>
                                    <span className="organization-card-label">Profile completeness</span>
                                    {org.profile_completeness.toFixed(0)}%
                                </div>
                            </div>
                        </article>
                    ))}
                </div>
                </div>
                {pageCount > 1 && renderPager('bottom')}
            </>)}

            <OrganizationDetailsPopup
                organization={selectedOrganization}
                visible={showPopup}
                onClose={this.handleClosePopup}
                isAuthenticated={this.props.isAuthenticated}
                signInUrlTemplate={this.props.signInUrlTemplate}
            />
        </div>);
    }
}

export default OrganizationList;

document.addEventListener('DOMContentLoaded', () => {
    const organizationDataElement = document.getElementById('organization-data');
    if (!organizationDataElement || !organizationDataElement.textContent) {
        return;
    }
    try {
        const organizationsData = JSON.parse(organizationDataElement.textContent);
        const container = document.getElementById('organization-list');
        if (!container) {
            return;
        }
        const configElement = document.getElementById('organization-list-config');
        const root = createRoot(container);
        let rankingPresets: RankingPreset[] | undefined;
        try {
            const presetsElement = document.getElementById('ranking-presets');
            rankingPresets = presetsElement?.textContent ? JSON.parse(presetsElement.textContent) : undefined;
        } catch (error) {
            console.error('Error parsing ranking presets:', error);
        }
        const currentAlgorithmId = Number(configElement?.getAttribute('data-current-algorithm-id'));
        const render = () => root.render(<OrganizationList organizations={organizationsData}
            canSuggestCompany={configElement?.getAttribute('data-can-suggest-company') === 'true'}
            isAuthenticated={container.dataset.authenticated === 'true'}
            signInUrlTemplate={configElement?.getAttribute('data-sign-in-url-template') || ''}
            rankingPresets={rankingPresets}
            currentAlgorithmId={Number.isInteger(currentAlgorithmId) && currentAlgorithmId > 0 ? currentAlgorithmId : null}
            algorithmUrlTemplate={configElement?.getAttribute('data-algorithm-url-template') || undefined}/>);
        render();
        // The full-page-cached shell renders auth-neutral and app-nav.js
        // hydrates the auth flags per request (issue #470); re-render once
        // with the hydrated values.
        document.addEventListener('crank:auth-hydrated', render, { once: true });
    } catch (error) {
        console.error('Error parsing organization data:', error);
    }
});
