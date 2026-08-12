// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

interface SuggestCompanyModalProps {
    visible: boolean;
    onClose: () => void;
}

interface SuggestCompanyModalState {
    companyName: string;
    websiteUrl: string;
    careersUrl: string;
    reason: string;
    submitting: boolean;
    error: string;
    fieldErrors: Record<string, string[]>;
    success: boolean;
}

function getCookie(name: string): string {
    const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match[2]) : '';
}

class SuggestCompanyModal extends React.Component<SuggestCompanyModalProps, SuggestCompanyModalState> {
    constructor(props: SuggestCompanyModalProps) {
        super(props);
        this.state = {
            companyName: '',
            websiteUrl: '',
            careersUrl: '',
            reason: '',
            submitting: false,
            error: '',
            fieldErrors: {},
            success: false,
        };
    }

    handleChange = (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
        const {name, value} = event.target;
        this.setState(prevState => ({...prevState, [name]: value}));
    };

    handleSubmit = async (event: React.FormEvent) => {
        event.preventDefault();
        this.setState({submitting: true, error: '', fieldErrors: {}, success: false});
        try {
            const response = await fetch('/api/company-requests/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken'),
                },
                body: JSON.stringify({
                    company_name: this.state.companyName,
                    website_url: this.state.websiteUrl,
                    careers_url: this.state.careersUrl,
                    reason: this.state.reason,
                }),
            });
            const data = await response.json();
            if (response.ok) {
                this.setState({success: true, submitting: false});
            } else {
                this.setState({
                    submitting: false,
                    error: data.error || 'Something went wrong. Please try again.',
                    fieldErrors: data.field_errors || {},
                });
            }
        } catch {
            this.setState({
                submitting: false,
                error: 'Network error. Please try again.',
            });
        }
    };

    handleClose = () => {
        this.setState({
            companyName: '',
            websiteUrl: '',
            careersUrl: '',
            reason: '',
            submitting: false,
            error: '',
            fieldErrors: {},
            success: false,
        });
        this.props.onClose();
    };

    render() {
        if (!this.props.visible) {
            return null;
        }
        const {companyName, websiteUrl, careersUrl, reason, submitting, error, fieldErrors, success} = this.state;
        return (
            <div className="modal d-block" tabIndex={-1} role="dialog" aria-modal="true"
                 data-testid="suggest-company-modal">
                <div className="modal-dialog" role="document">
                    <div className="modal-content">
                        <div className="modal-header">
                            <h5 className="modal-title">Suggest a company</h5>
                            <button type="button" className="btn-close" aria-label="Close"
                                    onClick={this.handleClose} data-testid="suggest-close-btn"></button>
                        </div>
                        <div className="modal-body">
                            {success ? (
                                <div data-testid="suggest-success">
                                    <p>Thanks! Your suggestion is in the review queue.</p>
                                    <p className="text-muted small">
                                        We evaluate suggestions as staff time allows. We will not promise a
                                        completion date, but your request is recorded and will be reviewed.
                                    </p>
                                    <button type="button" className="btn btn-secondary"
                                            onClick={this.handleClose}>Close</button>
                                </div>
                            ) : (
                                <form onSubmit={this.handleSubmit}>
                                    {error && (
                                        <div className="alert alert-danger" role="alert"
                                             data-testid="suggest-error">{error}</div>
                                    )}
                                    <div className="mb-3">
                                        <label htmlFor="suggest-company-name" className="form-label">
                                            Company name <span className="text-danger">*</span>
                                        </label>
                                        <input type="text" className="form-control"
                                               id="suggest-company-name" name="companyName"
                                               value={companyName} onChange={this.handleChange}
                                               maxLength={100} required
                                               aria-invalid={!!fieldErrors.company_name}
                                        />
                                        {fieldErrors.company_name && (
                                            <div className="invalid-feedback d-block">
                                                {fieldErrors.company_name.join(' ')}
                                            </div>
                                        )}
                                    </div>
                                    <div className="mb-3">
                                        <label htmlFor="suggest-website-url" className="form-label">
                                            Public website <span className="text-danger">*</span>
                                        </label>
                                        <input type="url" className="form-control"
                                               id="suggest-website-url" name="websiteUrl"
                                               value={websiteUrl} onChange={this.handleChange}
                                               placeholder="https://example.com" required
                                               aria-invalid={!!fieldErrors.website_url}
                                        />
                                        {fieldErrors.website_url && (
                                            <div className="invalid-feedback d-block">
                                                {fieldErrors.website_url.join(' ')}
                                            </div>
                                        )}
                                    </div>
                                    <div className="mb-3">
                                        <label htmlFor="suggest-careers-url" className="form-label">
                                            Careers page (optional)
                                        </label>
                                        <input type="url" className="form-control"
                                               id="suggest-careers-url" name="careersUrl"
                                               value={careersUrl} onChange={this.handleChange}
                                               placeholder="https://example.com/careers"
                                               aria-invalid={!!fieldErrors.careers_url}
                                        />
                                        {fieldErrors.careers_url && (
                                            <div className="invalid-feedback d-block">
                                                {fieldErrors.careers_url.join(' ')}
                                            </div>
                                        )}
                                    </div>
                                    <div className="mb-3">
                                        <label htmlFor="suggest-reason" className="form-label">
                                            Why should CRank evaluate this company? (optional)
                                        </label>
                                        <textarea className="form-control" id="suggest-reason"
                                                  name="reason" value={reason} onChange={this.handleChange}
                                                  maxLength={500} rows={3}
                                                  aria-invalid={!!fieldErrors.reason}
                                        />
                                        {fieldErrors.reason && (
                                            <div className="invalid-feedback d-block">
                                                {fieldErrors.reason.join(' ')}
                                            </div>
                                        )}
                                    </div>
                                    <div className="d-flex justify-content-end gap-2">
                                        <button type="button" className="btn btn-secondary"
                                                onClick={this.handleClose}>Cancel</button>
                                        <button type="submit" className="btn btn-primary"
                                                disabled={submitting} data-testid="suggest-submit-btn">
                                            {submitting ? 'Submitting…' : 'Submit suggestion'}
                                        </button>
                                    </div>
                                </form>
                            )}
                        </div>
                    </div>
                </div>
            </div>
        );
    }
}

export default SuggestCompanyModal;
