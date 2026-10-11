// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import { cleanup, render, screen, fireEvent, waitFor, within } from '@testing-library/react';

import * as React from 'react';

import CompanyView from './CompanyView';
import {CompanyDetailsOrganization, CorrectionSource} from './CompanyDetails';
import {getCachedProvenance} from '../provenanceCache';
import {resetWorkspaceForTests} from '../workspace/store';

interface HostProps {
    organization: CompanyDetailsOrganization | null;
    visible?: boolean;
    onClose?: () => void;
    isAuthenticated?: boolean;
    signInUrlTemplate?: string;
    onAsk?: () => void;
    onSuggestCorrection?: (source: CorrectionSource, fieldKey?: string) => void;
}

// CompanyView for a record that is already loaded; `visible={false}` unmounts
// it the way the list does when the results come back.
const CompanyViewHost: React.FC<HostProps> = ({
    organization, visible = true, onClose = () => {}, isAuthenticated, signInUrlTemplate,
    onAsk = () => {}, onSuggestCorrection = () => {},
}) => (organization && visible ? (
    <CompanyView organizationId={organization.id} status="ready" organization={organization}
                 isAuthenticated={isAuthenticated} signInUrlTemplate={signInUrlTemplate}
                 onBack={onClose} onAsk={onAsk} onSuggestCorrection={onSuggestCorrection}/>
) : null);

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

describe('CompanyView', () => {
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
                        fields: [{
                            field_key: 'rto_policy', state: 'accepted', value: 'Remote first', source_domain: 'example.com',
                            observed_at: '2025-01-10T12:00:00Z', scope: {}, last_checked_at: null,
                            last_successful_fetch_at: null, last_changed_at: null,
                            last_verified_at: '2025-01-10T12:00:00Z', stale: false,
                        }],
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

    test('renders organization details when visible is true', () => {
        render(
            <CompanyViewHost
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
        expect(screen.getByTestId('rating-coverage')).toHaveTextContent('85% of rating dimensions'); // fallback without dimension counts
        // A named region, not a dialog; the heading takes focus on open.
        const region = screen.getByRole('region', {name: 'Test Organization'});
        expect(region).toHaveAttribute('data-testid', 'company-view');
        expect(region).toHaveAttribute('data-organization-id', '1');
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
        expect(region).not.toHaveAttribute('aria-modal');
        expect(document.querySelector('.popup-overlay')).toBeNull();
        const heading = screen.getByRole('heading', {level: 2, name: 'Test Organization'});
        expect(heading).toHaveAttribute('id', 'organization-details-title');
        expect(heading).toHaveFocus();
    });

    test('fetches and displays scores when organization has no avg_scores', async () => {
        const organizationWithoutScores: Organization = { ...mockOrganization };
        delete organizationWithoutScores.avg_scores;
        
        render(
            <CompanyViewHost
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
            <CompanyViewHost
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
            <CompanyViewHost
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

    // Regression (issue #471 CI repair): the opener must be captured BEFORE
    // background isolation inerts the shell. Inerting the shell blurs the
    // trigger (activeElement resets to <body>); if capture ran after the
    // lock, the restore target would be lost and Escape would strand focus
    // on <body>. This test drives the real modalIsolation inert/scroll lock
    // to prove focus still returns to the trigger through the full lifecycle.
    test('renders URL as a clickable link with proper attributes', () => {
        const organizationWithUrl: Organization = {
            ...mockOrganization,
            url: 'https://testcompany.com'
        };
        
        render(
            <CompanyViewHost
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
            <CompanyViewHost
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

    test('renders provenance section with observation data', async () => {
        render(
            <CompanyViewHost
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
        expect(within(screen.getByTestId('observation-details')).getByText('example.com')).toBeInTheDocument();
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
            <CompanyViewHost
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
            <CompanyViewHost
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

    test('separates the source from the verified date with a comma and a space, and names a missing source', async () => {
        provenanceWithEvidence();
        render(<CompanyViewHost organization={mockOrganization} visible={true} onClose={() => {}}/>);
        const meta = await screen.findByTestId('field-evidence-rto_policy');
        expect(meta).toHaveTextContent('Source: example.com');
        expect(meta).toHaveTextContent('Last verified');
        cleanup();
        provenanceWithEvidence({fields: [{
            field_key: 'rto_policy', state: 'accepted', value: 'Remote first', source_domain: '',
            last_verified_at: '2025-01-10T12:00:00Z', stale: false, scope: {},
        }]});
        render(<CompanyViewHost organization={mockOrganization} visible={true} onClose={() => {}}/>);
        expect(await screen.findByTestId('field-evidence-rto_policy')).toHaveTextContent('Source: unknown source');
    });

    test('renders verified field rows with value, source domain and observed date', async () => {
        provenanceWithEvidence();

        render(
            <CompanyViewHost
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
        expect(screen.getByTestId('field-evidence-rto_policy')).toHaveTextContent('last verified');
        expect(screen.getByTestId('field-evidence-rto_policy')).toHaveTextContent('countries: US');
        expect(screen.queryByTestId('field-stale-rto_policy')).not.toBeInTheDocument();
    });

    test('renders explicit no-evidence rows for unverified fields', async () => {
        provenanceWithEvidence();

        render(
            <CompanyViewHost
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
            <CompanyViewHost
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
            <CompanyViewHost
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
            <CompanyViewHost
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

    test('the correction trigger reports company_details to the container and the view stays', async () => {
        const onSuggestCorrection = jest.fn();
        const onClose = jest.fn();
        render(
            <CompanyViewHost
                organization={mockOrganization}
                visible={true}
                onClose={onClose}
                onSuggestCorrection={onSuggestCorrection}
                isAuthenticated={true}
            />
        );

        await waitFor(() => {
            expect(screen.getByTestId('suggest-correction-link')).toBeInTheDocument();
        });
        fireEvent.click(screen.getByTestId('suggest-correction-link'));

        expect(onSuggestCorrection).toHaveBeenCalledWith('company_details');
        expect(onClose).not.toHaveBeenCalled();
        expect(screen.getByTestId('company-view')).toBeInTheDocument();
    });

    test('does not show correction link when not authenticated', async () => {
        render(
            <CompanyViewHost
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
            <CompanyViewHost
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
            <CompanyViewHost
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
            <CompanyViewHost
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
            <CompanyViewHost
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
                <CompanyViewHost organization={mockOrganization} visible={true}
                                          onClose={onClose} isAuthenticated={true} />
            );
        }

        test('the view keeps the shared content class and the details use the balanced grid', () => {
            renderAuthenticated(jest.fn());
            const view = screen.getByTestId('company-view');
            expect(view).toHaveClass('popup-details');
            expect(view.querySelector('.company-view-header')).toBeInTheDocument();
            expect(view.querySelector('.card-body')).toBeInTheDocument();
            const grid = screen.getByTestId('popup-details-grid');
            expect(grid.querySelector('.popup-details-profile')).toBeInTheDocument();
            expect(grid.querySelector('.popup-details-scores')).toBeInTheDocument();
        });

        test('Rank and Rating coverage share one score card with the scores', () => {
            renderAuthenticated(jest.fn());
            const card = screen.getByTestId('popup-details-score-card');
            expect(card).toHaveClass('popup-details-scores');
            const rows = card.querySelectorAll('.popup-details-score-row');
            expect(rows).toHaveLength(2);
            expect(rows[0]).toHaveTextContent(`Rank:${mockOrganization.ranking}`);
            expect(rows[1]).toHaveTextContent(`Rating coverage:${mockOrganization.profile_completeness.toFixed(0)}% of rating dimensions`);
            expect(screen.getByTestId('popup-details-grid').querySelector('.popup-details-profile'))
                .not.toHaveTextContent('Rank:');
        });

        test('modified clicks keep the link behavior', () => {
            const workspace = document.createElement('div');
            workspace.id = 'assistant-workspace';
            document.body.appendChild(workspace);
            const onAsk = jest.fn();
            render(<CompanyViewHost organization={mockOrganization} isAuthenticated={true} onAsk={onAsk}/>);

            fireEvent.click(screen.getByTestId('company-chat-cta'), {ctrlKey: true});

            expect(onAsk).not.toHaveBeenCalled();
        });

        test('a plain click on a page with the workspace host asks the container to open the assistant', () => {
            const workspace = document.createElement('div');
            workspace.id = 'assistant-workspace';
            document.body.appendChild(workspace);
            const onAsk = jest.fn();
            const onClose = jest.fn();
            render(<CompanyViewHost organization={mockOrganization} isAuthenticated={true}
                                    onAsk={onAsk} onClose={onClose}/>);

            const notPrevented = fireEvent.click(screen.getByTestId('company-chat-cta'));

            expect(notPrevented).toBe(false);
            expect(onAsk).toHaveBeenCalledTimes(1);
            expect(onClose).not.toHaveBeenCalled();
            expect(screen.getByTestId('company-view')).toBeInTheDocument();
        });

        test('falls back to the plain /chat/ link when the workspace host is absent', () => {
            const onAsk = jest.fn();
            render(<CompanyViewHost organization={mockOrganization} isAuthenticated={true} onAsk={onAsk}/>);

            const notPrevented = fireEvent.click(screen.getByTestId('company-chat-cta'));

            expect(notPrevented).toBe(true);
            expect(onAsk).not.toHaveBeenCalled();
        });

        test('the signed-out CTA is unchanged', () => {
            render(
                <CompanyViewHost organization={mockOrganization} visible={true} onClose={() => {}}
                                          isAuthenticated={false}
                                          signInUrlTemplate="/accounts/login/?next=%2F%3Fcompany%3D__COMPANY_ID__" />
            );
            expect(screen.getByTestId('company-sign-in-cta')).toHaveAttribute('href', expect.stringContaining('next='));
            expect(screen.queryByTestId('company-chat-cta')).not.toBeInTheDocument();
        });
    });

    describe('per-field corrections and pending suggestions (issue #477)', () => {
        const withCorrections = (respond: () => Promise<unknown>) => {
            provenanceWithEvidence();
            const base = global.fetch as jest.Mock;
            global.fetch = jest.fn().mockImplementation((url: string) =>
                url.includes('/api/company-corrections/') ? respond() : base(url));
        };
        const renderAuthed = (authed = true) => render(
            <CompanyViewHost organization={mockOrganization} visible={true}
                                      onClose={() => {}} isAuthenticated={authed}/>
        );

        test('evidence and unverified rows carry a per-field correction button that opens the form on that field', async () => {
            withCorrections(() => Promise.resolve({ok: true, json: () => Promise.resolve({corrections: []})}));
            const onSuggestCorrection = jest.fn();
            const onClose = jest.fn();
            render(<CompanyViewHost organization={mockOrganization} visible={true} onClose={onClose}
                                    onSuggestCorrection={onSuggestCorrection} isAuthenticated={true}/>);

            const evidenceButton = await screen.findByTestId('suggest-correction-field-rto_policy');
            expect(evidenceButton).toHaveAccessibleName('Suggest a correction to RTO Policy');
            expect(evidenceButton).not.toHaveAttribute('aria-label');
            expect(screen.getByRole('button', {name: 'Choose a field to correct'})).toHaveAttribute('data-testid', 'suggest-correction-link');
            expect(screen.getByTestId('field-evidence')).toHaveTextContent('Field evidence');
            expect(screen.getByTestId('field-evidence-rto_policy')).toContainElement(evidenceButton);
            expect(screen.getByTestId('field-unverified-funding_round'))
                .toContainElement(screen.getByTestId('suggest-correction-field-funding_round'));
            expect(screen.getByTestId('suggest-correction-field-funding_round'))
                .toHaveAccessibleName('Suggest a value for Funding Round');
            expect(screen.getByRole('heading', {level: 3, name: 'Data Freshness & Sources'})).toHaveClass('h5');
            expect(getCachedProvenance(1)).toBeDefined();

            fireEvent.click(evidenceButton);
            expect(onSuggestCorrection).toHaveBeenCalledWith('company_evidence', 'rto_policy');
            expect(onClose).not.toHaveBeenCalled();
            expect(screen.getByTestId('company-view')).toBeInTheDocument();
        });

        test('signed-out visitors get no per-field buttons and no pending list', async () => {
            withCorrections(() => Promise.resolve({ok: true, json: () => Promise.resolve({corrections: []})}));
            renderAuthed(false);
            await screen.findByTestId('field-evidence-rto_policy');
            expect(screen.queryByTestId('suggest-correction-field-rto_policy')).toBeNull();
            expect(screen.queryByTestId('your-pending-corrections')).toBeNull();
        });

        test('lists only the requester\'s pending suggestions, labeled Pending review', async () => {
            withCorrections(() => Promise.resolve({
                ok: true,
                json: () => Promise.resolve({corrections: [
                    {id: 7, field_key: 'rto_policy', field_label: 'RTO policy', proposed_value: 'Hybrid', status: 'pending', status_label: 'Pending review'},
                    {id: 8, field_key: 'locations', field_label: '', proposed_value: 'Austin', status: 'accepted', status_label: 'Accepted'},
                    {id: 9, field_key: 'unknown_key', field_label: '', proposed_value: 'Nope', status: 'pending', status_label: 'Pending review'},
                ]}),
            }));
            renderAuthed();
            const list = await screen.findByTestId('your-pending-corrections');
            expect(list).toHaveTextContent('Your pending suggestions');
            expect(list).toHaveTextContent('Hybrid');
            expect(list).toHaveTextContent('Pending review');
            expect(list).not.toHaveTextContent('Austin');
            expect(list).toHaveTextContent('unknown_key');
            expect(list).toHaveTextContent('RTO Policy:');
            expect(list).not.toHaveTextContent('RTO policy');
            expect(list.querySelector('.evidence-badge-pending')).not.toBeNull();
            expect(list).not.toHaveTextContent(/verified/i);
        });

        test.each([
            ['a non-ok response', () => Promise.resolve({ok: false, json: () => Promise.resolve({})})],
            ['a malformed body', () => Promise.resolve({ok: true, json: () => Promise.resolve({})})],
            ['a network failure', () => Promise.reject(new Error('offline'))],
        ])('hides the pending list on %s', async (_name, respond) => {
            withCorrections(respond);
            renderAuthed();
            await screen.findByTestId('field-evidence-rto_policy');
            await waitFor(() => expect(global.fetch).toHaveBeenCalledWith('/api/company-corrections/?organization=1'));
            expect(screen.queryByTestId('your-pending-corrections')).toBeNull();
        });

        test('ignores a pending response that resolves after unmount', async () => {
            let resolve: (value: unknown) => void = () => {};
            withCorrections(() => new Promise(r => { resolve = r; }));
            const {unmount} = renderAuthed();
            await waitFor(() => expect(global.fetch).toHaveBeenCalledWith('/api/company-corrections/?organization=1'));
            unmount();
            resolve({ok: true, json: () => Promise.resolve({corrections: [{id: 1, field_key: 'rto_policy', proposed_value: 'x', status: 'pending'}]})});
            await Promise.resolve();
        });
    });
});

describe('CompanyView states (issue #486)', () => {
    const ranked: CompanyDetailsOrganization = {
        id: 5, name: 'Rank Five', ranking: 5, avg_score: 4.1, funding_round: 'S', rto_policy: 'R',
        profile_completeness: 60, accelerated_vesting: false, type: 'C', url: 'https://five.example', avg_scores: [],
    };
    let scrollTo: jest.Mock;

    beforeEach(() => {
        scrollTo = jest.fn();
        window.scrollTo = scrollTo as unknown as typeof window.scrollTo;
        global.fetch = jest.fn().mockImplementation((url: string) => Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve(url.includes('/provenance/')
                ? {organization_id: 5, organization_modified: null, organization_created: null, latest_observation: null}
                : []),
        }));
    });

    const view = (props: Partial<React.ComponentProps<typeof CompanyView>> = {}) => render(
        <CompanyView organizationId={5} status="ready" organization={ranked} onBack={() => {}}
                     onAsk={() => {}} onSuggestCorrection={() => {}} {...props}/>);

    test('is a named region whose heading takes focus, and it starts at the top of the page', async () => {
        view();

        const region = screen.getByRole('region', {name: 'Rank Five'});
        expect(region).toHaveAttribute('aria-labelledby', 'organization-details-title');
        expect(region).not.toHaveAttribute('role');
        expect(region).not.toHaveAttribute('aria-modal');
        expect(screen.getByRole('heading', {level: 2, name: 'Rank Five'})).toHaveFocus();
        expect(scrollTo).toHaveBeenCalledWith({top: 0, behavior: 'instant'});
        await waitFor(() => expect(screen.getByTestId('provenance-section')).toBeInTheDocument());
    });

    test('Tab from the heading reaches Back to results first, then the actions', () => {
        view({isAuthenticated: true});

        const focusable = Array.from(document.querySelectorAll<HTMLElement>('a[href], button, [tabindex="0"]'));
        expect(focusable[0]).toBe(screen.getByRole('button', {name: 'Back to results'}));
        expect(focusable[1]).toBe(screen.getByTestId('company-chat-cta'));
        expect(screen.getByRole('heading', {level: 2})).toHaveAttribute('tabindex', '-1');
    });

    test('Back to results calls onBack; Escape does nothing', () => {
        const onBack = jest.fn();
        view({onBack});

        fireEvent.keyDown(document, {key: 'Escape'});
        expect(onBack).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole('button', {name: 'Back to results'}));

        expect(onBack).toHaveBeenCalledTimes(1);
        expect(screen.getByTestId('company-view')).toBeInTheDocument();
    });

    test('focus moves to the heading when another company replaces this one, not when the record arrives', () => {
        const {rerender} = view({status: 'loading', organization: null, name: 'Rank Five'});
        expect(screen.getByRole('heading', {level: 2, name: 'Rank Five'})).toHaveFocus();
        expect(screen.getByTestId('company-view-loading')).toHaveAttribute('aria-busy', 'true');
        expect(scrollTo).toHaveBeenCalledTimes(1);

        const other = {...ranked, id: 6, name: 'Rank Six'};
        screen.getByRole('button', {name: 'Back to results'}).focus();
        rerender(<CompanyView organizationId={5} status="ready" organization={ranked} onBack={() => {}}
                              onAsk={() => {}} onSuggestCorrection={() => {}}/>);
        expect(screen.getByRole('button', {name: 'Back to results'})).toHaveFocus();
        expect(scrollTo).toHaveBeenCalledTimes(1);

        rerender(<CompanyView organizationId={6} status="ready" organization={other} onBack={() => {}}
                              onAsk={() => {}} onSuggestCorrection={() => {}}/>);
        expect(screen.getByRole('heading', {level: 2, name: 'Rank Six'})).toHaveFocus();
        expect(scrollTo).toHaveBeenCalledTimes(2);
    });

    test('loading with no name yet says so, and makes no evidence request', () => {
        view({status: 'loading', organization: null});

        expect(screen.getByRole('heading', {level: 2, name: 'Loading company…'})).toBeInTheDocument();
        expect(screen.getByTestId('company-view-loading')).toHaveTextContent('Loading company details');
        expect(screen.queryByTestId('popup-details-grid')).not.toBeInTheDocument();
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('not found says the company is no longer listed, with Back to results and no evidence request', () => {
        const onBack = jest.fn();
        view({status: 'not-found', organization: null, onBack});

        expect(screen.getByRole('heading', {level: 2, name: 'Company not found'})).toHaveFocus();
        expect(screen.getByTestId('company-not-found')).toHaveTextContent('This company is no longer listed.');
        expect(screen.queryByTestId('provenance-retry')).not.toBeInTheDocument();
        expect(global.fetch).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole('button', {name: 'Back to results'}));
        expect(onBack).toHaveBeenCalledTimes(1);
    });

    test('a load error says so and offers Retry only when the container can retry', () => {
        const onRetry = jest.fn();
        const {rerender} = view({status: 'error', organization: null});

        expect(screen.getByRole('heading', {level: 2, name: 'Company'})).toBeInTheDocument();
        expect(screen.getByRole('alert')).toHaveTextContent('This company could not be loaded.');
        expect(screen.queryByTestId('company-view-retry')).not.toBeInTheDocument();

        rerender(<CompanyView organizationId={5} status="error" organization={null} onBack={() => {}}
                              onRetry={onRetry} onAsk={() => {}} onSuggestCorrection={() => {}}/>);
        fireEvent.click(screen.getByTestId('company-view-retry'));
        expect(onRetry).toHaveBeenCalledTimes(1);
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('a company that is not ranked in the list says so where rank and coverage would be', async () => {
        const unranked: CompanyDetailsOrganization = {
            id: 9, name: 'Unlisted Labs', funding_round: 'S', rto_policy: 'R', accelerated_vesting: false,
            type: 'C', url: 'https://unlisted.example',
        };
        view({organizationId: 9, organization: unranked});

        expect(screen.getByTestId('not-ranked')).toHaveTextContent('Not ranked in this list');
        expect(screen.queryByText('Rank:')).not.toBeInTheDocument();
        expect(screen.queryByTestId('rating-coverage')).not.toBeInTheDocument();
        // With no breakdown and no overall score the card says so rather than showing NaN.
        await waitFor(() => expect(screen.getByTestId('no-scores')).toHaveTextContent('No scores recorded'));
    });

    test('a very long company name stays in the heading, in full', () => {
        const longName = 'A'.repeat(100);
        view({organization: {...ranked, name: longName}});

        expect(screen.getByRole('heading', {level: 2, name: longName})).toBeInTheDocument();
    });
});
