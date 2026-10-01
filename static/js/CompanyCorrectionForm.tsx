// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createPortal} from 'react-dom';
import {CORRECTABLE_FIELD_LABELS, fieldKeyLabel} from './labels';
import {lockBackground, unlockBackground} from './modalIsolation';
import {getCachedProvenance, setCachedProvenance} from './provenanceCache';
import {COMPANY_OPEN_EVENT} from './suggestCompany/controller';
import {getWorkspaceSnapshot, openAssistant} from './workspace/store';
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
    source_domain?: string | null;
    scope?: Record<string, unknown> | null;
}

interface ProvenanceResult {
    fields?: EvidenceRow[];
    unverified_fields?: string[];
    displayed_values?: Record<string, string>;
}

interface PendingSuggestion {
    field_key: string;
    proposed_value: string;
}

interface SavedCorrection {
    field_label: string;
    proposed_value: string;
    current_value: string;
    status_label: string;
}

type ProvenanceState =
    {status: 'loading'} | {status: 'unavailable'} | {status: 'ready'; data: ProvenanceResult};

const FIELD_ORDER = Object.keys(CORRECTABLE_FIELD_LABELS);
const SCOPE_LEVELS: ReadonlyArray<[string, string]> = [
    ['company', 'Whole company'],
    ['role', 'A role'],
    ['location', 'A location'],
];
const VALIDATION_SUMMARY = 'Please correct the highlighted fields.';
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
): {
    kind: 'loading' | 'unavailable' | 'missing' | 'verified' | 'unselected';
    text: string; value: string; meta: string; domain: string; stale: boolean; date: string;
    displayed: string;
} => {
    const displayed = provenance.status === 'ready'
        ? (provenance.data.displayed_values || {})[fieldKey] || '' : '';
    const blank = {value: '', meta: '', domain: '', stale: false, date: '', displayed};
    if (!fieldKey) {
        return {kind: 'unselected', text: 'Choose a field to see its current value', ...blank};
    }
    if (provenance.status === 'loading') {
        return {kind: 'loading', text: '', ...blank};
    }
    const row = provenance.status === 'ready'
        ? (provenance.data.fields || []).find(item => item.field_key === fieldKey)
        : undefined;
    if (row) {
        const date = formatDate(row.last_verified_at);
        const source = row.source_domain ? `from ${row.source_domain} · ` : '';
        const verified = `${source}last verified ${date}`;
        return {
            kind: 'verified',
            value: row.value,
            meta: source,
            domain: row.source_domain || '',
            stale: row.stale,
            date,
            displayed,
            text: row.stale ? `${row.value} · ${verified} · Stale` : `${row.value} · ${verified}`,
        };
    }
    if (provenance.status === 'ready' && (provenance.data.unverified_fields || []).includes(fieldKey)) {
        return {kind: 'missing', text: 'No verified value on record', ...blank};
    }
    return {kind: 'unavailable', text: 'Current value unavailable', ...blank};
};

const CompanyCorrectionForm: React.FC<CompanyCorrectionFormProps> = ({context, onClose}) => {
    const organizationId = context.organizationId as number;
    const companyName = context.companyName || 'this company';
    const backToCompany = context.source === 'company_details' || context.source === 'company_evidence';

    const [fieldKey, setFieldKey] = React.useState(context.fieldKey || '');
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
    const [rateLimited, setRateLimited] = React.useState(false);
    const [retryAt, setRetryAt] = React.useState<number | null>(null);
    const [pending, setPending] = React.useState<PendingSuggestion[]>([]);
    const [announcement, setAnnouncement] = React.useState('');

    const dialogRef = React.useRef<HTMLDivElement>(null);
    const closeButtonRef = React.useRef<HTMLButtonElement>(null);
    const backButtonRef = React.useRef<HTMLButtonElement>(null);
    const openerRef = React.useRef<HTMLElement | null>(null);
    const errorRef = React.useRef<HTMLDivElement>(null);
    const submitInFlight = React.useRef(false);
    const focusInvalidAfterSubmit = React.useRef(false);
    const focusAlertAfterError = React.useRef(false);
    const idempotencyKey = React.useRef(newIdempotencyKey());
    const lastSentPayload = React.useRef('');

    React.useEffect(() => {
        let cancelled = false;
        const cached = getCachedProvenance(organizationId);
        setProvenance(cached ? {status: 'ready', data: cached} : {status: 'loading'});
        fetch(`/api/organizations/${organizationId}/provenance/`)
            .then(response => {
                if (!response.ok) {
                    throw new Error(`provenance ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                setCachedProvenance(organizationId, data);
                if (!cancelled) setProvenance({status: 'ready', data});
            })
            .catch(() => {
                if (!cancelled && !cached) setProvenance({status: 'unavailable'});
            });
        return () => {
            cancelled = true;
        };
    }, [organizationId, reloadCount]);

    React.useEffect(() => {
        let cancelled = false;
        fetch(`/api/company-corrections/?organization=${organizationId}`)
            .then(response => (response.ok ? response.json() : null))
            .then(data => {
                if (cancelled || !data) return;
                setPending((data.corrections || [])
                    .filter((item: {status: string}) => item.status === 'pending')
                    .map((item: {field_key: string; proposed_value: string}) => ({
                        field_key: item.field_key, proposed_value: item.proposed_value,
                    })));
            })
            .catch(() => undefined);
        return () => {
            cancelled = true;
        };
    }, [organizationId]);

    // Opener is captured BEFORE lockBackground() (issue #464 contract). The
    // details dialog that launched this form unmounts around the same time,
    // so a disconnected opener falls back to the company's list entry.
    React.useLayoutEffect(() => {
        const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        openerRef.current = opener;
        // The phone assistant sheet yields to this dialog (issue #472), so
        // it has to be brought back, with focus on the control that opened
        // the form, once the dialog closes (issue #477 AC 8).
        const sheetWasOpen = getWorkspaceSnapshot().visibility === 'open';
        const openerTestId = opener?.getAttribute('data-testid') || '';
        // Cards for one company share a test id, so remember which of them
        // opened the form rather than refocusing the first.
        const openerIndex = openerTestId
            ? Array.from(document.querySelectorAll(`[data-testid="${openerTestId}"]`)).indexOf(opener!)
            : -1;
        lockBackground();
        return () => {
            unlockBackground();
            if (sheetWasOpen && getWorkspaceSnapshot().visibility === 'closed') {
                openAssistant();
                if (openerTestId) {
                    let frames = 0;
                    const refocus = () => {
                        const matches = Array.from(document.querySelectorAll<HTMLElement>(
                            `[data-testid="${openerTestId}"]`
                        ));
                        const visible = (element?: HTMLElement) =>
                            !!element && element.getBoundingClientRect().height > 0;
                        const target = visible(matches[openerIndex])
                            ? matches[openerIndex]
                            : (frames >= 60 ? matches.find(visible) : undefined);
                        if (target) {
                            target.focus();
                        } else if (frames++ < 120) {
                            window.requestAnimationFrame(refocus);
                        }
                    };
                    window.requestAnimationFrame(refocus);
                }
                return;
            }
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
        const frame = window.requestAnimationFrame(() => {
            // A slow frame must not steal focus the dialog already placed itself
            // (Back after success, first invalid field).
            if (dialogRef.current!.contains(document.activeElement)) return;
            closeButtonRef.current!.focus();
        });
        return () => window.cancelAnimationFrame(frame);
    }, []);

    const showBack = !!saved || duplicate;
    React.useEffect(() => {
        if (duplicate) {
            setAnnouncement('You already suggested a change to this field. Pending review.');
        } else if (saved) {
            setAnnouncement(`Suggestion submitted for ${saved.field_label}: ${saved.proposed_value}. `
                + 'Pending review. Staff will review it.');
        } else {
            setAnnouncement('');
        }
    }, [saved, duplicate]);
    React.useEffect(() => {
        if (showBack) {
            backButtonRef.current?.focus();
        }
    }, [showBack]);

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
        if (!focusInvalidAfterSubmit.current) return;
        focusInvalidAfterSubmit.current = false;
        const firstInvalid = FORM_FIELDS.find(name => fieldErrors[name]);
        if (firstInvalid) {
            dialogRef.current?.querySelector<HTMLElement>(`[data-field="${firstInvalid}"]`)?.focus();
        }
    }, [fieldErrors]);

    React.useEffect(() => {
        if (error === VALIDATION_SUMMARY && Object.keys(fieldErrors).length === 0) setError('');
    }, [error, fieldErrors]);

    React.useEffect(() => {
        if (focusAlertAfterError.current && error) {
            focusAlertAfterError.current = false;
            errorRef.current?.focus();
        }
    }, [error]);

    React.useEffect(() => {
        if (retryAt === null) return;
        const timer = window.setTimeout(() => {
            setRateLimited(false);
            setError('');
            setRetryAt(null);
        }, Math.max(0, retryAt - Date.now()));
        return () => window.clearTimeout(timer);
    }, [retryAt]);

    const clearFieldError = (name: string) => setFieldErrors(prev => {
        if (!prev[name]) return prev;
        const {[name]: _cleared, ...rest} = prev;
        return rest;
    });

    const current = describeCurrentValue(provenance, fieldKey);
    const pendingForField = pending.find(item => item.field_key === fieldKey);
    const currentRow = provenance.status === 'ready'
        ? (provenance.data.fields || []).find(item => item.field_key === fieldKey)
        : undefined;
    const scopedBlocked = !!currentRow && !currentRow.scope?.countries && !currentRow.scope?.role_families;
    React.useEffect(() => {
        if (scopedBlocked) {
            setScopeLevel('company');
            clearFieldError('scope_value');
        }
    }, [scopedBlocked]);

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
        if (submitInFlight.current || pendingForField) {
            return;
        }
        const clientErrors: Record<string, string[]> = {};
        if (!fieldKey) clientErrors.field_key = ['Choose what to correct.'];
        if (!proposedValue.trim()) clientErrors.proposed_value = ['Enter the corrected value.'];
        if (!evidenceUrl.trim()) {
            clientErrors.evidence_url = ['Add a public link that starts with https://.'];
        } else if (!/^https:\/\//i.test(evidenceUrl.trim())) {
            clientErrors.evidence_url = ['Use a link that starts with https://.'];
        }
        if (scopeLevel !== 'company' && !scopeValue.trim()) {
            clientErrors.scope_value = ['Say which one this applies to.'];
        }
        if (Object.keys(clientErrors).length) {
            setError(VALIDATION_SUMMARY);
            focusInvalidAfterSubmit.current = true;
            setFieldErrors(clientErrors);
            return;
        }
        const payload = {
            organization_id: organizationId,
            field_key: fieldKey,
            proposed_value: proposedValue,
            evidence_url: evidenceUrl,
            scope: {level: scopeLevel, value: scopeValue},
            note,
        };
        const signature = JSON.stringify(payload);
        if (lastSentPayload.current && lastSentPayload.current !== signature) {
            idempotencyKey.current = newIdempotencyKey();
        }
        lastSentPayload.current = signature;
        submitInFlight.current = true;
        setSubmitting(true);
        setError('');
        setFieldErrors({});
        setAuthRequired(false);
        setDuplicate(false);
        setRateLimited(false);
        setRetryAt(null);
        try {
            const response = await fetch('/api/company-corrections/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken'),
                },
                body: JSON.stringify({...payload, idempotency_key: idempotencyKey.current}),
            });
            const data = await response.json();
            if (response.ok) {
                setSaved({
                    field_label: data.field_label || fieldKeyLabel(fieldKey),
                    proposed_value: data.proposed_value,
                    current_value: data.current_value || '',
                    status_label: data.status_label,
                });
            } else if (response.status === 401) {
                setAuthRequired(true);
            } else if (response.status === 409) {
                setDuplicate(true);
            } else if (response.status === 429) {
                const wait = Number(response.headers.get('Retry-After'));
                const minutes = Number.isFinite(wait) && wait > 0 ? Math.ceil(wait / 60) : 0;
                setRateLimited(true);
                if (minutes) {
                    setRetryAt(Date.now() + wait * 1000);
                }
                focusAlertAfterError.current = true;
                setError(minutes
                    ? `Hourly limit reached. Submit again in ${minutes} min — keep this open.`
                    : 'Hourly limit reached. Please try again later.');
            } else {
                const serverFieldErrors = data.field_errors || {};
                if (Object.keys(serverFieldErrors).length) {
                    focusInvalidAfterSubmit.current = true;
                } else {
                    focusAlertAfterError.current = true;
                }
                setError(data.error || 'Something went wrong. Please try again.');
                setFieldErrors(serverFieldErrors);
            }
        } catch {
            focusAlertAfterError.current = true;
            setError('Network error. Your draft is kept; please try again.');
        } finally {
            submitInFlight.current = false;
            setSubmitting(false);
        }
    };

    const describedBy = (name: string) => (fieldErrors[name] ? `correction-${name}-error` : undefined);
    const invalid = (name: string) => (fieldErrors[name] ? ' is-invalid' : '');
    const errorFor = (name: string) => fieldErrors[name] && (
        <div className="invalid-feedback d-block" id={`correction-${name}-error`}>
            {fieldErrors[name][0]}
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
        if (current.kind === 'verified') {
            return (
                <span data-testid="correction-current-text">
                    <span className="small text-body-secondary d-block">Verified evidence</span>
                    <span className="fw-semibold me-2">{current.value}</span>
                    <span className="small text-body-secondary d-inline-block">
                        {current.domain && <><span className="text-nowrap">{current.domain},</span>{' '}</>}
                        <span className="text-nowrap">last verified {current.date}</span>
                    </span>
                    {current.stale && <>{' '}<span className="badge text-bg-warning">Stale</span></>}
                </span>
            );
        }
        if (current.kind === 'unavailable') {
            return (
                <>
                    <span className="text-warning-emphasis" data-testid="correction-current-text">
                        <i className="fa-solid fa-triangle-exclamation me-1" aria-hidden="true"></i>
                        {current.text}
                    </span>
                    <button type="button" className="btn btn-link p-0"
                            onClick={() => setReloadCount(count => count + 1)}
                            data-testid="correction-current-retry">Try again</button>
                </>
            );
        }
        return (
            <span className="text-body-secondary" data-testid="correction-current-text">{current.text}</span>
        );
    };

    const renderDisplayed = () => {
        if (!current.displayed || current.kind === 'loading' || current.kind === 'unselected') {
            return null;
        }
        return (
            <div className="small mt-2" data-testid="correction-displayed-value">
                <span className="text-body-secondary d-block">Shown on the company card</span>
                <span className="fw-semibold">{current.displayed}</span>
                {current.kind === 'verified' && current.displayed !== current.value && (
                    <span className="text-body-secondary d-block">
                        These can differ: the card uses the company profile, and the evidence record is
                        the sourced record. Accepting a suggestion updates the evidence record only.
                    </span>
                )}
            </div>
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
            <div id="correction-status-text" data-testid="correction-status">
                {duplicate ? (
                    <p className="fw-bold mb-2" data-testid="correction-duplicate">
                        You already suggested a change to this field — Pending review
                    </p>
                ) : (
                    <>
                        <div className="d-flex align-items-start gap-2 mb-3">
                            <i className="fa-solid fa-circle-check text-success fs-5 mt-1" aria-hidden="true"></i>
                            <div>
                                <p className="fw-semibold mb-1">Suggestion submitted</p>
                                <p className="small text-body-secondary mb-0">
                                    Staff will review it. Nothing changes until then. If accepted, it updates
                                    the evidence record only; it does not change the value on the company card.
                                </p>
                            </div>
                        </div>
                        <dl className="correction-summary mb-0">
                            <dt>Your suggestion · {saved?.field_label}</dt>
                            <dd className="d-flex flex-wrap align-items-center gap-2">
                                <span data-testid="correction-saved-proposed">{saved?.proposed_value}</span>
                                <span className="badge text-bg-warning badge-pending">
                                    <i className="fa-solid fa-hourglass-half me-1" aria-hidden="true"></i>
                                    Pending review
                                </span>
                            </dd>
                            <dt>Verified evidence</dt>
                            <dd className="mb-0">
                                <span data-testid="correction-saved-current">
                                    {saved?.current_value || 'No verified value on record'}
                                </span>
                                <div className="small text-body-secondary">Still in effect until review</div>
                            </dd>
                        </dl>
                    </>
                )}
            </div>
        );
    } else {
        body = (
            <form id="correction-form" onSubmit={handleSubmit} noValidate data-testid="correction-form"
                  aria-labelledby="correction-title">
                <p className="small text-body-secondary mb-3">
                    Staff review every suggestion. The current value stays until one is approved.
                </p>
                {pendingForField && (
                    <div id="correction-pending-notice" role="status" className="alert alert-info py-2 small"
                         data-testid="correction-pending-notice">
                        You already suggested <strong>{pendingForField.proposed_value}</strong> for{' '}
                        {fieldKeyLabel(fieldKey)} — Pending review. Choose another field, or wait for staff.
                    </div>
                )}
                <div className="mb-3">
                    <label htmlFor="correction-field" className="form-label">Field</label>
                    <select id="correction-field" className={`form-select${invalid('field_key')}`} data-testid="correction-field"
                            data-field="field_key" value={fieldKey}
                            aria-invalid={!!fieldErrors.field_key} aria-describedby={describedBy('field_key')}
                            onChange={e => {
                                setFieldKey(e.target.value);
                                if (e.target.value) clearFieldError('field_key');
                            }}>
                        {!context.fieldKey && (
                            <option value="" disabled>Choose what to correct…</option>
                        )}
                        {FIELD_ORDER.map(key => (
                            <option key={key} value={key}>{fieldKeyLabel(key)}</option>
                        ))}
                    </select>
                    {errorFor('field_key')}
                </div>
                <div className="mb-3" data-testid="correction-current-value">
                    <div className="form-label" id="correction-current-label">Current value</div>
                    <div className="correction-current" aria-labelledby="correction-current-label"
                         aria-live="polite">{renderCurrent()}</div>
                    {renderDisplayed()}
                    {current.kind === 'missing' && (
                        <div className="text-body-secondary small mt-1">
                            No evidence record yet — your suggestion could be the first.
                        </div>
                    )}
                </div>
                <div className="mb-3">
                    <label htmlFor="correction-proposed-value" className="form-label">Suggested value</label>
                    <input type="text" id="correction-proposed-value" className={`form-control${invalid('proposed_value')}`}
                           data-testid="correction-proposed-value" data-field="proposed_value"
                           value={proposedValue} maxLength={500} autoComplete="off"
                           aria-required="true" aria-invalid={!!fieldErrors.proposed_value}
                           aria-describedby={describedBy('proposed_value')}
                           onChange={e => {
                               setProposedValue(e.target.value);
                               if (e.target.value.trim()) clearFieldError('proposed_value');
                           }}/>
                    {errorFor('proposed_value')}
                </div>
                <div className="mb-3">
                    <label htmlFor="correction-evidence-url" className="form-label">Evidence link</label>
                    <input type="url" id="correction-evidence-url" className={`form-control${invalid('evidence_url')}`}
                           data-testid="correction-evidence-url" data-field="evidence_url"
                           value={evidenceUrl} autoComplete="off"
                           aria-required="true" aria-invalid={!!fieldErrors.evidence_url}
                           aria-describedby={[describedBy('evidence_url'), 'correction-evidence-help']
                               .filter(Boolean).join(' ')}
                           onChange={e => {
                               setEvidenceUrl(e.target.value);
                               if (/^https:\/\/\S+/i.test(e.target.value.trim())) clearFieldError('evidence_url');
                           }}/>
                    {errorFor('evidence_url')}
                    <div id="correction-evidence-help" className="form-text">
                        A public page that shows the correct value. Must start with https://.
                        We never fetch this link.
                    </div>
                </div>
                <div className="mb-3">
                    <label htmlFor="correction-scope-level" className="form-label">Applies to</label>
                    <select id="correction-scope-level" className={`form-select${invalid('scope_level')}`}
                            data-testid="correction-scope-level" data-field="scope_level" value={scopeLevel}
                            aria-invalid={!!fieldErrors.scope_level}
                            aria-describedby={[describedBy('scope_level'), scopedBlocked ? 'correction-scope-help' : '']
                                .filter(Boolean).join(' ') || undefined}
                            onChange={e => {
                                setScopeLevel(e.target.value);
                                if (e.target.value === 'company') clearFieldError('scope_value');
                            }}>
                        {SCOPE_LEVELS.map(([value, label]) => (
                            <option key={value} value={value} disabled={scopedBlocked && value !== 'company'}>
                                {label}
                            </option>
                        ))}
                    </select>
                    {scopedBlocked && (
                        <div id="correction-scope-help" className="form-text" data-testid="correction-scope-help">
                            This field already has a company-wide fact, so staff can only apply a
                            whole-company change to it.
                        </div>
                    )}
                    {errorFor('scope_level')}
                </div>
                {scopeLevel !== 'company' && (
                    <div className="mb-3">
                        <label htmlFor="correction-scope-value" className="form-label">Which one?</label>
                        <input type="text" id="correction-scope-value" className={`form-control${invalid('scope_value')}`}
                               data-testid="correction-scope-value" data-field="scope_value"
                               value={scopeValue} maxLength={100} autoComplete="off"
                               aria-required="true" aria-invalid={!!fieldErrors.scope_value}
                               aria-describedby={describedBy('scope_value')}
                               onChange={e => {
                                   setScopeValue(e.target.value);
                                   if (e.target.value.trim()) clearFieldError('scope_value');
                               }}/>
                        {errorFor('scope_value')}
                    </div>
                )}
                <div className="mb-3">
                    <label htmlFor="correction-note" className="form-label">Note (optional)</label>
                    <textarea id="correction-note" className={`form-control${invalid('note')}`} rows={2} maxLength={500}
                              data-field="note" value={note} aria-invalid={!!fieldErrors.note}
                              aria-describedby={describedBy('note')}
                              onChange={e => setNote(e.target.value)}/>
                    {errorFor('note')}
                </div>
            </form>
        );
    }

    const showForm = !authRequired && !saved && !duplicate;
    return createPortal(
        <div ref={dialogRef} className="modal d-block blocking-modal" tabIndex={-1} role="dialog"
             aria-modal="true" aria-labelledby="correction-title correction-company" data-testid="company-correction-modal">
            <div className="visually-hidden" role="status" aria-live="polite"
                 data-testid="correction-live">{announcement}</div>
            <div className="modal-dialog" role="document">
                <div className="modal-content">
                    <div className="modal-header">
                        <div className="correction-heading">
                            <h5 className="modal-title" id="correction-title">Suggest a correction</h5>
                            <p className="mb-0 small text-body-secondary text-break" id="correction-company">
                                {companyName}
                            </p>
                        </div>
                        <button ref={closeButtonRef} type="button" className="btn-close" aria-label="Close"
                                onClick={onClose} data-testid="correction-close"></button>
                    </div>
                    <div className="modal-body">{body}</div>
                    {showForm && (
                        <div className="modal-footer correction-footer">
                            {error && (
                                <div ref={errorRef} tabIndex={-1}
                                     className={`alert ${rateLimited ? 'alert-warning' : 'alert-danger'} w-100 py-2 mb-0 small`}
                                     role="alert" data-testid="correction-error">{error}</div>
                            )}
                            <button type="button" className="btn btn-secondary" onClick={onClose}>Cancel</button>
                            <button type="submit" form="correction-form" className="btn btn-primary"
                                    disabled={retryAt !== null}
                                    aria-disabled={submitting || !!pendingForField || undefined}
                                    aria-describedby={pendingForField ? 'correction-pending-notice' : undefined}
                                    data-testid="correction-submit">
                                {submitting ? 'Submitting…' : 'Submit suggestion'}
                            </button>
                        </div>
                    )}
                    {showBack && (
                        <div className="modal-footer">
                            <button ref={backButtonRef} type="button" className="btn btn-primary" onClick={handleBack}
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
