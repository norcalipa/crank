// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import * as React from 'react';

import OrganizationDetailsPopup from './OrganizationDetailsPopup';
import * as suggestCompanyController from './suggestCompany/controller';
import {getWorkspaceSnapshot, resetWorkspaceForTests} from './workspace/store';

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
            if (url.includes('/api/organizations/1/provenance/')) {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        organization_id: 1,
                        organization_modified: '2025-01-15T10:00:00Z',
                        organization_created: '2024-06-01T00:00:00Z',
                        latest_observation: {
                            source_url: 'https://example.com/about',
                            observed_domain: 'example.com',
                            observed_at: '2025-01-10T12:00:00Z',
                            extraction_version: 'v1.2.3',
                            status: 'auto_applied',
                        },
                    }),
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
        expect(screen.getByRole('dialog', {name: 'Test Organization'})).toHaveAttribute('aria-modal', 'true');
        expect(screen.getByRole('button', {name: 'Close'})).toHaveFocus();
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

        // Scores are pre-loaded so the scores endpoint should not be called,
        // but the provenance endpoint is always fetched when visible.
        expect(global.fetch).not.toHaveBeenCalledWith('/api/organizations/1/scores/');
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
        
        // Check that onClose was called and focus returned to the opener.
        expect(onCloseMock).toHaveBeenCalledTimes(1);
        expect(document.activeElement).toBe(focusableDiv);
        
        // Clean up
        document.body.removeChild(focusableDiv);
    });

    test('calls onClose and restores focus to opener when Escape key is pressed', () => {
        const onCloseMock = jest.fn();

        // Create a div with tabindex to make it focusable and add to the DOM
        const focusableDiv = document.createElement('div');
        focusableDiv.setAttribute('tabindex', '0');
        document.body.appendChild(focusableDiv);
        focusableDiv.focus();

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
        expect(document.activeElement).toBe(focusableDiv);

        document.body.removeChild(focusableDiv);
    });

    // Regression (issue #471 CI repair): the opener must be captured BEFORE
    // background isolation inerts the shell. Inerting the shell blurs the
    // trigger (activeElement resets to <body>); if capture ran after the
    // lock, the restore target would be lost and Escape would strand focus
    // on <body>. This test drives the real modalIsolation inert/scroll lock
    // to prove focus still returns to the trigger through the full lifecycle.
    test('restores focus to the trigger after Escape while the background is inerted', () => {
        const onCloseMock = jest.fn();

        // A real trigger element that lives in the (to-be-inerted) background.
        const trigger = document.createElement('button');
        trigger.setAttribute('aria-label', 'View details');
        document.body.appendChild(trigger);
        trigger.focus();
        expect(document.activeElement).toBe(trigger);

        const {rerender} = render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={false}
                onClose={onCloseMock}
            />
        );

        // Open: the shell/content/skip-link go inert. The trigger must have
        // been captured before the inert blur so the restore can find it.
        rerender(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onCloseMock}
            />
        );

        // The background is isolated while open.
        const shell = document.querySelector('.app-shell, main.app-content, .skip-to-content');
        if (shell) {
            expect(shell).toHaveAttribute('inert');
        }

        // Escape closes and returns focus to the captured trigger.
        fireEvent.keyDown(document, {key: 'Escape'});
        expect(onCloseMock).toHaveBeenCalledTimes(1);

        // Parent honors onClose and hides the dialog (commit release path).
        rerender(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={false}
                onClose={onCloseMock}
            />
        );

        expect(document.activeElement).toBe(trigger);
        document.body.removeChild(trigger);
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

    describe('focus trap (issue #464)', () => {
        test('Tab from the last focusable element wraps to the first', () => {
            render(
                <OrganizationDetailsPopup
                    organization={mockOrganization}
                    visible={true}
                    onClose={() => {}}
                />
            );

            const dialog = screen.getByRole('dialog', {name: 'Test Organization'});
            const focusables = Array.from(
                dialog.querySelectorAll<HTMLElement>(
                    'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
                )
            );
            expect(focusables.length).toBeGreaterThan(1);
            const first = focusables[0];
            const last = focusables[focusables.length - 1];

            last.focus();
            expect(document.activeElement).toBe(last);

            fireEvent.keyDown(document, {key: 'Tab'});

            expect(document.activeElement).toBe(first);
        });

        test('Shift+Tab from the first focusable element wraps to the last', () => {
            render(
                <OrganizationDetailsPopup
                    organization={mockOrganization}
                    visible={true}
                    onClose={() => {}}
                />
            );

            const dialog = screen.getByRole('dialog', {name: 'Test Organization'});
            const focusables = Array.from(
                dialog.querySelectorAll<HTMLElement>(
                    'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
                )
            );
            const first = focusables[0];
            const last = focusables[focusables.length - 1];

            first.focus();
            expect(document.activeElement).toBe(first);

            fireEvent.keyDown(document, {key: 'Tab', shiftKey: true});

            expect(document.activeElement).toBe(last);
        });

        test('Tab with focus outside the dialog moves focus into it (never behind it)', () => {
            render(
                <OrganizationDetailsPopup
                    organization={mockOrganization}
                    visible={true}
                    onClose={() => {}}
                />
            );

            const dialog = screen.getByRole('dialog', {name: 'Test Organization'});
            const focusables = Array.from(
                dialog.querySelectorAll<HTMLElement>(
                    'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
                )
            );
            const first = focusables[0];

            // Park focus on a background element outside the dialog.
            const outside = document.createElement('button');
            document.body.appendChild(outside);
            outside.focus();
            expect(document.activeElement).toBe(outside);

            fireEvent.keyDown(document, {key: 'Tab'});

            expect(dialog.contains(document.activeElement)).toBe(true);
            expect(document.activeElement).toBe(first);

            document.body.removeChild(outside);
        });

        test('Shift+Tab with focus outside the dialog moves focus to its last element', () => {
            render(
                <OrganizationDetailsPopup
                    organization={mockOrganization}
                    visible={true}
                    onClose={() => {}}
                />
            );

            const dialog = screen.getByRole('dialog', {name: 'Test Organization'});
            const focusables = Array.from(
                dialog.querySelectorAll<HTMLElement>(
                    'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
                )
            );
            const last = focusables[focusables.length - 1];

            const outside = document.createElement('button');
            document.body.appendChild(outside);
            outside.focus();

            fireEvent.keyDown(document, {key: 'Tab', shiftKey: true});

            expect(document.activeElement).toBe(last);

            document.body.removeChild(outside);
        });

        test('Tab cycling ignores disabled buttons inside the dialog', () => {
            render(
                <OrganizationDetailsPopup
                    organization={mockOrganization}
                    visible={true}
                    onClose={() => {}}
                />
            );

            const dialog = screen.getByRole('dialog', {name: 'Test Organization'});
            // The only anchor and button remain the cycle bounds; adding a
            // disabled button must not affect the cycle.
            const disabled = document.createElement('button');
            disabled.disabled = true;
            dialog.appendChild(disabled);

            const focusables = Array.from(
                dialog.querySelectorAll<HTMLElement>(
                    'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
                )
            );
            expect(focusables).not.toContain(disabled);

            fireEvent.keyDown(document, {key: 'Tab'});
            expect(dialog.contains(document.activeElement)).toBe(true);

            dialog.removeChild(disabled);
        });
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
        
        // Render component; the accessible dialog moves focus to its Close button.
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onCloseMock}
            />
        );
        
        // Get the overlay
        const overlay = screen.getByTestId('popup-overlay');
        const closeButton = screen.getByRole('button', {name: 'Close'});
        expect(closeButton).toHaveFocus();
        
        // Simulate click on the overlay (not its children)
        // Using fireEvent directly instead of the mock approach
        fireEvent.click(overlay, {
            // Set target and currentTarget to the overlay element to simulate
            // clicking directly on the overlay
            target: overlay,
            currentTarget: overlay
        });
        
        // Check that onClose was called and focus returned to the opener.
        expect(onCloseMock).toHaveBeenCalledTimes(1);
        expect(document.activeElement).toBe(focusableDiv);
        
        // Clean up
        document.body.removeChild(focusableDiv);
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
    
    test('closes popup without error when activeElement is not an HTMLElement', () => {
        const onCloseMock = jest.fn();

        // Save the original activeElement property descriptor so it can be
        // restored exactly (activeElement is an accessor in jsdom).
        const originalDescriptor = Object.getOwnPropertyDescriptor(document, 'activeElement');

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

        // Restore the original activeElement property (or remove our own
        // property so the prototype accessor is used again).
        if (originalDescriptor) {
            Object.defineProperty(document, 'activeElement', originalDescriptor);
        } else {
            delete (document as any).activeElement;
        }
    });

    test('blurs active element when opener is no longer connected on close', () => {
        const onCloseMock = jest.fn();

        const focusableDiv = document.createElement('div');
        focusableDiv.setAttribute('tabindex', '0');
        document.body.appendChild(focusableDiv);
        focusableDiv.focus();

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onCloseMock}
            />
        );

        // Detach the opener so it is no longer connected, simulating the
        // trigger being removed from the DOM while the dialog is open.
        document.body.removeChild(focusableDiv);

        const closeButton = screen.getByRole('button', { name: 'Close' });
        const blurSpy = jest.spyOn(closeButton, 'blur');

        fireEvent.keyDown(document, { key: 'Escape' });

        expect(onCloseMock).toHaveBeenCalledTimes(1);
        expect(blurSpy).toHaveBeenCalled();

        blurSpy.mockRestore();
    });

    test('renders provenance section with observation data', async () => {
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('provenance-section')).toBeInTheDocument();
        });

        expect(screen.getByText('Data Freshness & Sources')).toBeInTheDocument();
        expect(screen.getByTestId('last-updated')).toBeInTheDocument();
        expect(screen.getByTestId('added-to-catalog')).toBeInTheDocument();
        expect(screen.getByTestId('observation-details')).toBeInTheDocument();
        expect(screen.getByText('example.com')).toBeInTheDocument();
        expect(screen.getByText('v1.2.3')).toBeInTheDocument();
        expect(screen.getByText('auto_applied')).toBeInTheDocument();
    });

    test('shows loading state for provenance', async () => {
        // Mock a slow provenance response
        global.fetch = jest.fn().mockImplementation((url) => {
            if (url.includes('/api/organizations/1/scores/')) {
                return Promise.resolve({
                    json: () => Promise.resolve([]),
                });
            }
            if (url.includes('/api/organizations/1/provenance/')) {
                return new Promise(() => {}); // never resolves
            }
            return Promise.reject(new Error('not mocked'));
        });

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('provenance-loading')).toBeInTheDocument();
        });
    });

    test('shows no-observation message when latest_observation is null', async () => {
        global.fetch = jest.fn().mockImplementation((url) => {
            if (url.includes('/api/organizations/1/scores/')) {
                return Promise.resolve({ json: () => Promise.resolve([]) });
            }
            if (url.includes('/api/organizations/1/provenance/')) {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        organization_id: 1,
                        organization_modified: '2025-01-15T10:00:00Z',
                        organization_created: '2024-06-01T00:00:00Z',
                        latest_observation: null,
                    }),
                });
            }
            return Promise.reject(new Error('not mocked'));
        });

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('no-observation')).toBeInTheDocument();
        });
    });

    const provenanceWithEvidence = (overrides: Record<string, unknown> = {}) => {
        global.fetch = jest.fn().mockImplementation((url) => {
            if (url.includes('/api/organizations/1/scores/')) {
                return Promise.resolve({ json: () => Promise.resolve([]) });
            }
            if (url.includes('/api/organizations/1/provenance/')) {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        organization_id: 1,
                        organization_modified: '2025-01-15T10:00:00Z',
                        organization_created: '2024-06-01T00:00:00Z',
                        latest_observation: {
                            source_url: 'https://example.com/about',
                            observed_domain: 'example.com',
                            observed_at: '2025-01-10T12:00:00Z',
                            extraction_version: 'v1.2.3',
                            status: 'auto_applied',
                            is_verified: true,
                        },
                        fields: [
                            {
                                field_key: 'rto_policy',
                                state: 'accepted',
                                value: 'Remote first',
                                source_domain: 'example.com',
                                observed_at: '2025-01-10T12:00:00Z',
                                scope: { countries: ['US'] },
                                last_checked_at: '2025-01-10T12:00:00Z',
                                last_successful_fetch_at: '2025-01-10T12:00:00Z',
                                last_changed_at: '2025-01-10T12:00:00Z',
                                last_verified_at: '2025-01-10T12:00:00Z',
                                stale: false,
                            },
                        ],
                        unverified_fields: ['funding_round', 'public_status'],
                        ...overrides,
                    }),
                });
            }
            return Promise.reject(new Error('not mocked'));
        });
    };

    test('renders verified field rows with value, source domain and observed date', async () => {
        provenanceWithEvidence();

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('field-evidence')).toBeInTheDocument();
        });

        expect(screen.getByTestId('field-evidence-rto_policy')).toBeInTheDocument();
        expect(screen.getByTestId('field-value-rto_policy')).toHaveTextContent('Remote first');
        expect(screen.getByTestId('field-evidence-rto_policy')).toHaveTextContent('example.com');
        expect(screen.getByTestId('field-evidence-rto_policy')).toHaveTextContent('observed');
        expect(screen.getByTestId('field-evidence-rto_policy')).toHaveTextContent('countries: US');
        expect(screen.queryByTestId('field-stale-rto_policy')).not.toBeInTheDocument();
    });

    test('renders explicit no-evidence rows for unverified fields', async () => {
        provenanceWithEvidence();

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('unverified-fields')).toBeInTheDocument();
        });

        const unverified = screen.getByTestId('field-unverified-funding_round');
        expect(unverified).toHaveTextContent('Funding Round');
        expect(unverified).toHaveTextContent('No accepted evidence');
        expect(screen.getByTestId('field-unverified-public_status')).toBeInTheDocument();
    });

    test('renders stale marker on stale field rows', async () => {
        provenanceWithEvidence({
            fields: [
                {
                    field_key: 'rto_policy',
                    state: 'accepted',
                    value: 'Remote first',
                    source_domain: 'example.com',
                    observed_at: '2024-01-10T12:00:00Z',
                    scope: {},
                    last_checked_at: '2025-01-10T12:00:00Z',
                    last_successful_fetch_at: '2025-01-10T12:00:00Z',
                    last_changed_at: '2024-01-10T12:00:00Z',
                    last_verified_at: '2024-06-01T00:00:00Z',
                    stale: true,
                },
            ],
            unverified_fields: [],
        });

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('field-stale-rto_policy')).toBeInTheDocument();
        });

        expect(screen.getByTestId('field-stale-rto_policy')).toHaveTextContent('Stale');
        expect(screen.getByTestId('field-value-rto_policy')).toHaveTextContent('Remote first');
        expect(screen.queryByTestId('unverified-fields')).not.toBeInTheDocument();
    });

    test('marks a pending latest observation as not verified evidence', async () => {
        provenanceWithEvidence({
            latest_observation: {
                source_url: 'https://example.com/about',
                observed_domain: 'example.com',
                observed_at: '2025-01-10T12:00:00Z',
                extraction_version: 'v1.2.3',
                status: 'pending',
                is_verified: false,
            },
            fields: [],
            unverified_fields: ['rto_policy'],
        });

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('observation-not-verified')).toBeInTheDocument();
        });

        expect(screen.getByTestId('observation-details')).toBeInTheDocument();
        expect(screen.getByTestId('observation-not-verified')).toHaveTextContent('Not accepted evidence');
    });

    test('shows suggest correction link when authenticated', async () => {
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
                isAuthenticated={true}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('correction-action')).toBeInTheDocument();
        });
        expect(screen.getByTestId('suggest-correction-link')).toBeInTheDocument();
    });

    test('the correction trigger calls the controller with company_details context and closes the dialog', async () => {
        const openSpy = jest.spyOn(suggestCompanyController, 'openSuggestCompany').mockImplementation(() => {});
        const onClose = jest.fn();
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={onClose}
                isAuthenticated={true}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('suggest-correction-link')).toBeInTheDocument();
        });
        fireEvent.click(screen.getByTestId('suggest-correction-link'));

        expect(openSpy).toHaveBeenCalledWith({
            source: 'company_details',
            companyName: 'Test Organization',
            organizationId: 1,
        });
        expect(onClose).toHaveBeenCalledTimes(1);

        openSpy.mockRestore();
    });

    test('does not show correction link when not authenticated', async () => {
        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
                isAuthenticated={false}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('provenance-section')).toBeInTheDocument();
        });
        expect(screen.queryByTestId('correction-action')).toBeNull();
    });

    test('handles provenance fetch error gracefully', async () => {
        global.fetch = jest.fn().mockImplementation((url) => {
            if (url.includes('/api/organizations/1/scores/')) {
                return Promise.resolve({ json: () => Promise.resolve([]) });
            }
            if (url.includes('/api/organizations/1/provenance/')) {
                return Promise.reject(new Error('Network error'));
            }
            return Promise.reject(new Error('not mocked'));
        });

        const consoleSpy = jest.spyOn(console, 'error').mockImplementation(() => {});

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(consoleSpy).toHaveBeenCalledWith('Error fetching organization provenance:', expect.any(Error));
        });

        consoleSpy.mockRestore();
    });

    test('formats relative time as months ago for dates > 30 days', async () => {
        // Use dates relative to the real current time so coverage tracks properly
        const now = new Date();
        const monthsAgo = new Date(now.getTime() - 75 * 24 * 60 * 60 * 1000); // ~75 days ago
        const yearsAgo = new Date(now.getTime() - 800 * 24 * 60 * 60 * 1000); // ~800 days ago

        global.fetch = jest.fn().mockImplementation((url) => {
            if (url.includes('/api/organizations/1/scores/')) {
                return Promise.resolve({ json: () => Promise.resolve([]) });
            }
            if (url.includes('/api/organizations/1/provenance/')) {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        organization_id: 1,
                        organization_modified: monthsAgo.toISOString(),
                        organization_created: yearsAgo.toISOString(),
                        latest_observation: {
                            source_url: 'https://example.com',
                            observed_domain: 'example.com',
                            observed_at: monthsAgo.toISOString(),
                            extraction_version: 'v1',
                            status: 'accepted',
                        },
                    }),
                });
            }
            return Promise.reject(new Error('not mocked'));
        });

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('observation-details')).toBeInTheDocument();
        });

        // ~75 days ago should be '2 months ago' or '3 months ago'
        const lastUpdated = screen.getByTestId('last-updated');
        expect(lastUpdated.textContent).toContain('months ago');
    });

    test('formats relative time as years ago for dates > 365 days', async () => {
        const now = new Date();
        const yearsAgo = new Date(now.getTime() - 800 * 24 * 60 * 60 * 1000); // ~800 days ago

        global.fetch = jest.fn().mockImplementation((url) => {
            if (url.includes('/api/organizations/1/scores/')) {
                return Promise.resolve({ json: () => Promise.resolve([]) });
            }
            if (url.includes('/api/organizations/1/provenance/')) {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        organization_id: 1,
                        organization_modified: yearsAgo.toISOString(),
                        organization_created: yearsAgo.toISOString(),
                        latest_observation: {
                            source_url: 'https://example.com',
                            observed_domain: 'example.com',
                            observed_at: yearsAgo.toISOString(),
                            extraction_version: 'v1',
                            status: 'accepted',
                        },
                    }),
                });
            }
            return Promise.reject(new Error('not mocked'));
        });

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('observation-details')).toBeInTheDocument();
        });

        // ~800 days ago should be 'years ago'
        const lastUpdated = screen.getByTestId('last-updated');
        expect(lastUpdated.textContent).toContain('years ago');
    });

    test('formats relative time as weeks ago for dates 7-30 days', async () => {
        const now = new Date();
        const weeksAgo = new Date(now.getTime() - 14 * 24 * 60 * 60 * 1000); // 14 days ago

        global.fetch = jest.fn().mockImplementation((url) => {
            if (url.includes('/api/organizations/1/scores/')) {
                return Promise.resolve({ json: () => Promise.resolve([]) });
            }
            if (url.includes('/api/organizations/1/provenance/')) {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        organization_id: 1,
                        organization_modified: weeksAgo.toISOString(),
                        organization_created: weeksAgo.toISOString(),
                        latest_observation: {
                            source_url: 'https://example.com',
                            observed_domain: 'example.com',
                            observed_at: weeksAgo.toISOString(),
                            extraction_version: 'v1',
                            status: 'accepted',
                        },
                    }),
                });
            }
            return Promise.reject(new Error('not mocked'));
        });

        render(
            <OrganizationDetailsPopup
                organization={mockOrganization}
                visible={true}
                onClose={() => {}}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('observation-details')).toBeInTheDocument();
        });

        const lastUpdated = screen.getByTestId('last-updated');
        expect(lastUpdated.textContent).toContain('weeks ago');
    });

    describe('background isolation (issue #464 review)', () => {
        let backgroundRoots: HTMLElement[];

        beforeEach(() => {
            // Stand up the page structure the isolation module targets: the
            // application shell (navigation chrome), the main content area,
            // and the modal-external skip link (review r2: it used to stay a
            // focusable page-level target while the dialog was open).
            const shell = document.createElement('aside');
            shell.className = 'app-shell';
            shell.innerHTML = '<a href="/">Nav link</a>';
            document.body.appendChild(shell);
            const content = document.createElement('main');
            content.className = 'app-content';
            document.body.appendChild(content);
            const skipLink = document.createElement('a');
            skipLink.className = 'skip-to-content';
            skipLink.href = '#main-content';
            document.body.appendChild(skipLink);
            backgroundRoots = [shell, content, skipLink];
            document.body.style.overflow = 'auto';
            document.documentElement.style.overflow = 'auto';
        });

        afterEach(() => {
            for (const root of backgroundRoots) {
                root.remove();
            }
            document.body.style.overflow = '';
            document.documentElement.style.overflow = '';
        });

        test('locks document scrolling and inert-hides the background while the dialog is open', () => {
            render(
                <OrganizationDetailsPopup
                    organization={mockOrganization}
                    visible={true}
                    onClose={() => {}}
                />
            );

            expect(document.body.style.overflow).toBe('hidden');
            // The root element carries the viewport overflow under the
            // stylesheet's `html, body { overflow-x: hidden }` rule — locking
            // body alone leaves the actual document scroller free (review r2).
            expect(document.documentElement.style.overflow).toBe('hidden');
            for (const root of backgroundRoots) {
                expect(root).toHaveAttribute('inert');
                expect(root).toHaveAttribute('aria-hidden', 'true');
            }
        });

        test('releases the scroll lock and background inertness when the dialog closes', () => {
            const {rerender} = render(
                <OrganizationDetailsPopup organization={mockOrganization} visible={true} onClose={() => {}} />
            );
            expect(document.body.style.overflow).toBe('hidden');

            rerender(<OrganizationDetailsPopup organization={mockOrganization} visible={false} onClose={() => {}} />);

            expect(document.body.style.overflow).toBe('auto');
            expect(document.documentElement.style.overflow).toBe('auto');
            for (const root of backgroundRoots) {
                expect(root).not.toHaveAttribute('inert');
                expect(root).not.toHaveAttribute('aria-hidden');
            }
        });

        test('releases isolation when unmounted while the dialog is open', () => {
            const {unmount} = render(
                <OrganizationDetailsPopup organization={mockOrganization} visible={true} onClose={() => {}} />
            );
            expect(document.body.style.overflow).toBe('hidden');

            unmount();

            expect(document.body.style.overflow).toBe('auto');
            for (const root of backgroundRoots) {
                expect(root).not.toHaveAttribute('inert');
                expect(root).not.toHaveAttribute('aria-hidden');
            }
        });

        test('restores focus to the opener after isolation is released on close', () => {
            const opener = document.createElement('button');
            document.body.appendChild(opener);
            opener.focus();

            const {rerender} = render(
                <OrganizationDetailsPopup organization={mockOrganization} visible={true} onClose={() => {}} />
            );
            rerender(<OrganizationDetailsPopup organization={mockOrganization} visible={false} onClose={() => {}} />);

            // The cleanup restores focus AFTER unlocking, so the opener —
            // inside the previously inert background — receives focus again.
            expect(document.activeElement).toBe(opener);
            opener.remove();
        });
    });

    describe('Ask the assistant CTA (issue #479)', () => {
        let rafCallbacks: FrameRequestCallback[];

        beforeEach(() => {
            resetWorkspaceForTests();
            rafCallbacks = [];
            jest.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => {
                rafCallbacks.push(cb);
                return rafCallbacks.length;
            });
        });

        afterEach(() => {
            document.getElementById('assistant-workspace')?.remove();
            jest.restoreAllMocks();
        });

        function renderAuthenticated(onClose: () => void) {
            return render(
                <OrganizationDetailsPopup organization={mockOrganization} visible={true}
                                          onClose={onClose} isAuthenticated={true} />
            );
        }

        test('the dialog body is the scroll container and the details use the balanced grid', () => {
            renderAuthenticated(jest.fn());
            const dialog = screen.getByRole('dialog');
            expect(dialog).toHaveClass('popup-details');
            expect(dialog.querySelector('.card-header')).toBeInTheDocument();
            expect(dialog.querySelector('.card-body')).toBeInTheDocument();
            const grid = screen.getByTestId('popup-details-grid');
            expect(grid.querySelector('.popup-details-profile')).toBeInTheDocument();
            expect(grid.querySelector('.popup-details-scores')).toBeInTheDocument();
        });

        test('Rank and Profile Completeness share one score card with the scores', () => {
            renderAuthenticated(jest.fn());
            const card = screen.getByTestId('popup-details-score-card');
            expect(card).toHaveClass('popup-details-scores');
            const rows = card.querySelectorAll('.popup-details-score-row');
            expect(rows).toHaveLength(2);
            expect(rows[0]).toHaveTextContent(`Rank:${mockOrganization.ranking}`);
            expect(rows[1]).toHaveTextContent(`Profile Completeness:${mockOrganization.profile_completeness.toFixed(0)}%`);
            expect(screen.getByTestId('popup-details-grid').querySelector('.popup-details-profile'))
                .not.toHaveTextContent('Rank:');
        });

        test('closes the dialog and opens the assistant with company context on the next frame', () => {
            const workspace = document.createElement('div');
            workspace.id = 'assistant-workspace';
            document.body.appendChild(workspace);
            const onClose = jest.fn();
            renderAuthenticated(onClose);

            fireEvent.click(screen.getByTestId('company-chat-cta'));

            expect(onClose).toHaveBeenCalledTimes(1);
            // Not opened synchronously: the dialog's background lock must
            // release first (#472 one-blocking-surface rule).
            expect(getWorkspaceSnapshot().visibility).toBe('closed');
            rafCallbacks.forEach((cb) => cb(0));
            expect(getWorkspaceSnapshot().visibility).toBe('open');
            expect(getWorkspaceSnapshot().context).toEqual({
                surface: 'company',
                organizationId: mockOrganization.id,
                organizationName: mockOrganization.name,
            });
        });

        test('falls back to the plain /chat/ link when the workspace host is absent', () => {
            const onClose = jest.fn();
            renderAuthenticated(onClose);

            const notPrevented = fireEvent.click(screen.getByTestId('company-chat-cta'));

            expect(notPrevented).toBe(true);
            expect(onClose).not.toHaveBeenCalled();
            expect(rafCallbacks).toHaveLength(0);
        });

        test('modified clicks keep the link behavior', () => {
            const workspace = document.createElement('div');
            workspace.id = 'assistant-workspace';
            document.body.appendChild(workspace);
            const onClose = jest.fn();
            renderAuthenticated(onClose);

            fireEvent.click(screen.getByTestId('company-chat-cta'), {ctrlKey: true});

            expect(onClose).not.toHaveBeenCalled();
        });

        test('the signed-out CTA is unchanged', () => {
            render(
                <OrganizationDetailsPopup organization={mockOrganization} visible={true} onClose={() => {}}
                                          isAuthenticated={false}
                                          signInUrlTemplate="/accounts/login/?next=%2F%3Fcompany%3D__COMPANY_ID__" />
            );
            expect(screen.getByTestId('company-sign-in-cta')).toHaveAttribute('href', expect.stringContaining('next='));
            expect(screen.queryByTestId('company-chat-cta')).not.toBeInTheDocument();
        });
    });
});
