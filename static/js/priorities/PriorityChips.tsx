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

const COLLAPSED_COUNT = 5;

export default function PriorityChips({chips, onEdit}: {chips: PriorityChip[]; onEdit: () => void}) {
    const [expanded, setExpanded] = React.useState(false);
    const hidden = chips.length - COLLAPSED_COUNT;
    const shown = expanded || hidden <= 0 ? chips : chips.slice(0, COLLAPSED_COUNT);
    const listId = `priority-chips-${React.useId()}`;
    return (
        <>
            <ul className="priority-chips" id={listId} aria-label="Your saved priorities">
                {shown.map((chip) => (
                    <li key={chip.path} data-testid="priority-chip"
                        className={`priority-chip${chip.hard ? ' priority-chip-hard' : ''}${chip.supported ? '' : ' priority-chip-unsupported'}`}>
                        <button type="button" className="priority-chip-button" onClick={onEdit}
                                aria-label={`Edit ${chip.label}`}>
                            {chip.hard && <i className="fa-solid fa-lock priority-chip-lock" aria-hidden="true"></i>}
                            <span className="priority-chip-label">{chip.label}:</span>
                            <span className="priority-chip-value">{chipValueLabel(chip.path, chip.display)}</span>
                            <i className="fa-solid fa-pen priority-chip-icon" aria-hidden="true"></i>
                        </button>
                        {chip.hard && <span className="visually-hidden">(required)</span>}
                        {!chip.supported && <span className="visually-hidden">(not used for matching yet)</span>}
                    </li>
                ))}
            </ul>
            {hidden > 0 && (
                <button type="button" className="btn btn-sm btn-link text-light priority-chips-more"
                        aria-expanded={expanded} aria-controls={listId}
                        onClick={() => setExpanded((open) => !open)}>
                    {expanded ? 'Show less' : `+${hidden} more`}
                </button>
            )}
            {chips.some((chip) => chip.hard || !chip.supported) && (
                <p className="priority-chip-legend" data-testid="priority-chip-legend">
                    <i className="fa-solid fa-lock" aria-hidden="true"></i> Requirement
                    <span className="priority-chip-legend-dashed" aria-hidden="true"></span> Not used for matching yet
                </p>
            )}
        </>
    );
}
