// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createPortal} from 'react-dom';
import {CORRECTABLE_FIELD_LABELS, fieldKeyLabel} from './labels';
import {lockBackground, unlockBackground} from './modalIsolation';
import {COMPANY_OPEN_EVENT} from './suggestCompany/controller';
import type {SuggestCompanyContext} from './suggestCompany/controller';

interface CompanyCorrectionFormProps {
    context: SuggestCompanyContext;
    onClose: () => void;
}

interface EvidenceRow {
    field_key: string;
    value: string;
    stale: boolean;
    last_verified_at: string | null;
}

interface ProvenanceResult {
    fields?: EvidenceRow[];
    unverified_fields?: string[];
}

interface SavedCorrection {
    proposed_value: string;
    current_value: string;
    status_label: string;
    field_label: string;
}

type ProvenanceState =
    {status: 'loading'} | {status: 'unavailable'} | {status: 'ready'; data: ProvenanceResult};

const FIELD_ORDER = Object.keys(CORRECTABLE_FIELD_LABELS);
const SCOPE_LEVELS: ReadonlyArray<[string, string]> = [
    ['company', 'Whole company'],
    ['team', 'A team'],
    ['role', 'A role'],
    ['location', 'A location'],
];
const FORM_FIELDS = ['field_key', 'proposed_value', 'evidence_url', 'scope_level', 'scope_value', 'note'];

const getCookie = (name: string): string => {
    const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match[2]) : '';
};

const newIdempotencyKey = (): string =>
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (ch) => {
              const n = Math.floor(Math.random() * 16);
              return (ch === 'x' ? n : (n & 0x3) | 0x8).toString(16);
          });

const formatDate = (isoString: string | null): string =>
    isoString
        ? new Date(isoString).toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric'})
        : 'Unknown';

export const describeCurrentValue = (
    provenance: ProvenanceState, fieldKey: string
): {kind: 'loading' | 'unavailable' | 'missing' | 'verified'; text: string; value: string} => {
    if (provenance.status === 'loading') {
        return {kind: 'loading', text: '', value: ''};
    }
    const row = provenance.status === 'ready'
        ? (provenance.data.fields || []).find(item => item.field_key === fieldKey)
        : undefined;
    if (row) {
        const verified = `last verified ${formatDate(row.last_verified_at)}`;
        return {
            kind: 'verified',
            value: row.value,
            text: row.stale ? `${row.value} — stale, ${verified}` : `${row.value} — ${verified}`,
        };
    }
    if (provenance.status === 'ready' && (provenance.data.unverified_fields || []).includes(fieldKey)) {
        return {kind: 'missing', text: 'No verified value on record', value: ''};
    }
    return {kind: 'unavailable', text: 'Current value unavailable', value: ''};
};

const CompanyCorrectionForm: React.FC<CompanyCorrectionFormProps> = ({context, onClose}) => {
    const organizationId = context.organizationId as number;
    const companyName = context.companyName || 'this company';
    const backToCompany = context.source === 'company_details' || context.source === 'company_evidence';

    const [fieldKey, setFieldKey] = React.useState(context.fieldKey || FIELD_ORDER[0]);
    const [proposedValue, setProposedValue] = React.useState('');
    const [evidenceUrl, setEvidenceUrl] = React.useState('');
    const [scopeLevel, setScopeLevel] = React.useState('company');
    const [scopeValue, setScopeValue] = React.useState('');
    const [note, setNote] = React.useState('');
    const [provenance, setProvenance] = React.useState<ProvenanceState>({status: 'loading'});
    const [reloadCount, setReloadCount] = React.useState(0);
    const [submitting, setSubmitting] = React.useState(false);
    const [error, setError] = React.useState('');
    const [fieldErrors, setFieldErrors] = React.useState<Record<string, string[]>>({});
    const [authRequired, setAuthRequired] = React.useState(false);
    const [saved, setSaved] = React.useState<SavedCorrection | null>(null);
    const [duplicate, setDuplicate] = React.useState(false);

    const dialogRef = React.useRef<HTMLDivElement>(null);
    const closeButtonRef = React.useRef<HTMLButtonElement>(null);
    const openerRef = React.useRef<HTMLElement | null>(null);
    const submitInFlight = React.useRef(false);
    const idempotencyKey = React.useRef(newIdempotencyKey());

    React.useEffect(() => {
        let cancelled = false;
        setProvenance({status: 'loading'});
        fetch(`/api/organizations/${organizationId}/provenance/`)
            .then(response => {
                if (!response.ok) {
                    throw new Error(`provenance ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                if (!cancelled) setProvenance({status: 'ready', data});
            })
            .catch(() => {
                if (!cancelled) setProvenance({status: 'unavailable'});
            });
        return () => {
            cancelled = true;
        };
    }, [organizationId, reloadCount]);

    // Opener is captured BEFORE lockBackground() (issue #464 contract). The
    // details dialog that launched this form unmounts around the same time,
    // so a disconnected opener falls back to the company's list entry.
    React.useLayoutEffect(() => {
        openerRef.current = document.activeElement instanceof HTMLElement
            ? document.activeElement : null;
        lockBackground();
        return () => {
            unlockBackground();
            const opener = openerRef.current;
            const fallback = Array.from(document.querySelectorAll<HTMLElement>(
                `[data-organization-id="${organizationId}"]`
            )).find((element) => element.getBoundingClientRect().height > 0) ?? null;
            const target = opener && opener.isConnected ? opener : fallback;
            if (target) {
                target.focus();
            } else if (document.activeElement instanceof HTMLElement) {
                document.activeElement.blur();
            }
        };
    }, [organizationId]);

    React.useEffect(() => {
        closeButtonRef.current?.focus();
    }, []);

    React.useEffect(() => {
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                // Capture phase: the assistant drawer's document-level Escape
                // handler must not also close what sits behind this dialog.
                event.stopPropagation();
                event.preventDefault();
                onClose();
                return;
            }
            if (event.key !== 'Tab' || !dialogRef.current) {
                return;
            }
            const focusables = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
                'a[href], button:not([disabled]), input:not([disabled]), '
                + 'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
            ));
            if (focusables.length === 0) return;
            const first = focusables[0];
            const last = focusables[focusables.length - 1];
            const active = document.activeElement;
            const inside = active instanceof HTMLElement && dialogRef.current.contains(active);
            if (event.shiftKey) {
                if (!inside || active === first) {
                    event.preventDefault();
                    last.focus();
                }
            } else if (!inside || active === last) {
                event.preventDefault();
                first.focus();
            }
        };
        window.addEventListener('keydown', handleKeyDown, true);
        return () => window.removeEventListener('keydown', handleKeyDown, true);
    }, [onClose]);

    React.useEffect(() => {
        const firstInvalid = FORM_FIELDS.find(name => fieldErrors[name]);
        if (firstInvalid) {
            dialogRef.current?.querySelector<HTMLElement>(`[data-field="${firstInvalid}"]`)?.focus();
        }
    }, [fieldErrors]);

    const current = describeCurrentValue(provenance, fieldKey);

    const handleBack = () => {
        onClose();
        if (backToCompany) {
            // After the form has unmounted and returned focus to the opener.
            window.setTimeout(() => {
                window.dispatchEvent(new CustomEvent(COMPANY_OPEN_EVENT, {detail: {organizationId}}));
            }, 0);
        }
    };

    const handleSubmit = async (event: React.FormEvent) => {
        event.preventDefault();
        if (submitInFlight.current) {
            return;
        }
        submitInFlight.current = true;
        setSubmitting(true);
        setError('');
        setFieldErrors({});
        setAuthRequired(false);
        setDuplicate(false);
        try {
            const response = await fetch('/api/company-corrections/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken'),
                },
                body: JSON.stringify({
                    organization_id: organizationId,
                    field_key: fieldKey,
                    proposed_value: proposedValue,
                    evidence_url: evidenceUrl,
                    scope: {level: scopeLevel, value: scopeValue},
                    note,
                    idempotency_key: idempotencyKey.current,
                }),
            });
            const data = await response.json();
            if (response.ok) {
                setSaved({
                    proposed_value: data.proposed_value,
                    current_value: data.current_value || '',
                    status_label: data.status_label,
                    field_label: data.field_label,
                });
            } else if (response.status === 401) {
                setAuthRequired(true);
            } else if (response.status === 409) {
                setDuplicate(true);
            } else {
                setError(data.error || 'Something went wrong. Please try again.');
                setFieldErrors(data.field_errors || {});
            }
        } catch {
            setError('Network error. Your draft is kept; please try again.');
        } finally {
            submitInFlight.current = false;
            setSubmitting(false);
        }
    };

    const describedBy = (name: string) => (fieldErrors[name] ? `correction-${name}-error` : undefined);
    const errorFor = (name: string) => fieldErrors[name] && (
        <div className="invalid-feedback d-block" id={`correction-${name}-error`}>
            {fieldErrors[name].join(' ')}
        </div>
    );

    const renderCurrent = () => {
        if (current.kind === 'loading') {
            return (
                <div className="correction-skeleton" aria-busy="true" data-testid="correction-current-loading">
                    <span className="visually-hidden">Loading the current value…</span>
                </div>
            );
        }
        return (
            <>
                <span data-testid="correction-current-text">{current.text}</span>
                {current.kind === 'unavailable' && (
                    <button type="button" className="btn btn-sm btn-link"
                            onClick={() => setReloadCount(count => count + 1)}
                            data-testid="correction-current-retry">Try again</button>
                )}
            </>
        );
    };

    let body: React.ReactNode;
    if (authRequired) {
        body = (
            <div className="text-center py-2" data-testid="correction-auth-required">
                <p className="mb-3">Sign in to suggest a correction.</p>
                <a href="/accounts/login/" className="btn btn-primary px-4">Sign in</a>
            </div>
        );
    } else if (saved || duplicate) {
        body = (
            <div role="status" data-testid="correction-status">
                {duplicate ? (
                    <p className="fw-bold mb-2" data-testid="correction-duplicate">
                        You already suggested a change to this field — Pending review
                    </p>
                ) : (
                    <>
                        <p className="mb-2">
                            <span className="badge bg-secondary me-1">Pending review</span>
                            Thanks — your suggestion was recorded and staff will review it.
                        </p>
                        <dl className="mb-2">
                            <dt>Your suggestion ({saved?.field_label})</dt>
                            <dd data-testid="correction-saved-proposed">{saved?.proposed_value}</dd>
                            <dt>Current value — Still in effect until review</dt>
                            <dd data-testid="correction-saved-current">
                                {saved?.current_value || 'No verified value on record'}
                            </dd>
                        </dl>
                    </>
                )}
            </div>
        );
    } else {
        body = (
            <form onSubmit={handleSubmit} noValidate data-testid="correction-form"
                  aria-labelledby="correction-title">
                <p className="text-muted small">
                    Suggestions enter a review queue. The current value stays in effect until staff review it.
                    We never fetch the evidence link on the server.
                </p>
                {error && (
                    <div className="alert alert-danger" role="alert" data-testid="correction-error">{error}</div>
                )}
                <div className="mb-3">
                    <label htmlFor="correction-field" className="form-label">Field</label>
                    <select id="correction-field" className="form-select" data-testid="correction-field"
                            data-field="field_key" value={fieldKey}
                            aria-invalid={!!fieldErrors.field_key} aria-describedby={describedBy('field_key')}
                            onChange={e => setFieldKey(e.target.value)}>
                        {FIELD_ORDER.map(key => (
                            <option key={key} value={key}>{fieldKeyLabel(key)}</option>
                        ))}
                    </select>
                    {errorFor('field_key')}
                </div>
                <div className="mb-3" data-testid="correction-current-value">
                    <div className="form-label fw-bold" id="correction-current-label">Current value</div>
                    <div aria-labelledby="correction-current-label" aria-live="polite">{renderCurrent()}</div>
                    {current.kind === 'missing' && (
                        <div className="text-muted small">Nothing is verified yet — your suggestion could be the first.</div>
                    )}
                </div>
                <div className="mb-3">
                    <label htmlFor="correction-proposed-value" className="form-label">Suggested value</label>
                    <input type="text" id="correction-proposed-value" className="form-control"
                           data-testid="correction-proposed-value" data-field="proposed_value"
                           value={proposedValue} maxLength={500} autoComplete="off"
                           aria-required="true" aria-invalid={!!fieldErrors.proposed_value}
                           aria-describedby={describedBy('proposed_value')}
                           onChange={e => setProposedValue(e.target.value)}/>
                    {errorFor('proposed_value')}
                </div>
                <div className="mb-3">
                    <label htmlFor="correction-evidence-url" className="form-label">Evidence link (https)</label>
                    <input type="url" id="correction-evidence-url" className="form-control"
                           data-testid="correction-evidence-url" data-field="evidence_url"
                           value={evidenceUrl} placeholder="https://example.com/careers" autoComplete="off"
                           aria-required="true" aria-invalid={!!fieldErrors.evidence_url}
                           aria-describedby={describedBy('evidence_url')}
                           onChange={e => setEvidenceUrl(e.target.value)}/>
                    {errorFor('evidence_url')}
                </div>
                <div className="mb-3">
                    <label htmlFor="correction-scope-level" className="form-label">Applies to</label>
                    <select id="correction-scope-level" className="form-select"
                            data-testid="correction-scope-level" data-field="scope_level" value={scopeLevel}
                            aria-invalid={!!fieldErrors.scope_level} aria-describedby={describedBy('scope_level')}
                            onChange={e => setScopeLevel(e.target.value)}>
                        {SCOPE_LEVELS.map(([value, label]) => (
                            <option key={value} value={value}>{label}</option>
                        ))}
                    </select>
                    {errorFor('scope_level')}
                </div>
                {scopeLevel !== 'company' && (
                    <div className="mb-3">
                        <label htmlFor="correction-scope-value" className="form-label">Which one?</label>
                        <input type="text" id="correction-scope-value" className="form-control"
                               data-testid="correction-scope-value" data-field="scope_value"
                               value={scopeValue} maxLength={100} autoComplete="off"
                               aria-required="true" aria-invalid={!!fieldErrors.scope_value}
                               aria-describedby={describedBy('scope_value')}
                               onChange={e => setScopeValue(e.target.value)}/>
                        {errorFor('scope_value')}
                    </div>
                )}
                <div className="mb-3">
                    <label htmlFor="correction-note" className="form-label">Note (optional)</label>
                    <textarea id="correction-note" className="form-control" rows={2} maxLength={500}
                              data-field="note" value={note} aria-invalid={!!fieldErrors.note}
                              aria-describedby={describedBy('note')}
                              onChange={e => setNote(e.target.value)}/>
                    {errorFor('note')}
                </div>
                <div className="d-flex justify-content-end gap-2">
                    <button type="button" className="btn btn-secondary" onClick={onClose}>Cancel</button>
                    <button type="submit" className="btn btn-primary" disabled={submitting}
                            data-testid="correction-submit">
                        {submitting ? 'Submitting…' : 'Submit suggestion'}
                    </button>
                </div>
            </form>
        );
    }

    const showBack = saved || duplicate;
    return createPortal(
        <div ref={dialogRef} className="modal d-block blocking-modal" tabIndex={-1} role="dialog"
             aria-modal="true" aria-labelledby="correction-title" data-testid="company-correction-modal">
            <div className="modal-dialog" role="document">
                <div className="modal-content">
                    <div className="modal-header">
                        <h5 className="modal-title" id="correction-title">Suggest a correction — {companyName}</h5>
                        <button ref={closeButtonRef} type="button" className="btn-close" aria-label="Close"
                                onClick={onClose} data-testid="correction-close"></button>
                    </div>
                    <div className="modal-body">{body}</div>
                    {showBack && (
                        <div className="modal-footer">
                            <button type="button" className="btn btn-primary" onClick={handleBack}
                                    data-testid="correction-back">
                                {backToCompany ? `Back to ${companyName}` : 'Back to results'}
                            </button>
                        </div>
                    )}
                </div>
            </div>
        </div>,
        document.body
    );
};

export default CompanyCorrectionForm;
