// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, fireEvent, act} from '@testing-library/react';
import * as React from 'react';

import {Composer} from './Composer';

const setup = () => {
    const onEnter = jest.fn();
    const ref = React.createRef<HTMLTextAreaElement>();
    render(
        <Composer
            textareaRef={ref}
            value="hello"
            pending={false}
            authenticated
            textareaDisabled={false}
            sendDisabled={false}
            onChange={jest.fn()}
            onSubmit={jest.fn()}
            onEnter={onEnter}
            onStop={jest.fn()}
        />,
    );
    return {onEnter, textarea: screen.getByRole('textbox', {name: 'Message'})};
};

describe('Composer Enter handling', () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(() => jest.useRealTimers());

    test('plain Enter sends; Shift+Enter does not', () => {
        const {onEnter, textarea} = setup();
        fireEvent.keyDown(textarea, {key: 'Enter', shiftKey: true});
        expect(onEnter).not.toHaveBeenCalled();
        fireEvent.keyDown(textarea, {key: 'Enter'});
        expect(onEnter).toHaveBeenCalledTimes(1);
    });

    test('Enter during composition (isComposing or keyCode 229) never sends', () => {
        const {onEnter, textarea} = setup();
        fireEvent.keyDown(textarea, {key: 'Enter', isComposing: true});
        fireEvent.keyDown(textarea, {key: 'Enter', keyCode: 229});
        expect(onEnter).not.toHaveBeenCalled();
    });

    test('Enter while a composition session is open never sends, even without isComposing', () => {
        const {onEnter, textarea} = setup();
        fireEvent.compositionStart(textarea);
        fireEvent.keyDown(textarea, {key: 'Enter'});
        expect(onEnter).not.toHaveBeenCalled();
    });

    test('the Enter that confirms a candidate right after compositionend (Safari order) does not send, the next one does', () => {
        const {onEnter, textarea} = setup();
        fireEvent.compositionStart(textarea);
        fireEvent.compositionEnd(textarea);
        fireEvent.keyDown(textarea, {key: 'Enter'});
        expect(onEnter).not.toHaveBeenCalled();
        act(() => { jest.runAllTimers(); });
        fireEvent.keyDown(textarea, {key: 'Enter'});
        expect(onEnter).toHaveBeenCalledTimes(1);
    });

    test('a new composition cancels a pending settle so Enter stays blocked', () => {
        const {onEnter, textarea} = setup();
        fireEvent.compositionStart(textarea);
        fireEvent.compositionEnd(textarea);
        fireEvent.compositionStart(textarea);
        act(() => { jest.runAllTimers(); });
        fireEvent.keyDown(textarea, {key: 'Enter'});
        expect(onEnter).not.toHaveBeenCalled();
    });
});
