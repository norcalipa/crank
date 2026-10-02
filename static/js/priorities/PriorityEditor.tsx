// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Inline, non-modal priorities editor (issue #480). Controls are chosen from
// the server's field metadata (type, choices, editable, supported) so the
// schema lives in one place. The editor only mounts as the result of a user
// action, so it takes focus on mount; nothing else autofocuses.

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
    onUndoClear?: (path: string) => void;
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

// One entry per item: an entry is saved exactly as typed, so a comma inside it
// ("San Francisco, CA") stays part of that entry.
function ListEditor({items, inputId, describedBy, invalid, label, onChange}: {
    items: string[];
    inputId: string;
    describedBy?: string;
    invalid: boolean;
    label: string;
    onChange: (value: string[]) => void;
}) {
    const [text, setText] = React.useState('');
    const commit = () => {
        const entry = text.trim();
        setText('');
        if (entry !== '' && !items.includes(entry)) {
            onChange([...items, entry]);
        }
    };
    return (
        <div className="priority-list-editor">
            {items.length > 0 && (
                <ul className="priority-list-items" aria-label={`${label} entries`}>
                    {items.map((item) => (
                        <li key={item} className="priority-list-item">
                            <span className="priority-list-text">{item}</span>
                            <button type="button" className="priority-list-remove"
                                    aria-label={`Remove ${item} from ${label}`}
                                    onClick={() => onChange(items.filter((other) => other !== item))}>
                                <i className="fa-solid fa-xmark" aria-hidden="true"></i>
                            </button>
                        </li>
                    ))}
                </ul>
            )}
            <div className="input-group input-group-sm">
                <input id={inputId} type="text" aria-describedby={describedBy} aria-invalid={invalid || undefined}
                       className={`form-control form-control-sm${invalid ? ' is-invalid' : ''}`}
                       placeholder="Add an entry" value={text}
                       onChange={(e) => setText(e.target.value)} onBlur={commit}
                       onKeyDown={(e) => {
                           if (e.key === 'Enter') {
                               e.preventDefault();
                               commit();
                           }
                       }}/>
                <button type="button" className="btn btn-outline-secondary" onMouseDown={(e) => e.preventDefault()}
                        onClick={commit} disabled={text.trim() === ''}>Add</button>
            </div>
        </div>
    );
}

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
        // Not set / Yes / No: unchecking never leaves a stray explicit "No".
        // An always-a-requirement field only means something when "Yes".
        return (
            <select className={`form-select form-select-sm${invalid ? ' is-invalid' : ''}`} {...common}
                    value={String(value)} onChange={(e) => onChange(e.target.value)}>
                <option value="">Not set</option>
                <option value="true">Yes</option>
                {(!field.hard_locked || value === 'false') && <option value="false">No</option>}
            </select>
        );
    }
    if (field.type === 'str_list' && !field.choices) {
        return (
            <ListEditor items={Array.isArray(value) ? value : []} inputId={inputId} describedBy={describedBy}
                        invalid={invalid} label={field.label} onChange={onChange}/>
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
               placeholder="Not set"
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
    const hasValue = field.type === 'bool'
        ? value === 'true'
        : Array.isArray(value) ? value.length > 0 : value !== '' && value !== null && value !== undefined;
    const message = errors.join(' ')
        .replace(/^Field '[^']+' must be non-negative$/, `${field.label} can\u2019t be negative.`)
        .replace(/^Field '[^']+' /, `${field.label} `);
    const control = (
        <FieldControl field={field} value={value} inputId={inputId} describedBy={describedBy}
                      invalid={errors.length > 0} currency={currency}
                      onChange={(v) => onChange(field.path, v)}/>
    );
    const label = (
        <label id={`${inputId}-label`} htmlFor={inputId} className="priorities-label">
            {field.label}
        </label>
    );
    return (
        <div className="priorities-field" data-testid="priorities-field">
            {label}{control}
            {errors.length > 0 && (
                <div id={`${inputId}-error`} className="invalid-feedback d-block priorities-field-error">
                    {message}
                </div>
            )}
            {field.supported && (hasValue || hard) && (
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
    onChange, onToggleHard, onUndoClear = () => undefined, onReview, onCancel, idPrefix = 'priority',
}: PriorityEditorProps) {
    const formRef = React.useRef<HTMLFormElement>(null);
    React.useEffect(() => {
        formRef.current?.focus();
    }, []);
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
    // The server's `editable` flag decides what can be changed here; the rest is shown with a Clear action.
    const supported = visible.filter((f) => f.supported && f.editable);
    const unsupported = visible.filter((f) => !f.supported || !f.editable);
    const unsupportedEditable = unsupported.filter((f) => f.editable);
    const unsupportedReadOnly = unsupported.filter((f) => !f.editable);
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
        <form className="priorities-editor" aria-label="Edit priorities" noValidate ref={formRef} tabIndex={-1}
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
                <details className="priorities-unsupported" open={unsupported.some((f) => draft.values[f.path] !== undefined) || undefined}>
                    <summary>Not used for matching yet ({unsupported.length})</summary>
                    {unsupportedEditable.map((field) => (
                        <FieldRow key={field.path} field={{...field, supported: false}} inputId={idFor(field)} {...rowProps}/>
                    ))}
                    {unsupportedReadOnly.map((field) => {
                        const cleared = draft.values[field.path] !== undefined;
                        return (
                            <div key={field.path} className="priorities-readonly-row" data-testid="priorities-field">
                                <span id={`${idFor(field)}-label`} className="priorities-label">{field.label}</span>
                                <span id={idFor(field)} className={`priorities-readonly${field.set && !cleared ? ' is-set' : ''}`}>
                                    {field.set && !cleared ? preferenceValueLabel(field.value, field.path, currencyCode) : cleared ? 'Will be cleared' : 'Not set'}
                                </span>
                                {field.set && (
                                    <button type="button" className="btn btn-sm btn-link priorities-clear"
                                            aria-label={cleared ? `Keep ${field.label}` : `Clear ${field.label}`}
                                            onClick={() => cleared
                                                ? onUndoClear(field.path)
                                                : onChange(field.path, field.type === 'str_list' ? [] : '')}>
                                        {cleared ? 'Keep' : 'Clear'}
                                    </button>
                                )}
                            </div>
                        );
                    })}
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
