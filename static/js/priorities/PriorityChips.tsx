// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Priority chips (issue #480): one chip per saved criterion. A requirement is
// marked with an icon and the word "Required" (not colour alone); a criterion
// the matcher does not use yet is marked "Not used for matching yet".

import * as React from 'react';
import type {PriorityChip} from './api';
import {chipValueLabel} from './format';

export function PriorityChipsSkeleton({count = 3}: {count?: number}) {
    return (
        <ul className="priority-chips priority-chips-skeleton" aria-hidden="true" data-testid="priority-chips-skeleton">
            {Array.from({length: count}, (_, i) => (
                <li key={i} className="priority-chip priority-chip-skeleton"><span>&nbsp;</span></li>
            ))}
        </ul>
    );
}

function byImportance(a: PriorityChip, b: PriorityChip): number {
    return Number(!a.supported) - Number(!b.supported) || Number(b.hard) - Number(a.hard);
}

export default function PriorityChips({chips: unsorted, collapsedCount = 5, currency, readOnlyPaths, choicePaths, onEdit}: {
    chips: PriorityChip[];
    collapsedCount?: number;
    currency?: unknown;
    // Chips with no matching editor field are shown as plain text, not as edit buttons.
    readOnlyPaths?: ReadonlySet<string>;
    // Enumerated fields read as labels; free-text entries are shown exactly as saved.
    choicePaths?: ReadonlySet<string>;
    onEdit: () => void;
}) {
    const chips = React.useMemo(() => [...unsorted].sort(byImportance), [unsorted]);
    const [expanded, setExpanded] = React.useState(false);
    const hidden = chips.length - collapsedCount;
    const shown = expanded || hidden <= 0 ? chips : chips.slice(0, collapsedCount);
    const listId = `priority-chips-${React.useId()}`;
    return (
        <>
            <ul className="priority-chips" id={listId} aria-label="Your saved priorities">
                {shown.map((chip) => {
                    const value = chipValueLabel(chip.path, chip.display, currency, chip.items, choicePaths?.has(chip.path));
                    const status = [chip.hard ? 'requirement' : 'preference',
                        ...(chip.supported ? [] : ['not used for matching yet'])].join(', ');
                    const content = (
                        <>
                            {chip.hard && <i className="fa-solid fa-lock priority-chip-lock" aria-hidden="true"></i>}
                            <span className="priority-chip-text">
                                <span className="priority-chip-label">{chip.label}:</span>
                                <span className="priority-chip-value">{value}</span>
                            </span>
                        </>
                    );
                    return (
                        <li key={chip.path} data-testid="priority-chip"
                            className={`priority-chip${chip.hard ? ' priority-chip-hard' : ''}${chip.supported ? '' : ' priority-chip-unsupported'}`}>
                            {readOnlyPaths?.has(chip.path) ? (
                                <span className="priority-chip-button">
                                    {content}
                                    <span className="visually-hidden">, {status}</span>
                                </span>
                            ) : (
                                <button type="button" className="priority-chip-button" onClick={onEdit}>
                                    <span className="visually-hidden">Edit </span>
                                    {content}
                                    <span className="visually-hidden">, {status}</span>
                                    <i className="fa-solid fa-pen priority-chip-icon" aria-hidden="true"></i>
                                </button>
                            )}
                        </li>
                    );
                })}
            </ul>
            <div className="priority-chips-meta">
                {hidden > 0 && (
                    <button type="button" className="btn btn-sm btn-link text-light priority-chips-more"
                            aria-expanded={expanded} aria-controls={listId}
                            onClick={() => setExpanded((open) => !open)}>
                        {expanded ? 'Show less' : `+${hidden} more`}
                    </button>
                )}
                {chips.some((chip) => chip.hard || !chip.supported) && (
                    <p className="priority-chip-legend" data-testid="priority-chip-legend">
                        <span className="priority-chip-legend-item">
                            <i className="fa-solid fa-lock" aria-hidden="true"></i>Requirement
                        </span>
                        <span className="priority-chip-legend-item" title="Not used for matching yet">
                            <span className="priority-chip-legend-dashed" aria-hidden="true"></span>Not used yet
                        </span>
                    </p>
                )}
            </div>
        </>
    );
}
