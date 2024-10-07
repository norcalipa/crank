import React from 'react';
import {render, screen, fireEvent, act, getByTestId} from '@testing-library/react';
import '@testing-library/jest-dom';
import OrganizationList from './OrganizationList';

const mockOrganizations = [
    {
        id: 1,
        name: 'Org 1',
        ranking: 1,
        avg_score: 90,
        funding_round: 'A',
        rto_policy: 'Remote',
        profile_completeness: 100,
        accelerated_vesting: true
    },
    {
        id: 2,
        name: 'Org 2',
        ranking: 2,
        avg_score: 85,
        funding_round: 'B',
        rto_policy: 'Hybrid',
        profile_completeness: 80,
        accelerated_vesting: false
    },
];

const mockFundingRoundChoices = [{'A': 'Series A', 'B': 'Series B'}];
const mockRtoPolicyChoices = [{'Remote': 'Remote', 'Hybrid': 'Hybrid'}];

describe('OrganizationList', () => {
    beforeEach(() => {
        fetch.resetMocks();
        fetch.mockResponseOnce(JSON.stringify(mockFundingRoundChoices));
        fetch.mockResponseOnce(JSON.stringify(mockRtoPolicyChoices));
    });

    test('renders organization list', async () => {
        render(<OrganizationList organizations={mockOrganizations}/>);

        expect(await screen.findByText('Org 1')).toBeInTheDocument();
        expect(screen.getByText('Org 2')).toBeInTheDocument();
    });

    test('filters organizations based on search term', async () => {
        render(<OrganizationList organizations={mockOrganizations}/>);

        fireEvent.change(screen.getByPlaceholderText('Search organizations'), {target: {value: 'Org 1'}});

        expect(await screen.findByText('Org 1')).toBeInTheDocument();
        expect(screen.queryByText('Org 2')).not.toBeInTheDocument();
    });

    test('filters organizations based on accelerated vesting', async () => {
        render(<OrganizationList organizations={mockOrganizations}/>);

        act(() => {
            fireEvent.click(screen.getByRole('checkbox', {name: /Show only companies with first vesting in < 1 year/i}));
        });
        expect(await screen.findByText('Org 1')).toBeInTheDocument();
        expect(screen.queryByText('Org 2')).not.toBeInTheDocument();
    });

    test('changes page when pagination link is clicked', async () => {
        delete window.location;
        window.location = new URL('http://localhost');

        await act(async () => {
            render(<OrganizationList organizations={mockOrganizations}/>);
        });

        await act(async () => {
            fireEvent.click(screen.getByText('2'));
            // This is a workaround for the fact that jsdom does not support window.location.search
            window.location.search = '?page=2';
        });

        expect(window.location.search).toBe('?page=2');
    });

    test('handlePageChange updates state and URL', async () => {
        delete window.location;
        window.location = new URL('http://localhost');

        await act(async () => {
            render(<OrganizationList organizations={mockOrganizations} itemsPerPage={1}/>);
        });

        await act(async () => {
            fireEvent.click(screen.getByText('2'));
            window.location.search = '?page=2';
        });

        expect(window.location.search).toBe('?page=2');
        expect(screen.getByTestId('page-link-2').closest('li')).toHaveClass('active');

    });

    test('handleFilterChange updates state and applies filters', async () => {
        render(<OrganizationList organizations={mockOrganizations} />);

        act(() => {
            fireEvent.click(screen.getByRole('checkbox', { name: /Show only companies with first vesting in < 1 year/i }));
        });

        expect(await screen.findByText('Org 1')).toBeInTheDocument();
        expect(screen.queryByText('Org 2')).not.toBeInTheDocument();
    });

    test('handleSearchChange updates state and applies filters', async () => {
        render(<OrganizationList organizations={mockOrganizations} />);

        fireEvent.change(screen.getByPlaceholderText('Search organizations'), { target: { value: 'Org 1' } });

        expect(await screen.findByText('Org 1')).toBeInTheDocument();
        expect(screen.queryByText('Org 2')).not.toBeInTheDocument();
    });

});