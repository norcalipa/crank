// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import * as React from 'react';

import OrganizationDetailsPopup from './OrganizationDetailsPopup';

// Add an interface that matches the component's expected props
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
    type?: string;
    url?: string;
    gives_ratings?: boolean;
    public?: boolean;
    avg_scores?: ScoreDetail[];
}

describe('OrganizationDetailsPopup', () => {
    beforeEach(() => {
        // Mock fetch calls
        global.fetch = jest.fn().mockImplementation((url) => {
            if (url.includes('/api/organizations/1/scores/')) {
                return Promise.resolve({
                    json: () => Promise.resolve([
                        { type__name: 'Culture', avg_score: 4.5 },
                        { type__name: 'Leadership', avg_score: 3.8 }
                    ]),
                });
            }
            return Promise.reject(new Error('Fetch not mocked for this URL'));
        });
    });

    afterEach(() => {
        jest.clearAllMocks();
    });

    const mockOrganization: Organization = {
        id: 1,
        name: 'Test Organization',
        ranking: 5,
        avg_score: 4.2,
        funding_round: 'S',
        rto_policy: 'R',
        profile_completeness: 85,
        accelerated_vesting: true,
        type: 'C',
        url: 'https://example.com',
        gives_ratings: true,
        public: true
    };

    test('renders nothing when visible is false', () => {
        const { container } = render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={false}
                onClose={() => {}}
            />
        );
        
        expect(container.firstChild).toBeNull();
    });

    test('renders nothing when organization is null', () => {
        const { container } = render(
            <OrganizationDetailsPopup
                organization={null}
                visible={true}
                onClose={() => {}}
            />
        );
        
        expect(container.firstChild).toBeNull();
    });

    test('renders organization details when visible is true', () => {
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );
        
        expect(screen.getByText('Test Organization')).toBeInTheDocument();
        expect(screen.getByText('https://example.com')).toBeInTheDocument();
        expect(screen.getByText('Company (for profit)')).toBeInTheDocument();
        expect(screen.getByText('Seed')).toBeInTheDocument();
        expect(screen.getByText('Remote')).toBeInTheDocument();
        expect(screen.getAllByText('Yes').length).toBe(2); // For gives_ratings and accelerated_vesting
        expect(screen.getByText('5')).toBeInTheDocument(); // For ranking
        expect(screen.getByText('85%')).toBeInTheDocument(); // For profile_completeness
    });

    test('fetches and displays scores when organization has no avg_scores', async () => {
        const organizationWithoutScores: Organization = { ...mockOrganization };
        delete organizationWithoutScores.avg_scores;
        
        render(
            <OrganizationDetailsPopup
                organization={organizationWithoutScores}
                visible={true}
                onClose={() => {}}
            />
        );
        
        // First it should show loading state
        expect(screen.getByText('Loading scores...')).toBeInTheDocument();
        
        // Then it should fetch and display the scores
        await waitFor(() => {
            expect(screen.getByText('Culture')).toBeInTheDocument();
            expect(screen.getByText('4.50')).toBeInTheDocument();
            expect(screen.getByText('Leadership')).toBeInTheDocument();
            expect(screen.getByText('3.80')).toBeInTheDocument();
        });
        
        expect(global.fetch).toHaveBeenCalledWith('/api/organizations/1/scores/');
    });

    test('displays existing avg_scores without fetching when available', () => {
        const organizationWithScores: Organization = {
            ...mockOrganization,
            avg_scores: [
                { type__name: 'Total Compensation', avg_score: 4.7 },
                { type__name: 'Work-Life Balance', avg_score: 3.9 }
            ]
        };
        
        render(
            <OrganizationDetailsPopup
                organization={organizationWithScores}
                visible={true}
                onClose={() => {}}
            />
        );
        
        expect(screen.getByText('Total Compensation')).toBeInTheDocument();
        expect(screen.getByText('4.70')).toBeInTheDocument();
        expect(screen.getByText('Work-Life Balance')).toBeInTheDocument();
        expect(screen.getByText('3.90')).toBeInTheDocument();
        
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('handles API error when fetching scores', async () => {
        // Override the fetch mock to simulate an error
        global.fetch = jest.fn().mockImplementation(() => {
            return Promise.reject(new Error('API Error'));
        });
        
        const organizationWithoutScores: Organization = { ...mockOrganization };
        delete organizationWithoutScores.avg_scores;
        
        // Spy on console.error
        const consoleSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
        
        render(
            <OrganizationDetailsPopup
                organization={organizationWithoutScores}
                visible={true}
                onClose={() => {}}
            />
        );
        
        // First it should show loading state
        expect(screen.getByText('Loading scores...')).toBeInTheDocument();
        
        // Check if error was logged
        await waitFor(() => {
            expect(consoleSpy).toHaveBeenCalledWith('Error fetching organization scores:', expect.any(Error));
        });
        
        // Restore console.error
        consoleSpy.mockRestore();
    });

    test('calls onClose when close button is clicked', () => {
        const onCloseMock = jest.fn();
        
        // Create a div with tabindex to make it focusable
        const focusableDiv = document.createElement('div');
        focusableDiv.setAttribute('tabindex', '0');
        document.body.appendChild(focusableDiv);
        
        // Focus the element
        focusableDiv.focus();
        
        // Verify the element is focused
        expect(document.activeElement).toBe(focusableDiv);
        
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onCloseMock}
            />
        );
        
        const closeButton = screen.getByRole('button', { name: 'Close' });
        fireEvent.click(closeButton);
        
        // Check that onClose was called and that focus was removed
        expect(onCloseMock).toHaveBeenCalledTimes(1);
        expect(document.activeElement).not.toBe(focusableDiv);
        
        // Clean up
        document.body.removeChild(focusableDiv);
    });

    test('calls onClose when Escape key is pressed', () => {
        const onCloseMock = jest.fn();
        
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onCloseMock}
            />
        );
        
        // Simulate pressing the Escape key
        fireEvent.keyDown(document, { key: 'Escape' });
        
        expect(onCloseMock).toHaveBeenCalledTimes(1);
    });

    test('does not call onClose when other keys are pressed', () => {
        const onCloseMock = jest.fn();
        
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onCloseMock}
            />
        );
        
        // Simulate pressing other keys
        fireEvent.keyDown(document, { key: 'Enter' });
        fireEvent.keyDown(document, { key: 'a' });
        
        expect(onCloseMock).not.toHaveBeenCalled();
    });

    test('does not call onClose when Escape key is pressed but popup is not visible', () => {
        const onCloseMock = jest.fn();
        
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={false}
                onClose={onCloseMock}
            />
        );
        
        // Simulate pressing the Escape key
        fireEvent.keyDown(document, { key: 'Escape' });
        
        expect(onCloseMock).not.toHaveBeenCalled();
    });

    test('removes event listener on unmount', () => {
        const onCloseMock = jest.fn();
        const documentAddEventListenerSpy = jest.spyOn(document, 'addEventListener');
        const documentRemoveEventListenerSpy = jest.spyOn(document, 'removeEventListener');
        
        const { unmount } = render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onCloseMock}
            />
        );
        
        expect(documentAddEventListenerSpy).toHaveBeenCalledWith('keydown', expect.any(Function));
        
        unmount();
        
        expect(documentRemoveEventListenerSpy).toHaveBeenCalledWith('keydown', expect.any(Function));
        
        documentAddEventListenerSpy.mockRestore();
        documentRemoveEventListenerSpy.mockRestore();
    });

    test('renders URL as a clickable link with proper attributes', () => {
        const organizationWithUrl: Organization = {
            ...mockOrganization,
            url: 'https://testcompany.com'
        };
        
        render(
            <OrganizationDetailsPopup
                organization={organizationWithUrl}
                visible={true}
                onClose={() => {}}
            />
        );
        
        // Find the URL link
        const urlLink = screen.getByText('https://testcompany.com');
        expect(urlLink).toBeInTheDocument();
        expect(urlLink.tagName).toBe('A');
        expect(urlLink).toHaveAttribute('href', 'https://testcompany.com');
        expect(urlLink).toHaveAttribute('target', '_blank');
        expect(urlLink).toHaveAttribute('rel', 'noopener noreferrer');
    });

    test('does not render URL when not provided', () => {
        const organizationWithoutUrl: Organization = {
            ...mockOrganization
        };
        delete organizationWithoutUrl.url;
        
        render(
            <OrganizationDetailsPopup
                organization={organizationWithoutUrl}
                visible={true}
                onClose={() => {}}
            />
        );
        
        // Check that the URL label exists but no link is rendered
        expect(screen.getByText('URL:')).toBeInTheDocument();
        const urlContainer = screen.getByText('URL:').parentElement?.nextElementSibling;
        
        // Don't expect empty string - just verify no anchor element exists
        expect(urlContainer?.querySelector('a')).toBeNull();
    });

    // Note: We're not testing overlay click behavior directly due to challenges with
    // simulating the correct event bubbling and target/currentTarget relationships.
    // The component's actual behavior has been manually verified in the browser.
    // The tests below address this by directly triggering the click handlers with properly mocked events.

    test('closes popup when clicking on the overlay', () => {
        const onCloseMock = jest.fn();
        
        // Create a div with tabindex to make it focusable and add to the DOM
        const focusableDiv = document.createElement('div');
        focusableDiv.setAttribute('tabindex', '0');
        document.body.appendChild(focusableDiv);
        
        // Focus the element
        focusableDiv.focus();
        
        // Verify the element is focused
        expect(document.activeElement).toBe(focusableDiv);
        
        // Mock blur to verify it gets called
        const blurSpy = jest.spyOn(focusableDiv, 'blur');
        
        // Render component
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onCloseMock}
            />
        );
        
        // Get the overlay
        const overlay = screen.getByTestId('popup-overlay');
        
        // Simulate click on the overlay (not its children)
        // Using fireEvent directly instead of the mock approach
        fireEvent.click(overlay, {
            // Set target and currentTarget to the overlay element to simulate
            // clicking directly on the overlay
            target: overlay,
            currentTarget: overlay
        });
        
        // Check that onClose was called
        expect(onCloseMock).toHaveBeenCalledTimes(1);
        
        // Explicitly verify blur was called on the active element
        expect(blurSpy).toHaveBeenCalled();
        
        // Check that the active element was blurred
        expect(document.activeElement).not.toBe(focusableDiv);
        
        // Clean up
        document.body.removeChild(focusableDiv);
        blurSpy.mockRestore();
    });
    
    test('does not close popup when clicking on popup content', () => {
        const onCloseMock = jest.fn();
        
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onCloseMock}
            />
        );
        
        // Get the overlay and popup content
        const overlay = screen.getByTestId('popup-overlay');
        const popupContent = overlay.querySelector('.popup-details');
        
        expect(popupContent).not.toBeNull();
        
        // Simulate click on the popup content
        // Using fireEvent directly instead of the mock approach
        fireEvent.click(popupContent!, {
            // Set target to the popup content but currentTarget to the overlay
            // This simulates bubbling - click happened on content but bubbled to overlay
            target: popupContent,
            currentTarget: overlay
        });
        
        // Check that onClose was NOT called
        expect(onCloseMock).not.toHaveBeenCalled();
    });
    
    test('blurs activeElement when clicking overlay with non-HTMLElement activeElement', () => {
        const onCloseMock = jest.fn();
        
        // Save the original activeElement property
        const originalActiveElement = document.activeElement;
        
        // Mock document.activeElement to return a non-HTMLElement (like null)
        Object.defineProperty(document, 'activeElement', {
            get: jest.fn(() => null),
            configurable: true
        });
        
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onCloseMock}
            />
        );
        
        // Get the overlay
        const overlay = screen.getByTestId('popup-overlay');
        
        // Simulate click on the overlay
        fireEvent.click(overlay, {
            target: overlay,
            currentTarget: overlay
        });
        
        // Check that onClose was called
        expect(onCloseMock).toHaveBeenCalledTimes(1);
        
        // Restore the original activeElement property
        Object.defineProperty(document, 'activeElement', {
            value: originalActiveElement,
            configurable: true
        });
    });
}); 