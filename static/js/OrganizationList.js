import React from 'react';
import ReactDOM from 'react-dom';

class OrganizationList extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            organizations: props.organizations,
            filteredOrganizations: props.organizations,
            fundingRoundChoices: {},
            rtoPolicyChoices: {},
            currentPage: this.getCurrentPageFromQueryString(),
            itemsPerPage: 15,
            acceleratedVesting: false,
            searchTerm: ''
        };
    }

    componentDidMount() {
        fetch('/api/funding-round-choices/')
            .then(response => response.json())
            .then(data => {
                this.setState({fundingRoundChoices: data});
            })
            .catch(error => console.error('Error fetching funding round choices:', error));

        fetch('/api/rto-policy-choices/')
            .then(response => response.json())
            .then(data => {
                this.setState({rtoPolicyChoices: data});
            })
            .catch(error => console.error('Error fetching RTO policy choices:', error));
    }

    getCurrentPageFromQueryString() {
        const params = new URLSearchParams(window.location.search);
        const page = params.get('page');
        return page ? parseInt(page, 10) : 1;
    }

    handlePageChange = (pageNumber) => {
        this.setState({currentPage: pageNumber});
        const params = new URLSearchParams(window.location.search);
        params.set('page', pageNumber);
        window.history.pushState({}, '', `${window.location.pathname}?${params.toString()}`);
    }

    handleFilterChange = () => {
        this.setState(prevState => {
            const newAcceleratedVesting = !prevState.acceleratedVesting;
            return {
                acceleratedVesting: newAcceleratedVesting
            };
        }, this.applyFilters);
    }

    handleSearchChange = (event) => {
        const searchTerm = event.target.value.toLowerCase();
        this.setState({searchTerm: searchTerm}, this.applyFilters);
    }

    applyFilters = () => {
        this.setState(prevState => {
            const {organizations, acceleratedVesting, searchTerm} = prevState;
            const filteredOrganizations = organizations.filter(org => {
                const matchesSearch = org.name.toLowerCase().includes(searchTerm);
                const matchesFilter = !acceleratedVesting || org.accelerated_vesting;
                return matchesSearch && matchesFilter;
            });
            return {
                filteredOrganizations: filteredOrganizations, currentPage: 1
            };
        });
    }

    render() {
        const {
            filteredOrganizations,
            fundingRoundChoices,
            rtoPolicyChoices,
            currentPage,
            itemsPerPage,
            acceleratedVesting,
            searchTerm
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
                    <table className="table">
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
                        {currentOrganizations.map(org => (<tr key={org.id}>
                            <td>{org.ranking}</td>
                            <td><a href={`/organization/${org.id}`}>{org.name}</a></td>
                            <td>{org.avg_score.toFixed(2)}</td>
                            <td>{fundingRoundChoices[org.funding_round]}</td>
                            <td>{rtoPolicyChoices[org.rto_policy]}</td>
                            <td>{org.profile_completeness.toFixed(0)}%</td>
                        </tr>))}
                        </tbody>
                    </table>
                </div>)}
            </>
        </div>);
    }
}

export default OrganizationList;

document.addEventListener('DOMContentLoaded', () => {
    const organizationDataElement = document.getElementById('organization-data');
    if (organizationDataElement) {
        try {
            const organizationsData = JSON.parse(organizationDataElement.textContent);
            ReactDOM.render(<OrganizationList
                organizations={organizationsData}/>, document.getElementById('organization-list'));
        } catch (error) {
            console.error('Error parsing organization data:', error);
        }
    }
});