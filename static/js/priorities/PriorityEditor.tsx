// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Inline, non-modal priorities editor (issue #480). Controls are chosen from
// the server's field metadata (type, choices, editable, supported) so the
// schema lives in one place. Nothing autofocuses on mount.

import * as React from 'react';
import type {EditorField} from './api';
import {Draft, DraftValue, toDraftValue} from './patch';
import {humanizeToken, preferenceValueLabel} from './format';

export interface PriorityEditorProps {
    fields: EditorField[];
    draft: Draft;
    fieldErrors: Record<string, string[]>;
    dirty: boolean;
    pending?: boolean;
    formError?: string | null;
    onChange: (path: string, value: DraftValue) => void;
    onToggleHard: (path: string, hard: boolean) => void;
    onReview: () => void;
    onCancel: () => void;
    idPrefix?: string;
}

function groupOf(path: string): string {
    const head = path.split('.')[0].replace(/_/g, ' ');
    return head.charAt(0).toUpperCase() + head.slice(1);
}

const MONEY_PATHS = ['compensation.minimum_salary', 'compensation.minimum_total_compensation'];
const CURRENCY_PATH = 'compensation.currency';

function FieldControl({field, value, inputId, describedBy, invalid, currency, onChange}: {
    field: EditorField;
    value: DraftValue;
    inputId: string;
    describedBy?: string;
    invalid: boolean;
    currency: string;
    onChange: (value: DraftValue) => void;
}) {
    const common = {id: inputId, 'aria-describedby': describedBy, 'aria-invalid': invalid || undefined};
    if (field.type === 'bool') {
        return (
            <input type="checkbox" className={`form-check-input${invalid ? ' is-invalid' : ''}`} {...common}
                   checked={value === true} onChange={(e) => onChange(e.target.checked)}/>
        );
    }
    if (field.type === 'str_list' && field.choices) {
        const chosen = Array.isArray(value) ? value : [];
        return (
            <div role="group" aria-labelledby={`${inputId}-label`} className="priority-choices">
                {field.choices.map((choice) => (
                    <label key={choice} className="form-check-label priority-choice">
                        <input type="checkbox" className="form-check-input"
                               checked={chosen.includes(choice)}
                               onChange={(e) => onChange(
                                   e.target.checked ? [...chosen, choice] : chosen.filter((c) => c !== choice),
                               )}/>
                        {humanizeToken(choice)}
                    </label>
                ))}
            </div>
        );
    }
    if (field.choices) {
        return (
            <select className={`form-select form-select-sm${invalid ? ' is-invalid' : ''}`} {...common}
                    value={String(value)} onChange={(e) => onChange(e.target.value)}>
                <option value="">Not set</option>
                {field.choices.map((choice) => <option key={choice} value={choice}>{humanizeToken(choice)}</option>)}
            </select>
        );
    }
    const numeric = field.type === 'int' || field.type === 'float';
    const input = (
        <input className={`form-control form-control-sm${invalid ? ' is-invalid' : ''}`} {...common}
               type={numeric ? 'number' : 'text'} inputMode={numeric ? 'decimal' : undefined}
               step={field.type === 'float' ? 'any' : undefined}
               placeholder={field.type === 'str_list' ? 'Comma-separated' : 'Not set'}
               value={String(value)} onChange={(e) => onChange(e.target.value)}/>
    );
    if (!MONEY_PATHS.includes(field.path)) return input;
    return (
        <div className="input-group input-group-sm has-validation">
            <span className="input-group-text">{currency}</span>
            {input}
        </div>
    );
}

function FieldRow({field, draft, fieldErrors, inputId, onChange, onToggleHard, currency}: {
    field: EditorField;
    draft: Draft;
    fieldErrors: Record<string, string[]>;
    inputId: string;
    currency: string;
    onChange: (path: string, value: DraftValue) => void;
    onToggleHard: (path: string, hard: boolean) => void;
}) {
    const errors = fieldErrors[field.path] || [];
    const value = draft.values[field.path] ?? toDraftValue(field);
    const hard = draft.hard[field.path] ?? field.hard;
    const describedBy = errors.length ? `${inputId}-error` : undefined;
    const isBool = field.type === 'bool';
    const hasValue = Array.isArray(value) ? value.length > 0 : value !== '' && value !== null && value !== undefined;
    const message = errors.join(' ')
        .replace(/^Field '[^']+' must be non-negative$/, `${field.label} can\u2019t be negative.`)
        .replace(/^Field '[^']+' /, `${field.label} `);
    const control = (
        <FieldControl field={field} value={value} inputId={inputId} describedBy={describedBy}
                      invalid={errors.length > 0} currency={currency}
                      onChange={(v) => onChange(field.path, v)}/>
    );
    const label = (
        <label id={`${inputId}-label`} htmlFor={inputId}
               className={isBool ? 'form-check-label priorities-label' : 'priorities-label'}>
            {field.label}
        </label>
    );
    return (
        <div className="priorities-field" data-testid="priorities-field">
            {isBool ? (
                <div className="form-check">
                    {control}{label}
                    {field.hard_locked && <div className="form-text priorities-field-note mt-0">Always a requirement</div>}
                </div>
            ) : <>{label}{control}</>}
            {errors.length > 0 && (
                <div id={`${inputId}-error`} className="invalid-feedback d-block priorities-field-error">
                    {message}
                </div>
            )}
            {field.supported && ((isBool ? value === true : hasValue) || hard) && !(isBool && field.hard_locked) && (
                field.hard_locked ? (
                    <span className="priorities-field-note">Always a requirement</span>
                ) : (
                    <div role="group" aria-label={`${field.label} importance`} className="priorities-importance">
                        <button type="button" className={`btn btn-sm priorities-toggle${!hard ? ' active' : ''}`}
                                aria-pressed={!hard} onClick={() => onToggleHard(field.path, false)}>
                            Preference
                        </button>
                        <button type="button" className={`btn btn-sm priorities-toggle${hard ? ' active' : ''}`}
                                aria-pressed={hard} onClick={() => onToggleHard(field.path, true)}>
                            Requirement
                        </button>
                    </div>
                )
            )}
        </div>
    );
}

export default function PriorityEditor({
    fields, draft, fieldErrors, dirty, pending = false, formError = null,
    onChange, onToggleHard, onReview, onCancel, idPrefix = 'priority',
}: PriorityEditorProps) {
    const formRef = React.useRef<HTMLFormElement>(null);
    // A failed submit brings the first invalid field into view and focuses it,
    // so the error is never left off-screen inside the scroll region.
    React.useEffect(() => {
        if (!formError) return;
        const frame = window.requestAnimationFrame(() => {
            const first = formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]');
            first?.scrollIntoView({block: 'center'});
            first?.focus({preventScroll: true});
        });
        return () => window.cancelAnimationFrame(frame);
    }, [formError, fieldErrors]);

    const visible = fields.filter((f) => f.type !== 'float_map');
    const supported = visible.filter((f) => f.supported);
    const unsupported = visible.filter((f) => !f.supported);
    const groups = new Map<string, EditorField[]>();
    supported.forEach((field) => {
        const name = groupOf(field.path);
        groups.set(name, [...(groups.get(name) || []), field]);
    });
    const idFor = (field: EditorField) => `${idPrefix}-${field.path.replace(/\./g, '-')}`;
    const currencyDraft = draft.values[CURRENCY_PATH] ?? fields.find((f) => f.path === CURRENCY_PATH)?.value;
    const currencyCode = typeof currencyDraft === 'string' ? currencyDraft : '';
    const currency = currencyCode === '' || currencyCode === 'USD' ? '$' : currencyCode;
    const invalidLabels = visible.filter((f) => (fieldErrors[f.path] || []).length > 0).map((f) => f.label);
    const footerError = invalidLabels.length
        ? `Fix ${invalidLabels.length === 1 ? '1 field' : `${invalidLabels.length} fields`}: ${invalidLabels.join(', ')}`
        : formError;
    const rowProps = {draft, fieldErrors, currency, onChange, onToggleHard};
    return (
        <form className="priorities-editor" aria-label="Edit priorities" noValidate ref={formRef}
              onSubmit={(e) => { e.preventDefault(); onReview(); }}>
            {Array.from(groups.entries()).map(([name, group]) => (
                <fieldset key={name} className="priorities-group">
                    <legend className="priorities-legend">{name}</legend>
                    {group.map((field) => (
                        <FieldRow key={field.path} field={field} inputId={idFor(field)} {...rowProps}/>
                    ))}
                </fieldset>
            ))}
            {unsupported.length > 0 && (
                <details className="priorities-unsupported">
                    <summary>Not used for matching yet ({unsupported.length})</summary>
                    <p className="priorities-field-note">Ask the assistant to change these.</p>
                    {unsupported.map((field) => (
                        <div key={field.path} className="priorities-readonly-row" data-testid="priorities-field">
                            <span id={`${idFor(field)}-label`} className="priorities-label">{field.label}</span>
                            <span id={idFor(field)} className={`priorities-readonly${field.set ? ' is-set' : ''}`}>
                                {field.set ? preferenceValueLabel(field.value, field.path) : 'Not set'}
                            </span>
                        </div>
                    ))}
                </details>
            )}
            <div className="priorities-footer">
                {footerError && (
                    <div className="priorities-form-error" role="alert" data-testid="priorities-form-error">
                        <i className="fa-solid fa-circle-exclamation me-1" aria-hidden="true"></i>
                        {footerError}
                    </div>
                )}
                <div className="chat-actions" role="group" aria-label="Editor actions">
                    <button type="submit" className="btn btn-sm btn-primary"
                            disabled={!dirty || pending} aria-busy={pending}>
                        {pending ? 'Checking…' : 'Review changes'}
                    </button>
                    <button type="button" className="btn btn-sm btn-link text-light"
                            onClick={onCancel} disabled={pending}>Cancel</button>
                </div>
            </div>
        </form>
    );
}
