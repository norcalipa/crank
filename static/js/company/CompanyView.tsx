// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import CompanyDetails, {CompanyDetailsOrganization, CorrectionSource} from './CompanyDetails';

export type CompanyViewStatus = 'loading' | 'ready' | 'not-found' | 'error';

export interface CompanyViewProps {
    organizationId: number;
    status: CompanyViewStatus;
    // The company's record; null while it loads for an id that is not a list row.
    organization: CompanyDetailsOrganization | null;
    // Heading while the record loads, when the list already knows the name.
    name?: string;
    isAuthenticated?: boolean;
    // See CompanyDetails.
    signInUrlTemplate?: string;
    // Returns to the results. The view never closes itself.
    onBack: () => void;
    // Loads the record again after a failure.
    onRetry?: () => void;
    onAsk: () => void;
    onSuggestCorrection: (source: CorrectionSource, fieldKey?: string) => void;
}

/**
 * The company, shown inside the page's main content beside the assistant
 * (issue #486). A named region, not a dialog: nothing behind it is inert, the
 * document scrolls, Escape does nothing here, and Ask and Suggest correction
 * leave it open. On open the heading takes focus and the view starts at the
 * top, so a screen reader announces the company and Tab reaches Back to
 * results, the actions and then the content.
 */
const CompanyView: React.FC<CompanyViewProps> = ({
    organizationId,
    status,
    organization,
    name,
    isAuthenticated = false,
    signInUrlTemplate = '',
    onBack,
    onRetry,
    onAsk,
    onSuggestCorrection,
}) => {
    const headingRef = React.useRef<HTMLHeadingElement>(null);

    // Keyed on the id: opening another company from inside the view moves
    // focus to its heading too, while the record arriving later does not.
    React.useEffect(() => {
        window.scrollTo({top: 0, behavior: 'instant' as ScrollBehavior});
        headingRef.current?.focus({preventScroll: true});
    }, [organizationId]);

    const heading = organization
        ? organization.name
        : (name ?? (status === 'not-found' ? 'Company not found' : (status === 'error' ? 'Company' : 'Loading company…')));

    return (
        <section className="company-view popup-details" data-testid="company-view"
                 data-organization-id={organizationId} aria-labelledby="organization-details-title">
            <div className="company-view-header">
                <h2 id="organization-details-title" ref={headingRef} tabIndex={-1}>{heading}</h2>
                <button type="button" className="btn btn-outline-light company-view-back" data-testid="company-back"
                        onClick={onBack}>
                    <i className="fa-solid fa-arrow-left" aria-hidden="true"></i>
                    {' '}Back to results
                </button>
            </div>
            {status === 'not-found' ? (
                <div className="card-body company-view-not-found" data-testid="company-not-found" role="status">
                    <p className="mb-0">This company is no longer listed.</p>
                </div>
            ) : status === 'error' ? (
                <div className="card-body company-view-error" data-testid="company-view-error" role="alert">
                    <p>This company could not be loaded.</p>
                    {onRetry && (
                        <button type="button" className="btn btn-outline-light" data-testid="company-view-retry"
                                onClick={onRetry}>
                            Retry
                        </button>
                    )}
                </div>
            ) : organization ? (
                <CompanyDetails
                    organization={organization}
                    isAuthenticated={isAuthenticated}
                    signInUrlTemplate={signInUrlTemplate}
                    onAsk={onAsk}
                    onSuggestCorrection={onSuggestCorrection}
                />
            ) : (
                <div className="card-body company-view-loading" data-testid="company-view-loading"
                     role="status" aria-busy="true">
                    <p className="text-body-secondary mb-0">Loading company details…</p>
                </div>
            )}
        </section>
    );
};

export default CompanyView;
