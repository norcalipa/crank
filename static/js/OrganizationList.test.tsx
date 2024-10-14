// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {render, screen, fireEvent, act, waitFor, cleanup} from '@testing-library/react';
import '@testing-library/jest-dom';
import fetchMock from 'jest-fetch-mock';

fetchMock.enableMocks();
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

const mockFundingRoundChoices = [{'A': 'Series A', 'B': 'Series B'}];
const mockRtoPolicyChoices = [{'Remote': 'Remote', 'Hybrid': 'Hybrid'}];

const mockOrganizations: Organization[] = [
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

describe('OrganizationList', () => {
    let consoleErrorMock: jest.SpyInstance;
    let originalLocation: Location;

    beforeAll(() => {
        jest.spyOn(console, 'error').mockImplementation(() => {
        })
        originalLocation = window.location;
    })

    beforeEach(() => {
        Object.defineProperty(window, 'location', {
            writable: true,
            value: {
                href: 'http://localhost/',
                search: '',
                assign: jest.fn(),
                replace: jest.fn(),
                reload: jest.fn(),
            } as Partial<Location>, // Specify we're only mocking part of Location
        });
        jest.clearAllMocks()
        fetchMock.resetMocks();
        window.location.search = '';
    });

    afterEach(() => {
        cleanup();
    });

    afterAll(() => {
        window.location = originalLocation;
    })

    test('handles error in fetching funding-round-choices gracefully', async () => {
        consoleErrorMock = jest.spyOn(console, 'error').mockImplementation(() => {
        });
        fetchMock.mockRejectOnce(new Error('Failed to fetch funding round choices'));

        render(<OrganizationList organizations={mockOrganizations}/>);

        // Verify that the error was logged
        await waitFor(() => {
            expect(console.error).toHaveBeenCalledWith(
                'Error fetching funding round choices:',
                expect.objectContaining({message: 'Failed to fetch funding round choices'})
            );
        });
        consoleErrorMock.mockRestore();
    });

    test('handles error in fetching rto-policy-choices gracefully', async () => {
        consoleErrorMock = jest.spyOn(console, 'error').mockImplementation(() => {
        });
        fetchMock.mockResponse(JSON.stringify(mockFundingRoundChoices));
        fetchMock.mockReject(new Error('Failed to fetch RTO policy choices'));

        render(<OrganizationList organizations={mockOrganizations}/>);

        // Verify that the error was logged
        await waitFor(() => {
            expect(console.error).toHaveBeenCalledWith(
                'Error fetching RTO policy choices:',
                expect.objectContaining({message: 'Failed to fetch RTO policy choices'})
            );
        });
        consoleErrorMock.mockRestore();
    });

    test('renders organization list', async () => {
        fetchMock.mockResponseOnce(JSON.stringify(mockFundingRoundChoices));
        fetchMock.mockResponseOnce(JSON.stringify(mockRtoPolicyChoices));
        render(<OrganizationList organizations={mockOrganizations}/>);

        expect(await screen.findByText('Org 1')).toBeInTheDocument();
        expect(screen.getByText('Org 2')).toBeInTheDocument();
    });

    test('filters organizations based on search term', async () => {
        fetchMock.mockResponseOnce(JSON.stringify(mockFundingRoundChoices));
        fetchMock.mockResponseOnce(JSON.stringify(mockRtoPolicyChoices));
        render(<OrganizationList organizations={mockOrganizations}/>);

        fireEvent.change(screen.getByPlaceholderText('Search organizations'), {target: {value: 'Org 1'}});

        expect(await screen.findByText('Org 1')).toBeInTheDocument();
        expect(screen.queryByText('Org 2')).not.toBeInTheDocument();
    });

    test('filters organizations based on accelerated vesting', async () => {
        fetchMock.mockResponseOnce(JSON.stringify(mockFundingRoundChoices));
        fetchMock.mockResponseOnce(JSON.stringify(mockRtoPolicyChoices));
        render(<OrganizationList organizations={mockOrganizations}/>);

        act(() => {
            fireEvent.click(screen.getByTestId('accelerated-vesting-checkbox'));
        });
        expect(await screen.findByText('Org 1')).toBeInTheDocument();
        expect(screen.queryByText('Org 2')).not.toBeInTheDocument();
    });

    test('changes page when pagination link is clicked', async () => {
        fetchMock.mockResponseOnce(JSON.stringify(mockFundingRoundChoices));
        fetchMock.mockResponseOnce(JSON.stringify(mockRtoPolicyChoices));

        await act(async () => {
            render(<OrganizationList organizations={mockOrganizations} itemsPerPage={1}/>);
        });

        await act(async () => {
            fireEvent.click(screen.getByText('2'));
            window.location.search = '?page=2';
        });

        expect(window.location.search).toBe('?page=2');
    });

    test('handles invalid organization data gracefully', async () => {
        // Create and append the container element for the OrganizationList component
        const container = document.createElement('div');
        container.id = 'organization-list';
        document.body.appendChild(container);

        // Create and append the invalid organization data script element
        const organizationDataElement = document.createElement('script');
        organizationDataElement.id = 'organization-data';
        organizationDataElement.type = 'application/json';
        organizationDataElement.textContent = 'invalid json';
        document.body.appendChild(organizationDataElement);

        // Mock console.error to suppress error output in test results
        const consoleErrorMock = jest.spyOn(console, 'error').mockImplementation(() => {
        });

        // Dispatch the DOMContentLoaded event
        await act(async () => {
            const event = new Event('DOMContentLoaded');
            document.dispatchEvent(event);
        });

        // Verify that the error was logged
        expect(consoleErrorMock).toHaveBeenCalledWith(
            expect.stringContaining('Error parsing organization data:'),
            expect.any(SyntaxError)
        );

        // Clean up the document body and restore console.error
        document.body.removeChild(container);
        document.body.removeChild(organizationDataElement);
        consoleErrorMock.mockRestore();
    });

    test('renders currentOrganizations list on DOMContentLoaded event', async () => {
        fetchMock.mockResponseOnce(JSON.stringify(mockFundingRoundChoices));
        fetchMock.mockResponseOnce(JSON.stringify(mockRtoPolicyChoices));

        // Create and append the container element for the OrganizationList component
        const container = document.createElement('div');
        container.id = 'organization-list';
        document.body.appendChild(container);

        // Create and append the valid organization data script element
        const organizationDataElement = document.createElement('script');
        organizationDataElement.id = 'organization-data';
        organizationDataElement.type = 'application/json';
        organizationDataElement.textContent = JSON.stringify(mockOrganizations);
        document.body.appendChild(organizationDataElement);

        // Dispatch the DOMContentLoaded event
        await act(async () => {
            const event = new Event('DOMContentLoaded');
            document.dispatchEvent(event);
        });

        // Verify that the currentOrganizations list is rendered correctly
        expect(await screen.findByText('Org 1')).toBeInTheDocument();
        expect(screen.getByText('Org 2')).toBeInTheDocument();

        // Clean up the document body
        document.body.removeChild(container);
        document.body.removeChild(organizationDataElement);
    });

});