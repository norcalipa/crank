// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createPortal} from 'react-dom';
import {lockBackground, unlockBackground} from './modalIsolation';
import type {SuggestCompanyContext} from './suggestCompany/controller';

interface SuggestCompanyModalProps {
    visible: boolean;
    onClose: () => void;
    // Prefill/context from the trigger (issue #471). Only `companyName` is
    // rendered into the form; `source`/`searchTerm`/`page`/`organizationId`
    // are the caller's concern and are never sent to the API.
    context?: SuggestCompanyContext | null;
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
    // Set on a 401 response: renders the sign-in prompt instead of the form
    // (issue #471 AC-8/AC-12) rather than a generic error banner.
    authRequired: boolean;
}

function getCookie(name: string): string {
    const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match[2]) : '';
}

class SuggestCompanyModal extends React.Component<SuggestCompanyModalProps, SuggestCompanyModalState> {
    constructor(props: SuggestCompanyModalProps) {
        super(props);
        this.state = {
            companyName: props.context?.companyName || '',
            websiteUrl: '',
            careersUrl: '',
            reason: '',
            submitting: false,
            error: '',
            fieldErrors: {},
            success: false,
            authRequired: false,
        };
    }

    private closeButtonRef = React.createRef<HTMLButtonElement>();
    private modalRef = React.createRef<HTMLDivElement>();
    // Element that had focus when the dialog opened (the trigger). Restored on
    // close so keyboard and pointer users return to where they left off.
    private openerRef: HTMLElement | null = null;
    // Set by handler-initiated closes so the focus restore is retried after
    // background isolation is released (issue #464): the opener lives in the
    // inert background, so the synchronous restore attempt alone can
    // silently fail in real browsers.
    private pendingRestoreFocus = false;
    // Synchronous double-submit guard (issue #471 AC-7): `disabled={submitting}`
    // alone is not enough because two synchronous submit events in the same
    // tick both fire before React commits the state update.
    private submitInFlight = false;

    componentDidMount() {
        // Guard against a stale keydown listener after the modal closes.
        document.addEventListener('keydown', this.handleDocumentKeyDown);
        if (this.props.visible) {
            // Isolate the background when the modal mounts already open
            // (issue #464).
            lockBackground();
        }
    }

    componentWillUnmount() {
        document.removeEventListener('keydown', this.handleDocumentKeyDown);
        if (this.props.visible) {
            // Never leave the page scroll-locked or the background inert if
            // the modal unmounts while it is open (issue #464).
            unlockBackground();
        }
    }

    getSnapshotBeforeUpdate(prevProps: SuggestCompanyModalProps): boolean {
        // Runs before the DOM update: record whether keyboard focus is inside
        // the modal while it is about to close. Once the modal unmounts the
        // browser resets focus to <body>, so this is the only reliable place
        // to detect it (issue #464).
        return Boolean(
            prevProps.visible && this.modalRef.current
            && document.activeElement instanceof HTMLElement
            && this.modalRef.current.contains(document.activeElement)
        );
    }

    componentDidUpdate(prevProps: SuggestCompanyModalProps, _prevState: Readonly<SuggestCompanyModalState>, focusWasInside: boolean) {
        if (this.props.visible && !prevProps.visible) {
            // Capture the opener BEFORE lockBackground() inerts the shell (issue
            // #464 contract, mirrored from OrganizationDetailsPopup): inerting
            // blurs the trigger and resets activeElement to <body> in real
            // browsers, so capturing after the lock would lose the focus-restore
            // target and Escape/Close could not return focus to the opener.
            this.openerRef = document.activeElement instanceof HTMLElement
                ? document.activeElement : null;
            // On open, isolate the background (inert/aria-hidden app shell +
            // main content + modal-external skip link, and a scroll lock on
            // the actual document scroller) for the modal's lifetime, then
            // move focus into the dialog (WAI-ARIA dialog pattern, issue #464).
            lockBackground();
            this.pendingRestoreFocus = false;
            this.closeButtonRef.current?.focus();
            // Prefill the company name from the trigger's context (issue
            // #471 AC-5). A reopen with no context (or no companyName)
            // leaves the field empty rather than carrying over a stale value.
            this.setState({companyName: this.props.context?.companyName || ''});
        } else if (!this.props.visible && prevProps.visible) {
            // Release background isolation BEFORE restoring focus: the
            // opener sits in the inert background, so restoring earlier
            // would silently fail in real browsers (issue #464 focus-restore
            // contract). Handler-initiated closes set pendingRestoreFocus;
            // parent-initiated closes restore whenever focus was inside.
            unlockBackground();
            if (focusWasInside || this.pendingRestoreFocus) {
                this.restoreFocusToOpener();
                this.pendingRestoreFocus = false;
            }
        }
    }

    // Restore focus to the trigger element on close (WAI-ARIA dialog pattern).
    // If the opener is no longer in the document, defensively blur the active
    // element so focus never lingers on a now-hidden node.
    private restoreFocusToOpener = () => {
        if (this.openerRef && this.openerRef.isConnected) {
            this.openerRef.focus();
        } else if (document.activeElement instanceof HTMLElement) {
            document.activeElement.blur();
        }
    };

    private getFocusableElements = (): HTMLElement[] => {
        const dialog = this.modalRef.current;
        if (!dialog) return [];
        return Array.from(dialog.querySelectorAll<HTMLElement>(
            'a[href], button:not([disabled]), input:not([disabled]), '
            + 'select:not([disabled]), textarea:not([disabled]), '
            + '[tabindex]:not([tabindex="-1"])'
        ));
    };

    private handleDocumentKeyDown = (event: KeyboardEvent) => {
        if (!this.props.visible) return;
        if (event.key === 'Escape') {
            // WAI-ARIA dialog pattern: handleClose returns focus to the
            // trigger element, retried after background isolation is
            // released (issue #464).
            this.handleClose();
            return;
        }
        if (event.key === 'Tab') {
            // WAI-ARIA focus trap (issue #464): cycle Tab/Shift+Tab among the
            // dialog's own focusable elements so keyboard focus can never move
            // behind the modal into the page background.
            const focusables = this.getFocusableElements();
            if (focusables.length === 0) return;
            const first = focusables[0];
            const last = focusables[focusables.length - 1];
            const active = document.activeElement;
            const insideDialog = active instanceof HTMLElement
                && this.modalRef.current?.contains(active);
            if (event.shiftKey) {
                if (!insideDialog || active === first) {
                    event.preventDefault();
                    last.focus();
                }
            } else if (!insideDialog || active === last) {
                event.preventDefault();
                first.focus();
            }
        }
    };

    handleChange = (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
        const {name, value} = event.target;
        this.setState(prevState => ({...prevState, [name]: value}));
    };

    handleSubmit = async (event: React.FormEvent) => {
        event.preventDefault();
        // Double-click guard (issue #471 AC-7): the disabled attribute alone
        // takes effect only after React commits, so a second synchronous
        // submit in the same tick would otherwise still reach fetch.
        if (this.submitInFlight) {
            return;
        }
        this.submitInFlight = true;
        this.setState({submitting: true, error: '', fieldErrors: {}, success: false, authRequired: false});
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
            } else if (response.status === 401) {
                // Assistant-triggered opens are not auth-gated (issue #471
                // AC-12), so a signed-out submit surfaces here instead of
                // silently doing nothing.
                this.setState({submitting: false, authRequired: true});
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
        } finally {
            this.submitInFlight = false;
        }
    };

    handleClose = () => {
        // Return focus to the trigger element (WAI-ARIA dialog pattern). The
        // synchronous attempt keeps the contract for direct callers; in real
        // browsers the opener is still inert at this point, so the restore is
        // retried by componentDidUpdate after the isolation is released
        // (issue #464).
        this.restoreFocusToOpener();
        this.pendingRestoreFocus = true;
        this.setState({
            companyName: '',
            websiteUrl: '',
            careersUrl: '',
            reason: '',
            submitting: false,
            error: '',
            fieldErrors: {},
            success: false,
            authRequired: false,
        });
        this.props.onClose();
    };

    render() {
        if (!this.props.visible) {
            return null;
        }
        const {companyName, websiteUrl, careersUrl, reason, submitting, error, fieldErrors, success, authRequired} = this.state;
        // Render the dialog into a portal on <body>: the blocking dialog must
        // not live inside the (inert) background containers while it is open.
        return createPortal(
            <div ref={this.modalRef} className="modal d-block blocking-modal" tabIndex={-1} role="dialog" aria-modal="true"
                 data-testid="suggest-company-modal">
                <div className="modal-dialog" role="document">
                    <div className="modal-content">
                        <div className="modal-header">
                            <h5 className="modal-title">Suggest a company</h5>
                            <button ref={this.closeButtonRef} type="button" className="btn-close" aria-label="Close"
                                    onClick={this.handleClose} data-testid="suggest-close-btn"></button>
                        </div>
                        <div className="modal-body">
                            {authRequired ? (
                                <div className="text-center py-2" data-testid="suggest-auth-required">
                                    <p className="mb-3">Sign in to suggest a company.</p>
                                    <div className="d-grid gap-2 d-sm-block">
                                        <a href="/accounts/login/" className="btn btn-primary px-4"
                                           data-testid="suggest-sign-in-link">Sign in</a>
                                    </div>
                                </div>
                            ) : success ? (
                                <div data-testid="suggest-success">
                                    <p>Thanks! Your suggestion is in the review queue.</p>
                                    <p className="text-muted small">
                                        Submissions enter review and do not immediately change company
                                        facts. We evaluate suggestions as staff time allows. We will not
                                        promise a completion date, but your request is recorded and will
                                        be reviewed.
                                    </p>
                                    <button type="button" className="btn btn-secondary"
                                            onClick={this.handleClose}>Close</button>
                                </div>
                            ) : (
                                <form onSubmit={this.handleSubmit}>
                                    <p className="text-muted small" data-testid="suggest-review-notice">
                                        Suggestions enter a review queue and do not immediately change
                                        company facts.
                                    </p>
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
            </div>,
            document.body
        );
    }
}

export default SuggestCompanyModal;
