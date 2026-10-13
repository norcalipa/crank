// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {fireEvent, render, screen} from '@testing-library/react';
import * as React from 'react';

import {FooterNotices, LONG_ERROR_CHARS} from './FooterNotices';
import type {FooterNoticesProps} from './FooterNotices';
import type {AssistantStatus} from './types';

const unavailable = {state: 'temporarily_unavailable', actions: []} as unknown as AssistantStatus;
const ready = {state: 'ready', actions: []} as unknown as AssistantStatus;
const longError = 'x'.repeat(LONG_ERROR_CHARS + 1);

function setup(extra: Partial<FooterNoticesProps> = {}) {
    const props: FooterNoticesProps = {
        status: null, statusChecking: false, onStatusRetry: jest.fn(), error: null, errorType: null,
        retrying: false, pending: false, onRetry: jest.fn(), ...extra,
    };
    const view = render(<FooterNotices {...props}/>);
    return {props, view, rerender: (more: Partial<FooterNoticesProps>) => view.rerender(<FooterNotices {...props} {...more}/>)};
}

const notices = () => screen.getByTestId('chat-footer-notices');

describe('FooterNotices (issue #483)', () => {
    test('one short error is not collapsible and has no Details button', () => {
        setup({error: 'Could not send.'});
        expect(notices()).not.toHaveClass('is-collapsible');
        expect(notices()).not.toHaveClass('is-collapsed');
        expect(screen.queryByTestId('chat-notices-details')).not.toBeInTheDocument();
        expect(screen.getByTestId('chat-error')).toHaveTextContent('Could not send.');
    });

    test('a status notice alone is not collapsible', () => {
        setup({status: unavailable});
        expect(screen.getByTestId('assistant-status-notice')).toBeInTheDocument();
        expect(notices()).not.toHaveClass('is-collapsible');
        expect(screen.queryByTestId('chat-notices-details')).not.toBeInTheDocument();
    });

    test('an error exactly at the limit is not collapsible; one character more is', () => {
        const {rerender} = setup({error: 'x'.repeat(LONG_ERROR_CHARS)});
        expect(notices()).not.toHaveClass('is-collapsible');
        rerender({error: longError});
        expect(notices()).toHaveClass('is-collapsed');
    });

    test('a status notice plus an error collapses behind Details', () => {
        setup({status: unavailable, error: 'Could not send.'});
        expect(notices()).toHaveClass('is-collapsed');
        const details = screen.getByTestId('chat-notices-details');
        expect(details).toHaveTextContent('Details');
        expect(details).toHaveAttribute('aria-expanded', 'false');
        expect(details).toHaveAttribute('aria-controls', notices().id);
    });

    test('the error of a failed turn collapses, since its card in the transcript has the same Retry', () => {
        setup({error: 'Could not send.', retrying: true});
        expect(notices()).toHaveClass('is-collapsed');
        expect(screen.getByTestId('retry-button')).toBeInTheDocument();
        expect(screen.getByTestId('chat-notices-details')).toHaveAttribute('aria-expanded', 'false');
    });

    test('a status that renders nothing does not collapse a short error', () => {
        setup({status: ready, error: 'Could not send.'});
        expect(notices()).not.toHaveClass('is-collapsible');
    });

    test('Details expands and Hide details collapses, keeping focus on the button', () => {
        setup({error: longError});
        const details = screen.getByTestId('chat-notices-details');
        details.focus();
        fireEvent.click(details);
        expect(notices()).not.toHaveClass('is-collapsed');
        expect(details).toHaveTextContent('Hide details');
        expect(details).toHaveAttribute('aria-expanded', 'true');
        expect(details).toHaveFocus();
        fireEvent.click(details);
        expect(notices()).toHaveClass('is-collapsed');
        expect(details).toHaveTextContent('Details');
    });

    test('a new error starts collapsed', () => {
        const {rerender} = setup({error: longError});
        fireEvent.click(screen.getByTestId('chat-notices-details'));
        expect(notices()).not.toHaveClass('is-collapsed');
        rerender({error: `${longError} again`});
        expect(notices()).toHaveClass('is-collapsed');
        expect(screen.getByTestId('chat-notices-details')).toHaveAttribute('aria-expanded', 'false');
    });

    test('the same error text is not re-collapsed by an unrelated render', () => {
        const {rerender} = setup({error: longError});
        fireEvent.click(screen.getByTestId('chat-notices-details'));
        rerender({pending: true});
        expect(notices()).not.toHaveClass('is-collapsed');
    });

    test('the error is one alert with its whole text in both modes', () => {
        setup({status: unavailable, error: longError});
        for (const expected of ['false', 'true']) {
            const alerts = screen.getAllByRole('alert', {hidden: true}).filter((el) => el.getAttribute('data-testid') === 'chat-error');
            expect(alerts).toHaveLength(1);
            expect(alerts[0]).toHaveTextContent(longError);
            expect(screen.getAllByText(longError)).toHaveLength(1);
            expect(screen.getByTestId('chat-notices-details')).toHaveAttribute('aria-expanded', expected);
            fireEvent.click(screen.getByTestId('chat-notices-details'));
        }
    });

    test('Retry works collapsed and expanded, and is disabled while a turn is pending', () => {
        const {props, rerender} = setup({error: longError, retrying: true});
        fireEvent.click(screen.getByTestId('retry-button'));
        fireEvent.click(screen.getByTestId('chat-notices-details'));
        fireEvent.click(screen.getByTestId('retry-button'));
        expect(props.onRetry).toHaveBeenCalledTimes(2);
        rerender({pending: true});
        expect(screen.getByTestId('retry-button')).toBeDisabled();
    });

    test('the status notice\'s own retry reports to the caller and the error type is exposed', () => {
        const {props} = setup({status: unavailable, errorType: 'provider_error', error: null});
        fireEvent.click(screen.getByTestId('assistant-status-retry'));
        expect(props.onStatusRetry).toHaveBeenCalled();
        setup({error: 'Nope', errorType: 'provider_error'});
        expect(screen.getAllByTestId('chat-error').pop()).toHaveAttribute('data-error-type', 'provider_error');
    });
});
