// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Priority chips (issue #480): one chip per saved criterion. A requirement is
// marked with an icon and the word "Required" (not colour alone); a criterion
// the matcher does not use yet is marked "Not used for matching yet".

import * as React from 'react';
import type {PriorityChip} from './api';

export function PriorityChipsSkeleton({count = 3}: {count?: number}) {
    return (
        <ul className="priority-chips priority-chips-skeleton" aria-hidden="true" data-testid="priority-chips-skeleton">
            {Array.from({length: count}, (_, i) => (
                <li key={i} className="priority-chip priority-chip-skeleton"><span>&nbsp;</span></li>
            ))}
        </ul>
    );
}

export default function PriorityChips({chips}: {chips: PriorityChip[]}) {
    return (
        <ul className="priority-chips" aria-label="Your saved priorities">
            {chips.map((chip) => (
                <li key={chip.path}
                    className={`priority-chip${chip.hard ? ' priority-chip-hard' : ''}${chip.supported ? '' : ' priority-chip-unsupported'}`}
                    data-testid="priority-chip">
                    {chip.hard && <i className="fa-solid fa-lock priority-chip-icon" aria-hidden="true"></i>}
                    <span className="priority-chip-label">{chip.label}:</span>{' '}
                    <span className="priority-chip-value">{chip.display}</span>
                    {chip.hard && <span className="visually-hidden"> (required)</span>}
                    {!chip.supported && <span className="visually-hidden"> (not used for matching yet)</span>}
                </li>
            ))}
        </ul>
    );
}
