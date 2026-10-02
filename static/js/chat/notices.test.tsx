// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent} from '@testing-library/react';
import * as React from 'react';

import {PreferenceProposalNotice} from './notices';
import type {PreferenceProposal} from './types';

const proposal: PreferenceProposal = {
    id: 'p1',
    scope: 'account',
    changes: [],
    change_count: 0,
    base_revision: 1,
    unsupported_criteria: [],
    token: {patch: {}, scope: 'account', base_revision: 1},
};

describe('PreferenceProposalNotice', () => {
    test('the header dismiss control reports a dismiss decision', () => {
        const onDecision = jest.fn();
        render(<PreferenceProposalNotice proposal={proposal} state="idle" error={null} errorType={null} onDecision={onDecision}/>);
        fireEvent.click(screen.getByRole('button', {name: 'Dismiss preference proposal'}));
        expect(onDecision).toHaveBeenCalledWith('dismiss');
    });
});
