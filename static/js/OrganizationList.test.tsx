// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import * as React from 'react';

import OrganizationList from './OrganizationList';

interface Organization {
    id: number;
    name: string;
    ranking: number;
    avg_score: number;
    funding_round: string;
    rto_policy: string;
    profile_completeness: number;
    accelerated_vesting: boolean;
}

describe('OrganizationList', () => {
    beforeEach(() => {
        // Mock fetch calls
        global.fetch = jest.fn().mockImplementation((url) => {
            if (url === '/api/funding-round-choices/') {
                return Promise.resolve({
                    json: () => Promise.resolve({ 'S': 'Seed', 'A': 'Series A' }),
                });
            }
            if (url === '/api/rto-policy-choices/') {
                return Promise.resolve({
                    json: () => Promise.resolve({ 'R': 'Remote', 'H': 'Hybrid' }),
                });
            }
            if (url.includes('/api/organizations/')) {
                return Promise.resolve({
                    json: () => Promise.resolve({ 
                        id: 1, 
                        name: 'Organization 1', 
                        type: 'C',
                        url: 'https://org1.example.com',
                        gives_ratings: true,
                        public: true
                    }),
                });
            }
            return Promise.reject(new Error('Fetch not mocked for this URL'));
        });

        // Mock URL constructor and window methods
        const mockUrl = {
            searchParams: {
                get: jest.fn().mockImplementation((param) => {
                    if (param === 'page') return '1';
                    return null;
                }),
                set: jest.fn(),
            },
            toString: jest.fn().mockReturnValue('http://localhost/'),
        };
        
        // @ts-ignore - Mocking URL for testing
        global.URL = jest.fn(() => mockUrl);

        global.window.history.pushState = jest.fn();
    });

    afterEach(() => {
        jest.clearAllMocks();
    });

    const organizations: Organization[] = [
        {
            id: 1,
            name: 'Organization 1',
            ranking: 1,
            avg_score: 4.5,
            funding_round: 'S',
            rto_policy: 'R',
            profile_completeness: 80,
            accelerated_vesting: true,
        },
        {
            id: 2,
            name: 'Organization 2',
            ranking: 2,
            avg_score: 3.5,
            funding_round: 'A',
            rto_policy: 'H',
            profile_completeness: 70,
            accelerated_vesting: false,
        },
    ];

    test('renders organizations', async () => {
        render(<OrganizationList organizations={organizations} />);

        // Wait for the component to fetch choices
        await waitFor(() => {
            expect(screen.getByText('Organization 1')).toBeInTheDocument();
            expect(screen.getByText('Organization 2')).toBeInTheDocument();
        });
    });

    test('shows popup on organization name hover', async () => {
        render(<OrganizationList organizations={organizations} />);

        // Wait for the component to render
        await waitFor(() => {
            expect(screen.getByText('Organization 1')).toBeInTheDocument();
        });

        // Hover over the organization name
        fireEvent.mouseEnter(screen.getByText('Organization 1'));

        // Verify API call was made
        expect(global.fetch).toHaveBeenCalledWith('/api/organizations/1/');
    });

    test('filters organizations by search term', async () => {
        render(<OrganizationList organizations={organizations} />);

        // Wait for the component to fetch choices
        await waitFor(() => {
            expect(screen.getByText('Organization 1')).toBeInTheDocument();
            expect(screen.getByText('Organization 2')).toBeInTheDocument();
        });

        // Type a search term
        const searchInput = screen.getByPlaceholderText('Search organizations');
        fireEvent.change(searchInput, { target: { value: 'Organization 1' } });

        // Check that only Organization 1 is visible
        await waitFor(() => {
            expect(screen.getByText('Organization 1')).toBeInTheDocument();
            expect(screen.queryByText('Organization 2')).not.toBeInTheDocument();
        });
    });

    test('filters organizations by accelerated vesting', async () => {
        render(<OrganizationList organizations={organizations} />);

        // Wait for the component to fetch choices
        await waitFor(() => {
            expect(screen.getByText('Organization 1')).toBeInTheDocument();
            expect(screen.getByText('Organization 2')).toBeInTheDocument();
        });

        // Check the accelerated vesting checkbox
        const checkbox = screen.getByTestId('accelerated-vesting-checkbox');
        fireEvent.click(checkbox);

        // Check that only Organization 1 is visible (as it has accelerated_vesting: true)
        await waitFor(() => {
            expect(screen.getByText('Organization 1')).toBeInTheDocument();
            expect(screen.queryByText('Organization 2')).not.toBeInTheDocument();
        });
    });

    test('changes page', async () => {
        render(<OrganizationList organizations={Array(20).fill(organizations[0])} itemsPerPage={10} />);

        // Wait for the component to fetch choices
        await waitFor(() => {
            expect(screen.getByTestId('page-link-2')).toBeInTheDocument();
        });

        // Click the second page link
        fireEvent.click(screen.getByTestId('page-link-2'));

        // Check that the URL was updated
        expect(global.window.history.pushState).toHaveBeenCalled();
    });
});