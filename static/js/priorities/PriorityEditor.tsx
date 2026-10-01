// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Inline, non-modal priorities editor (issue #480). Controls are chosen from
// the server's field metadata (type, choices, editable, supported) so the
// schema lives in one place. Nothing autofocuses on mount.

import * as React from 'react';
import type {EditorField} from './api';
import {Draft, DraftValue, toDraftValue} from './patch';

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

function FieldControl({field, value, inputId, describedBy, invalid, onChange}: {
    field: EditorField;
    value: DraftValue;
    inputId: string;
    describedBy?: string;
    invalid: boolean;
    onChange: (value: DraftValue) => void;
}) {
    const common = {id: inputId, 'aria-describedby': describedBy, 'aria-invalid': invalid || undefined};
    if (field.type === 'bool') {
        return (
            <input type="checkbox" className="form-check-input" {...common}
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
                        {' '}{choice}
                    </label>
                ))}
            </div>
        );
    }
    if (field.choices) {
        return (
            <select className="form-select form-select-sm" {...common}
                    value={String(value)} onChange={(e) => onChange(e.target.value)}>
                <option value="">Not set</option>
                {field.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
            </select>
        );
    }
    const numeric = field.type === 'int' || field.type === 'float';
    return (
        <input className="form-control form-control-sm" {...common}
               type={numeric ? 'number' : 'text'} inputMode={numeric ? 'decimal' : undefined}
               step={field.type === 'float' ? 'any' : undefined}
               placeholder={field.type === 'str_list' ? 'Comma-separated' : 'Not set'}
               value={String(value)} onChange={(e) => onChange(e.target.value)}/>
    );
}

export default function PriorityEditor({
    fields, draft, fieldErrors, dirty, pending = false, formError = null,
    onChange, onToggleHard, onReview, onCancel, idPrefix = 'priority',
}: PriorityEditorProps) {
    const groups = new Map<string, EditorField[]>();
    fields.filter((f) => f.type !== 'float_map').forEach((field) => {
        const name = groupOf(field.path);
        groups.set(name, [...(groups.get(name) || []), field]);
    });
    return (
        <form className="priorities-editor" aria-label="Edit priorities" noValidate
              onSubmit={(e) => { e.preventDefault(); onReview(); }}>
            {formError && (
                <div className="pref-change-error" role="alert" data-testid="priorities-form-error">
                    <i className="fa-solid fa-triangle-exclamation me-1" aria-hidden="true"></i>
                    {formError}
                </div>
            )}
            {Array.from(groups.entries()).map(([name, group]) => (
                <fieldset key={name} className="priorities-group">
                    <legend className="priorities-legend">{name}</legend>
                    {group.map((field) => {
                        const inputId = `${idPrefix}-${field.path.replace(/\./g, '-')}`;
                        const errors = fieldErrors[field.path] || [];
                        const value = draft.values[field.path] ?? toDraftValue(field);
                        const hard = draft.hard[field.path] ?? field.hard;
                        const notes: string[] = [];
                        if (!field.supported) notes.push(`${inputId}-unsupported`);
                        if (errors.length) notes.push(`${inputId}-error`);
                        return (
                            <div key={field.path} className="priorities-field" data-testid="priorities-field">
                                <label id={`${inputId}-label`} htmlFor={inputId} className="priorities-label">
                                    {field.label}
                                </label>
                                {field.editable ? (
                                    <FieldControl field={field} value={value} inputId={inputId}
                                                  describedBy={notes.join(' ') || undefined}
                                                  invalid={errors.length > 0}
                                                  onChange={(v) => onChange(field.path, v)}/>
                                ) : (
                                    <span id={inputId} className="priorities-readonly">
                                        {field.set ? String(field.value) : 'Not set'}
                                        <span className="visually-hidden"> (edit this by chatting with the assistant)</span>
                                    </span>
                                )}
                                {errors.length > 0 && (
                                    <div id={`${inputId}-error`} className="priorities-field-error" role="alert">
                                        <i className="fa-solid fa-circle-exclamation me-1" aria-hidden="true"></i>
                                        {errors.join(' ')}
                                    </div>
                                )}
                                {!field.supported && (
                                    <div id={`${inputId}-unsupported`} className="priorities-field-note">
                                        Saved, but not used for matching yet
                                    </div>
                                )}
                                {field.supported && field.editable && (
                                    field.hard_locked ? (
                                        <span className="priorities-field-note">Always a requirement</span>
                                    ) : (
                                        <div role="group" aria-label={`${field.label} importance`}
                                             className="priorities-importance">
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
                    })}
                </fieldset>
            ))}
            <div className="chat-actions mt-2" role="group" aria-label="Editor actions">
                <button type="submit" className="chat-btn chat-btn-primary chat-focus"
                        disabled={!dirty || pending} aria-busy={pending}>
                    {pending ? 'Checking…' : 'Review changes'}
                </button>
                <button type="button" className="chat-btn chat-btn-secondary chat-focus"
                        onClick={onCancel} disabled={pending}>Cancel</button>
            </div>
        </form>
    );
}
