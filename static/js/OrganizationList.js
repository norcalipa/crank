import React from 'react';
import ReactDOM from 'react-dom';

class OrganizationList extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            organizations: props.organizations,
            fundingRoundChoices: {},
            rtoPolicyChoices: {},
            currentPage: 1,
            itemsPerPage: 15,
            acceleratedVesting: false
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

    handlePageChange = (pageNumber) => {
        this.setState({currentPage: pageNumber});
    }

    handleFilterChange = () => {
        this.setState(prevState => {
            const newAcceleratedVesting = !prevState.acceleratedVesting;
            const filteredOrganizations = newAcceleratedVesting
                ? prevState.organizations.filter(org => org.accelerated_vesting)
                : prevState.organizations;

            let newPage = prevState.currentPage;
            while (newPage > 1 && filteredOrganizations.slice((newPage - 1) * prevState.itemsPerPage, newPage * prevState.itemsPerPage).length === 0) {
                newPage--;
            }

            return {
                acceleratedVesting: newAcceleratedVesting,
                currentPage: newPage
            };
        });
    }

    render() {
        const {
            organizations,
            fundingRoundChoices,
            rtoPolicyChoices,
            currentPage,
            itemsPerPage,
            acceleratedVesting
        } = this.state;

        // Filter organizations based on accelerated vesting
        const filteredOrganizations = acceleratedVesting
            ? organizations.filter(org => org.accelerated_vesting)
            : organizations;

        // Calculate the current items to display
        const indexOfLastItem = currentPage * itemsPerPage;
        const indexOfFirstItem = indexOfLastItem - itemsPerPage;
        const currentOrganizations = filteredOrganizations.slice(indexOfFirstItem, indexOfLastItem);

        // Calculate page numbers
        const pageNumbers = [];
        for (let i = 1; i <= Math.ceil(organizations.length / itemsPerPage); i++) {
            pageNumbers.push(i);
        }

        return (
            <div>
                {filteredOrganizations.length === 0 ? (
                    <p>No organizations are available or the Score Algorithm you specified doesn't exist.</p>
                ) : (
                    <>
                        <div className="pagination">
                            <ul className="pagination">
                                {pageNumbers.map(number => (
                                    <li className={`page-item ${currentPage === number ? 'active' : ''}`}>
                                        <a className="page-link" key={number}
                                           onClick={() => this.handlePageChange(number)}>{number}</a>
                                    </li>
                                ))}
                            </ul>
                        </div>
                        <div className="form-check">
                            <label className="form-check-label" htmlFor="acceleratedVesting">
                                Show only companies with first vesting in &lt; 1 year
                            </label>
                            <input
                                type="checkbox"
                                className="form-check-input"
                                id="acceleratedVesting"
                                checked={acceleratedVesting}
                                onChange={this.handleFilterChange}
                            />
                        </div>
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
                            {currentOrganizations.map(org => (
                                <tr key={org.id}>
                                    <td>{org.ranking}</td>
                                    <td><a href={`/organization/${org.id}`}>{org.name}</a></td>
                                    <td>{org.avg_score.toFixed(2)}</td>
                                    <td>{fundingRoundChoices[org.funding_round]}</td>
                                    <td>{rtoPolicyChoices[org.rto_policy]}</td>
                                    <td>{org.profile_completeness.toFixed(0)}%</td>
                                </tr>
                            ))}
                            </tbody>
                        </table>
                    </>
                )}
            </div>
        );
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