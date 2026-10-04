// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent} from '@testing-library/react';
import * as React from 'react';

import {JumpToLatest} from './JumpToLatest';

describe('JumpToLatest', () => {
    test('without unread replies it offers a plain jump and no count', () => {
        const onJump = jest.fn();
        render(<JumpToLatest unreadCount={0} onJump={onJump}/>);
        const button = screen.getByRole('button', {name: 'Jump to latest message'});
        expect(button).not.toHaveTextContent(/new/);
        fireEvent.click(button);
        expect(onJump).toHaveBeenCalledTimes(1);
    });

    test.each([
        [1, 'Jump to latest message, 1 new message'],
        [3, 'Jump to latest message, 3 new messages'],
    ])('shows the unread count as a badge and in the name (%i)', (count, name) => {
        render(<JumpToLatest unreadCount={count} onJump={jest.fn()}/>);
        expect(screen.getByRole('button', {name})).toHaveTextContent(`${count} new`);
    });
});
