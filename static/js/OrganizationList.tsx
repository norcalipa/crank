// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createRoot} from "react-dom/client";
import CompanyView, {CompanyViewStatus} from './company/CompanyView';
import {CompanyDetailsOrganization, CorrectionSource} from './company/CompanyDetails';
import {isCompanyId, parseCompanyId} from './company/companyId';
import EvidenceSummary, {evidenceDescription} from './evidence/EvidenceSummary';
import {EvidenceSummaryData, RTO_POLICY_LABELS} from './labels';
import {closeSuggestCompany, COMPANY_OPEN_EVENT, openSuggestCompany} from './suggestCompany/controller';
import {installPositionTracking, PositionAnchor, restoreResultPosition, saveResultPosition} from './workspace/position';
import {
    clearWorkspaceContext,
    closeAssistant,
    getWorkspaceSnapshot,
    openAssistant,
    registerCompanyTarget,
    registerFilterTarget,
    setWorkspaceContext,
    setWorkspaceEntity,
    subscribeWorkspace,
} from './workspace/store';
import {RtoPolicyCode, WORKSPACE_FOCUS_EVENT, WorkspaceFilters} from './workspace/types';

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
    rating_dimensions_covered?: number;
    rating_dimensions_total?: number;
    evidence?: EvidenceSummaryData | null;
    accelerated_vesting: boolean;
    url?: string;
    type?: string;
    gives_ratings?: boolean;
    public?: boolean;
    avg_scores?: ScoreDetail[];
}

// "N of M" is exact when the server sent the dimension counts; a row without
// them (old cache entry, fixture) falls back to the completeness percentage.
const ratingCoverageText = (org: Organization): string =>
    org.rating_dimensions_total !== undefined && org.rating_dimensions_covered !== undefined
        ? `${org.rating_dimensions_covered} of ${org.rating_dimensions_total}`
        : `${org.profile_completeness.toFixed(0)}%`;

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
    // view's sign-in CTA (issue #465 AC-7); see
    // crank/views/index.py.
    signInUrlTemplate?: string;
    // Ranking presets (issue #478): rendered server-side as non-user-specific
    // JSON. When absent (fixture pages, older cached shells) the preset
    // select is omitted.
    rankingPresets?: RankingPreset[];
    currentAlgorithmId?: number | null;
    algorithmUrlTemplate?: string;
}

// Query parameters the list preserves when it rewrites the URL; anything
// else on the incoming URL is dropped (issue #479). The scoring preset lives
// in the path (/algo/<id>/), not the query.
const ALLOWED_QUERY_PARAMS = ['search', 'accelerated_vesting', 'page', 'company', 'rto'];

// Issue #484: `rto` is the only enum filter; any other value is ignored.
const RTO_CODES: readonly string[] = ['R', 'H', 'O'];
function parseRtoPolicy(value: string | null): RtoPolicyCode | '' {
    return value !== null && RTO_CODES.includes(value) ? value as RtoPolicyCode : '';
}

function allowlistedParams(search: string): URLSearchParams {
    const params = new URLSearchParams();
    new URLSearchParams(search).forEach((value, key) => {
        if (ALLOWED_QUERY_PARAMS.includes(key) && !params.has(key)) {
            params.set(key, value);
        }
    });
    return params;
}

interface CompanyViewState {
    id: number;
    status: CompanyViewStatus | 'error';
    organization: Organization | null;
    // Name of the list row while the record loads, so the heading is not blank.
    name?: string;
}

// Marks the history entry that opening a company added, so Back to results
// knows it can go back instead of rewriting the URL (issue #486).
const VIEW_ENTRY_KEY = 'crankCompanyView';
// Where the scroll tracker keeps its result position (workspace/position.ts).
const POSITION_STATE_KEY = 'crankPosition';
// Result rows and cards only: the company view and the correction form carry
// the attribute too, and neither is a place the results can be restored to.
const RESULT_ANCHOR_SELECTOR = '.organization-row[data-organization-id], .organization-card[data-organization-id]';
// `:root` smooths scrolling; a restored row must be where it is read at once.
const INSTANT = 'instant' as ScrollBehavior;

interface OrganizationListState {
    organizations: Organization[];
    filteredOrganizations: Organization[];
    fundingRoundChoices: Record<string, string>;
    rtoPolicyChoices: Record<string, string>;
    currentPage: number;
    itemsPerPage: number;
    acceleratedVesting: boolean;
    rtoPolicy: RtoPolicyCode | '';
    searchTerm: string;
    // The company shown instead of the results (issue #486); null when the
    // results are showing. `organization` is null while the record loads for an
    // id that is not a list row, and stays null for an id that is gone.
    view: CompanyViewState | null;
    choicesStatus: 'loading' | 'ready' | 'error';
}

class OrganizationList extends React.Component<OrganizationListProps, OrganizationListState> {
    constructor(props: OrganizationListProps) {
        super(props);
        const urlState = this.getUrlState();
        this.state = {
            organizations: props.organizations,
            filteredOrganizations: this.filterOrganizations(props.organizations, urlState.searchTerm, urlState.acceleratedVesting, urlState.rtoPolicy),
            fundingRoundChoices: {},
            rtoPolicyChoices: {},
            currentPage: urlState.currentPage,
            itemsPerPage: props.itemsPerPage || 15,
            acceleratedVesting: urlState.acceleratedVesting,
            rtoPolicy: urlState.rtoPolicy,
            searchTerm: urlState.searchTerm,
            view: null,
            choicesStatus: 'loading'
        };
    }

    // Monotonically increasing generation for company-view intents. Every
    // open/close claims the next value so an in-flight record fetch can detect,
    // when its response arrives, that a newer intent has superseded it.
    private modalGeneration = 0;
    // Back was requested through history.back(); the popstate it causes does
    // the closing, so further close requests in the meantime are ignored.
    private closeRequested = false;
    // Private state was dropped (a purge, or the store wiped for another
    // account): the view closed in place for it, once.
    private closedAfterPurge = false;
    // The store's account went from known to unknown, which only a wipe does:
    // a context cleared by the reader leaves the account as it is.
    private privateStateDropped = false;
    private lastAccountStatus = getWorkspaceSnapshot().account.status;

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
        window.addEventListener(COMPANY_OPEN_EVENT, this.handleCompanyOpenEvent);
        document.addEventListener('crank:private-state-purged', this.handlePrivateStatePurged);
        this.normalizeCurrentPage();
        this.openCompanyFromUrl();
        // Issue #479: keep the shared workspace context in step with this
        // list, restore the Back-navigation scroll position, and close the
        // company view when the assistant's "Clear context" removes it.
        this.reportContext();
        this.stopPositionTracking = installPositionTracking(this.visibleOrganizationAnchor);
        restoreResultPosition(this.findOrganizationAnchor);
        this.restoredScrollY = window.scrollY;
        this.lastAccountStatus = getWorkspaceSnapshot().account.status;
        this.unsubscribeWorkspace = subscribeWorkspace(this.handleWorkspaceChange);
        this.unregisterFilterTarget = registerFilterTarget(this.applyAssistantFilters);
        this.unregisterCompanyTarget = registerCompanyTarget(this.openCompanyById);
    }

    componentDidUpdate(_prevProps: unknown, prevState: OrganizationListState) {
        const companyChanged = prevState.view?.organization?.id !== this.state.view?.organization?.id;
        // A view that ended without a company (gone, or failed to load) must
        // not leave the previous company in the assistant. The URL keeps its
        // ?company=, so only the assistant's company goes.
        const failed = (status?: string) => status === 'not-found' || status === 'error';
        if (failed(this.state.view?.status) && !failed(prevState.view?.status)
            && this.reportedOrganizationId !== null) {
            if (getWorkspaceSnapshot().context?.organizationId === this.reportedOrganizationId) {
                setWorkspaceEntity({});
            }
            this.reportedOrganizationId = null;
        }
        if (companyChanged
            || prevState.searchTerm !== this.state.searchTerm
            || prevState.acceleratedVesting !== this.state.acceleratedVesting
            || prevState.rtoPolicy !== this.state.rtoPolicy
            || prevState.currentPage !== this.state.currentPage) {
            this.reportContext();
        }
    }

    componentWillUnmount() {
        window.removeEventListener('popstate', this.handlePopState);
        window.removeEventListener(COMPANY_OPEN_EVENT, this.handleCompanyOpenEvent);
        document.removeEventListener('crank:private-state-purged', this.handlePrivateStatePurged);
        this.stopPositionTracking?.();
        this.unsubscribeWorkspace?.();
        this.unregisterFilterTarget?.();
        this.unregisterCompanyTarget?.();
    }

    private unregisterFilterTarget?: () => void;
    private unregisterCompanyTarget?: () => void;
    private stopPositionTracking?: () => void;
    private restoredAfterHydration = false;
    private restoredScrollY = 0;

    // Organization id of the first result on screen. Table rows and cards
    // both carry the id; whichever layout is displayed has a non-empty box.
    // With a company open there is none: the view is not a result.
    visibleOrganizationAnchor = (): PositionAnchor | null => {
        const candidates = document.querySelectorAll<HTMLElement>(RESULT_ANCHOR_SELECTOR);
        for (const element of Array.from(candidates)) {
            const rect = element.getBoundingClientRect();
            if (rect.height > 0 && rect.bottom > 0) {
                return {id: element.getAttribute('data-organization-id') as string, offset: rect.top};
            }
        }
        return null;
    };

    findOrganizationAnchor = (id: string): HTMLElement | null => {
        if (!/^\d+$/.test(id)) {
            return null;
        }
        const candidates = document.querySelectorAll<HTMLElement>(RESULT_ANCHOR_SELECTOR);
        return Array.from(candidates).find((element) => element.getAttribute('data-organization-id') === id
            && element.getBoundingClientRect().height > 0) ?? null;
    };

    // Account hydration can reopen the drawer/docked panel and reflow rows
    // into cards, so the mount-time restore is repeated once, by result
    // anchor, after the account settles — unless the user already scrolled.
    private restoreAfterHydration = () => {
        if (this.restoredAfterHydration || getWorkspaceSnapshot().account.status === 'unknown') {
            return;
        }
        this.restoredAfterHydration = true;
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
            if (Math.abs(window.scrollY - this.restoredScrollY) <= 1) {
                restoreResultPosition(this.findOrganizationAnchor);
            }
        }));
    };
    private unsubscribeWorkspace?: () => void;
    // Company id this list last reported to the workspace, so a close only
    // removes context the list itself set (never the Ask CTA's).
    private reportedOrganizationId: number | null = null;
    // Company the user asked the assistant about from the view: the context
    // is then the assistant's, so leaving the view must not clear it, while
    // "Clear context" still takes the view with it (issue #486).
    private askedOrganizationId: number | null = null;
    // The company the view was first opened with from the results, so Back
    // can focus its row after a switch to a company the list does not show.
    private openedRowId: number | null = null;

    // Applies assistant-proposed filters (issue #484), one history entry, on
    // top of the current search; page resets to 1. Only enum values reach here.
    applyAssistantFilters = (filters: WorkspaceFilters): boolean => {
        // Filters act on the results: show them, leaving the company view in
        // place of going back through history (issue #486).
        this.leaveViewInPlace();
        const rtoPolicy = filters.rtoPolicy ?? this.state.rtoPolicy;
        const acceleratedVesting = filters.acceleratedVesting === true || this.state.acceleratedVesting;
        if (rtoPolicy === this.state.rtoPolicy && acceleratedVesting === this.state.acceleratedVesting) {
            return true;
        }
        this.setState({
            rtoPolicy,
            acceleratedVesting,
            filteredOrganizations: this.filterOrganizations(
                this.state.organizations, this.state.searchTerm, acceleratedVesting, rtoPolicy),
            currentPage: 1,
        });
        this.updateUrl(1, this.state.searchTerm, acceleratedVesting, false, rtoPolicy);
        return true;
    };

    reportedFilters = (): WorkspaceFilters => {
        const {acceleratedVesting, rtoPolicy} = this.state;
        const filters: WorkspaceFilters = {};
        if (rtoPolicy) {
            filters.rtoPolicy = rtoPolicy;
        }
        if (acceleratedVesting) {
            filters.acceleratedVesting = true;
        }
        return filters;
    };

    reportContext = () => {
        const {searchTerm, currentPage} = this.state;
        const selectedOrganization = this.state.view?.organization ?? null;
        const {currentAlgorithmId} = this.props;
        const view = {
            filters: this.reportedFilters(),
            ...(currentAlgorithmId ? {algorithmId: currentAlgorithmId} : {}),
        };
        if (selectedOrganization) {
            this.reportedOrganizationId = selectedOrganization.id;
            setWorkspaceContext({
                surface: 'company',
                organizationId: selectedOrganization.id,
                organizationName: selectedOrganization.name,
                ...view,
            });
            return;
        }
        const context = getWorkspaceSnapshot().context;
        // Switching companies from inside a view: while the next record loads
        // the previous company's context stays. Clearing it would also strip
        // `company` from the URL the switch just wrote.
        if (this.state.view?.status === 'loading' && this.reportedOrganizationId !== null) {
            return;
        }
        if (this.reportedOrganizationId !== null
            && context?.organizationId === this.reportedOrganizationId) {
            clearWorkspaceContext();
        }
        this.reportedOrganizationId = null;
        const keepsCompany = getWorkspaceSnapshot().context?.organizationId !== undefined;
        setWorkspaceContext(keepsCompany
            ? {searchTerm, page: currentPage, ...view}
            : {surface: 'rankings', searchTerm, page: currentPage, ...view});
    };

    handleWorkspaceChange = () => {
        this.restoreAfterHydration();
        const {account} = getWorkspaceSnapshot();
        if (account.status !== 'unknown') {
            this.privateStateDropped = false;
        } else if (this.lastAccountStatus !== 'unknown') {
            this.privateStateDropped = true;
        }
        this.lastAccountStatus = account.status;
        const shown = this.state.view?.organization;
        if (shown
            && (this.reportedOrganizationId === shown.id || this.askedOrganizationId === shown.id)
            && getWorkspaceSnapshot().context?.organizationId !== shown.id) {
            if (this.privateStateDropped) {
                this.closeAfterPurge();
            } else {
                this.closeView();
            }
        }
    };

    // The workspace purge (sign-out, account switch) drops every private
    // entity; the company view goes with it, whatever the store held.
    handlePrivateStatePurged = () => {
        this.closeAfterPurge();
    };

    // Never through history: a traversal queued in the same task as the
    // sign-out form's submission could cancel it. Once per purge, whichever of
    // the two listeners hears it first.
    private closeAfterPurge = () => {
        if (!this.state.view || this.closedAfterPurge) {
            return;
        }
        this.closedAfterPurge = true;
        this.leaveViewInPlace();
    };

    // A user sent to sign-in from a company's view returns with that company
    // id in the URL (issue #465 AC-7): open the same view on load so the round
    // trip through sign-in feels seamless. A missing or malformed id is
    // silently ignored, never a console error and never a request.
    openCompanyFromUrl = () => {
        const companyId = parseCompanyId(new URLSearchParams(window.location.search).get('company'));
        if (companyId !== null) {
            this.openView(companyId, 'url');
        }
    };

    // "Back to <Company>" from the correction form (issue #477): shows the
    // company's view through the same path as `?company=`. A malformed id is
    // ignored; a view that is already open stays as it is.
    handleCompanyOpenEvent = (event: Event) => {
        const organizationId = (event as CustomEvent).detail?.organizationId;
        if (!isCompanyId(organizationId)) return;
        this.openCompanyById(organizationId);
    };

    // The assistant's typed target: opens the view for any valid id, ranked in
    // this list or not; false only for an id that cannot be one.
    openCompanyById = (organizationId: number): boolean => {
        const opened = this.openView(organizationId, 'user');
        // On a phone the assistant is a sheet over the page: a company opened
        // from inside it would stay hidden behind it, so the sheet closes, as
        // it did when the company was a dialog.
        if (opened && document.body.classList.contains('assistant-sheet')) {
            closeAssistant();
        }
        return opened;
    };

    getUrlState = () => {
        const params = new URLSearchParams(window.location.search);
        const requestedPage = Number(params.get('page'));
        return {
            currentPage: Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1,
            searchTerm: params.get('search') || '',
            acceleratedVesting: params.get('accelerated_vesting') === '1',
            rtoPolicy: parseRtoPolicy(params.get('rto'))
        };
    };

    filterOrganizations = (organizations: Organization[], searchTerm: string, acceleratedVesting: boolean, rtoPolicy: RtoPolicyCode | '') => {
        let filteredOrganizations = organizations;

        if (rtoPolicy) {
            filteredOrganizations = filteredOrganizations.filter(org => org.rto_policy === rtoPolicy);
        }

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
        const params = allowlistedParams(window.location.search);
        params.set('page', pageNumber.toString());
        const query = params.toString();
        return `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`;
    };

    updateUrl = (pageNumber: number, searchTerm: string, acceleratedVesting: boolean, replace = false, rtoPolicy: RtoPolicyCode | '' = this.state.rtoPolicy) => {
        const params = allowlistedParams(window.location.search);
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
        if (rtoPolicy) {
            params.set('rto', rtoPolicy);
        } else {
            params.delete('rto');
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
            urlState.acceleratedVesting,
            urlState.rtoPolicy
        );
        const pageCount = Math.max(1, Math.ceil(filteredOrganizations.length / this.state.itemsPerPage));
        this.setState({
            currentPage: Math.min(urlState.currentPage, pageCount),
            searchTerm: urlState.searchTerm,
            acceleratedVesting: urlState.acceleratedVesting,
            rtoPolicy: urlState.rtoPolicy,
            filteredOrganizations
        });
        // Back and Forward move between the results entry and the company's:
        // the URL says which one is showing.
        this.closeRequested = false;
        const companyId = parseCompanyId(new URLSearchParams(window.location.search).get('company'));
        if (companyId !== null) {
            this.openView(companyId, 'url');
        } else if (this.state.view) {
            this.showResults();
        }
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
        const filteredOrganizations = this.filterOrganizations(this.state.organizations, this.state.searchTerm, acceleratedVesting, this.state.rtoPolicy);
        this.setState({acceleratedVesting, filteredOrganizations, currentPage: 1});
        this.updateUrl(1, this.state.searchTerm, acceleratedVesting);
    };

    handleSearchChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const searchTerm = event.target.value;
        const filteredOrganizations = this.filterOrganizations(this.state.organizations, searchTerm, this.state.acceleratedVesting, this.state.rtoPolicy);
        this.setState({searchTerm, filteredOrganizations, currentPage: 1});
        this.updateUrl(1, searchTerm, this.state.acceleratedVesting);
    };

    handleClearFilters = () => {
        this.setState({searchTerm: '', acceleratedVesting: false, rtoPolicy: '', filteredOrganizations: this.state.organizations, currentPage: 1});
        this.updateUrl(1, '', false, false, '');
    };

    handleRemoveSearch = () => {
        const filteredOrganizations = this.filterOrganizations(this.state.organizations, '', this.state.acceleratedVesting, this.state.rtoPolicy);
        this.setState({searchTerm: '', filteredOrganizations, currentPage: 1});
        this.updateUrl(1, '', this.state.acceleratedVesting);
    };

    handleRemoveAcceleratedVesting = () => {
        const filteredOrganizations = this.filterOrganizations(this.state.organizations, this.state.searchTerm, false, this.state.rtoPolicy);
        this.setState({acceleratedVesting: false, filteredOrganizations, currentPage: 1});
        this.updateUrl(1, this.state.searchTerm, false);
    };

    handleRemoveRtoPolicy = () => {
        const filteredOrganizations = this.filterOrganizations(this.state.organizations, this.state.searchTerm, this.state.acceleratedVesting, '');
        this.setState({rtoPolicy: '', filteredOrganizations, currentPage: 1});
        this.updateUrl(1, this.state.searchTerm, this.state.acceleratedVesting, false, '');
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
        if (this.state.rtoPolicy) {
            params.set('rto', this.state.rtoPolicy);
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
        this.openView(organization.id, 'user');
    };

    // Shows the company in place of the results (issue #486). `user` opens add
    // one history entry the first time, so Back returns to the results; a
    // view opened from inside a view replaces it. `url` opens (`?company=` on
    // load, Back/Forward) never touch history: the URL already says it.
    openView = (id: number, source: 'user' | 'url'): boolean => {
        if (!isCompanyId(id)) {
            return false;
        }
        const current = this.state.view;
        if (current && current.id === id) {
            return true;
        }
        const generation = ++this.modalGeneration;
        this.closeRequested = false;
        this.closedAfterPurge = false;
        this.privateStateDropped = false;
        this.askedOrganizationId = null;
        if (current === null) {
            this.openedRowId = id;
        }
        const row = this.state.organizations.find(org => org.id === id) ?? null;
        closeSuggestCompany();
        if (source === 'user') {
            this.writeViewEntry(id, current !== null);
        }
        if (row && row.url && row.type) {
            this.setState({view: {id, status: 'ready', organization: row}});
            return true;
        }
        this.setState({view: {id, status: 'loading', organization: null, name: row?.name}});
        this.loadRecord(id, row, generation);
        return true;
    };

    private writeViewEntry = (id: number, replace: boolean) => {
        const params = allowlistedParams(window.location.search);
        params.set('company', String(id));
        const url = `${window.location.pathname}?${params.toString()}${window.location.hash}`;
        if (replace) {
            window.history.replaceState(window.history.state, '', url);
            return;
        }
        // The results entry remembers where it was scrolled to and which row
        // was first on screen; Back puts that back.
        const anchor = this.visibleOrganizationAnchor();
        saveResultPosition(anchor?.id ?? null, anchor?.offset ?? 0);
        window.history.pushState({[VIEW_ENTRY_KEY]: true}, '', url);
    };

    // Loads `GET /api/organizations/<id>/` for a company the list row does not
    // fully describe (the ranking payload carries no url or type) or that is
    // not a list row at all. A 404 is the company's disappearance, not an
    // error; any other failure still shows a ranked row from what the list has.
    private loadRecord = (id: number, row: Organization | null, generation: number) => {
        // A response for a company that is no longer the one shown (another
        // was opened, or the user went back) is dropped, errors included.
        const current = () => generation === this.modalGeneration && this.state.view?.id === id;
        const apply = (view: CompanyViewState, organizations?: Organization[]) => {
            this.setState(previous => ({view, organizations: organizations ?? previous.organizations}));
        };
        fetch(`/api/organizations/${id}/`)
            .then(response => {
                if (response.status === 404) {
                    if (current()) apply({id, status: 'not-found', organization: null});
                    return undefined;
                }
                if (response.ok === false) {
                    throw new Error(`HTTP ${response.status}`);
                }
                return response.json().then(data => {
                    if (!current()) return;
                    const organization = {...row, ...data} as Organization;
                    apply(
                        {id, status: 'ready', organization},
                        row ? this.state.organizations.map(org => (org.id === id ? organization : org)) : undefined,
                    );
                });
            })
            .catch(error => {
                if (!current()) return;
                console.error('Error fetching organization details:', error);
                apply(row ? {id, status: 'ready', organization: row} : {id, status: 'error', organization: null});
            });
    };

    retryView = () => {
        const view = this.state.view;
        if (!view) {
            return;
        }
        const generation = ++this.modalGeneration;
        this.setState({view: {id: view.id, status: 'loading', organization: null}});
        this.loadRecord(view.id, null, generation);
    };

    // Back to results, the button and the assistant's "Clear context". When
    // opening added a history entry, go back through it so Back and Forward
    // stay in step; otherwise (a deep link) rewrite the URL in place.
    closeView = () => {
        if (!this.state.view || this.closeRequested) {
            return;
        }
        ++this.modalGeneration;
        if (window.history.state?.[VIEW_ENTRY_KEY] === true) {
            this.closeRequested = true;
            window.history.back();
            return;
        }
        this.leaveViewInPlace();
    };

    private leaveViewInPlace = () => {
        if (!this.state.view) {
            return;
        }
        const params = allowlistedParams(window.location.search);
        params.delete('company');
        const query = params.toString();
        // The position on this entry was saved while the company was on
        // screen, so it is dropped with the marker; the rest of the state stays.
        const {[POSITION_STATE_KEY]: _position, [VIEW_ENTRY_KEY]: _marker, ...kept} = window.history.state ?? {};
        window.history.replaceState(
            kept, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`);
        this.showResults(false);
    };

    // Shows the results again exactly as they were: the region was only
    // hidden, so search, filters and page are untouched. Through history the
    // entry's own saved position is restored; in place there is none to
    // restore. The row the view was opened from takes focus and is never
    // left off screen.
    private showResults = (throughHistory = true) => {
        const id = this.state.view?.id;
        const openedRowId = this.openedRowId;
        this.openedRowId = null;
        ++this.modalGeneration;
        this.setState({view: null}, () => {
            // After the commit that shows the results again (and any list
            // state a popstate restored in the same turn) has painted.
            window.requestAnimationFrame(() => {
                if (throughHistory) {
                    restoreResultPosition(this.findOrganizationAnchor);
                }
                // The row the view was opened from; after a switch to a
                // company that is gone it is still the one to come back to.
                // (`String(null)` is not an id, so a missing one finds no row.)
                const row = this.findOrganizationAnchor(String(openedRowId))
                    ?? this.findOrganizationAnchor(String(id));
                if (row) {
                    this.bringRowIntoView(row, throughHistory);
                } else if (!throughHistory) {
                    window.scrollTo({top: 0, behavior: INSTANT});
                }
                (row ?? this.resultsRef.current)?.focus({preventScroll: true});
            });
        });
    };

    // A restored row that is at least partly on screen stays where it was
    // restored to; one that is not is centred, clear of the top bar and the
    // Assistant bar. In place the row must be fully inside.
    private bringRowIntoView = (row: HTMLElement, partlyIsEnough: boolean) => {
        const rect = row.getBoundingClientRect();
        const visible = partlyIsEnough
            ? rect.bottom > 0 && rect.top < window.innerHeight
            : rect.top >= 0 && rect.bottom <= window.innerHeight;
        if (!visible) {
            const top = Math.round(rect.top + window.scrollY - (window.innerHeight - rect.height) / 2);
            window.scrollTo({top: Math.max(0, top), behavior: INSTANT});
        }
    };

    private resultsRef = React.createRef<HTMLDivElement>();

    handleAsk = () => {
        const organization = this.state.view?.organization;
        if (!organization) {
            return;
        }
        this.askedOrganizationId = organization.id;
        this.reportedOrganizationId = null;
        openAssistant({surface: 'company', organizationId: organization.id, organizationName: organization.name});
        // The assistant may never reach a composer (initialization error,
        // gated account); the shell still moves focus into it.
        window.dispatchEvent(new CustomEvent(WORKSPACE_FOCUS_EVENT));
    };

    handleSuggestCorrection = (source: CorrectionSource, fieldKey?: string) => {
        const organization = this.state.view?.organization;
        if (!organization) {
            return;
        }
        openSuggestCompany({
            kind: 'correction',
            source,
            companyName: organization.name,
            organizationId: organization.id,
            fieldKey,
        });
    };

    handleOpenSuggestModal = (source: 'rankings' | 'rankings_empty') => {
        openSuggestCompany({
            source,
            searchTerm: this.state.searchTerm,
            companyName: source === 'rankings_empty' && this.state.searchTerm ? this.state.searchTerm : undefined,
            page: this.state.currentPage
        });
    };

    renderChips = () => {
        const {searchTerm, acceleratedVesting, rtoPolicy, rtoPolicyChoices} = this.state;
        if (!searchTerm && !acceleratedVesting && !rtoPolicy) {
            return null;
        }
        const rtoLabel = rtoPolicy ? (rtoPolicyChoices[rtoPolicy] ?? RTO_POLICY_LABELS[rtoPolicy]) : '';
        return (<ul className="filter-chips" aria-label="Active filters">
            {searchTerm && <li><button type="button" className="filter-chip" data-testid="filter-chip-search"
                                       aria-label={`Remove filter: search "${searchTerm}"`} title={`Search: ${searchTerm}`}
                                       onClick={this.handleRemoveSearch}>
                <span className="filter-chip-text">Search: {searchTerm}</span> <span aria-hidden="true">×</span>
            </button></li>}
            {rtoPolicy && <li><button type="button" className="filter-chip" data-testid="filter-chip-rto"
                                      aria-label={`Remove filter: RTO policy ${rtoLabel}`}
                                      onClick={this.handleRemoveRtoPolicy}>
                <span className="filter-chip-text">RTO policy: {rtoLabel}</span> <span aria-hidden="true">×</span>
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
            rtoPolicy,
            searchTerm,
            view,
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
            choices[code] ?? (choicesStatus === 'loading'
                ? <span className="choices-skeleton" aria-hidden="true" data-testid="choice-skeleton"></span>
                : code);

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
            {view && (
                <CompanyView
                    organizationId={view.id}
                    status={view.status}
                    organization={view.organization as CompanyDetailsOrganization | null}
                    name={view.name}
                    isAuthenticated={this.props.isAuthenticated}
                    signInUrlTemplate={this.props.signInUrlTemplate}
                    onBack={this.closeView}
                    onRetry={this.retryView}
                    onAsk={this.handleAsk}
                    onSuggestCorrection={this.handleSuggestCorrection}
                />
            )}
            <div ref={this.resultsRef} tabIndex={-1} hidden={view !== null} className="organization-results-region"
                 data-testid="organization-results-region">
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
                {this.renderChips()}
                <div className="rankings-toolbar-count" role="status" aria-live="polite">
                    <div className="organization-results-count">{isEmpty ? 'Showing 0 organizations' : `Showing ${firstResult}-${lastResult} of ${filteredOrganizations.length} organizations`}</div>
                    <div className="choices-status-slot">
                    {choicesStatus === 'loading' && <div className="choices-status text-muted" data-testid="choices-status-loading"><span className="spinner-border spinner-border-sm" aria-hidden="true"></span><span>Loading funding round and RTO labels…</span></div>}
                    {choicesStatus === 'error' && <div className="choices-status choices-status-error alert alert-danger py-1 px-2 mt-1 mb-0" data-testid="choices-status-error">
                        <span>Couldn't load funding round and RTO labels, so raw codes are shown instead.</span>{' '}
                        <button type="button" className="btn btn-outline-danger btn-sm" data-testid="choices-retry" onClick={this.loadChoices}>Retry labels</button>
                    </div>}
                    </div>
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
                {(searchTerm || acceleratedVesting || rtoPolicy) && <button type="button" className="btn btn-primary" onClick={this.handleClearFilters}>Clear search and filters</button>}
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
                            <th className="col-score" aria-label={scoreLabel}>Company score{currentPreset && <>{' '}<span className="col-score-algorithm">{currentPreset.name}</span></>}</th>
                            <th className="col-funding">Funding Round</th>
                            <th className="col-rto">RTO Policy</th>
                            <th className="col-profile">Rating coverage</th>
                            <th className="col-evidence">Evidence</th>
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
                            aria-describedby={`organization-summary-${org.id}`}
                            className="organization-row"
                            data-organization-id={org.id}
                        >
                            <td className="col-rank">{org.ranking}</td>
                            <td className="col-name"><span className="organization-name" title={org.name}>{org.name}</span><span className="visually-hidden" id={`organization-summary-${org.id}`}>{evidenceDescription(ratingCoverageText(org), org.evidence)}</span></td>
                            <td className="col-score">{org.avg_score.toFixed(2)}</td>
                            <td className="col-funding">{choiceLabel(fundingRoundChoices, org.funding_round)}</td>
                            <td className="col-rto">{choiceLabel(rtoPolicyChoices, org.rto_policy)}</td>
                            <td className="col-profile">{ratingCoverageText(org)}</td>
                            <td className="col-evidence"><EvidenceSummary evidence={org.evidence}/></td>
                        </tr>))}
                        </tbody>
                    </table>
                </div>
                <div className="organization-cards" aria-label="Organization ranking cards">
                    {currentOrganizations.map(org => (
                        <article
                            key={`card-${org.id}`}
                            className="card organization-card"
                            data-organization-id={org.id}
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
                            aria-describedby={`organization-card-summary-${org.id}`}
                        >
                            <div className="card-body">
                                <span className="visually-hidden" id={`organization-card-summary-${org.id}`}>{evidenceDescription(ratingCoverageText(org), org.evidence)}</span>
                                <h2 className="h5 organization-card-name" title={org.name}>{org.name}</h2>
                                <div className="organization-card-score">
                                    <span className="organization-card-label">Company score{currentPreset && <span className="visually-hidden"> ({currentPreset.name})</span>}</span>
                                    #{org.ranking} · {org.avg_score.toFixed(2)}
                                </div>
                                <div>
                                    <span className="organization-card-label">Rating coverage</span>
                                    {ratingCoverageText(org)}
                                </div>
                                <div>
                                    <span className="organization-card-label">RTO policy</span>
                                    {choiceLabel(rtoPolicyChoices, org.rto_policy)}
                                </div>
                                <div>
                                    <span className="organization-card-label">Funding round</span>
                                    {choiceLabel(fundingRoundChoices, org.funding_round)}
                                </div>
                                <div className="organization-card-evidence">
                                    <span className="organization-card-label">Facts</span>
                                    <EvidenceSummary evidence={org.evidence} variant="card"/>
                                </div>
                            </div>
                        </article>
                    ))}
                </div>
                </div>
                {pageCount > 1 && renderPager('bottom')}
            </>)}
            </div>
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
