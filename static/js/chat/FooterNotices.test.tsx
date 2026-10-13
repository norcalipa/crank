// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {act, fireEvent, render, screen} from '@testing-library/react';
import * as React from 'react';

import {FooterNotices} from './FooterNotices';
import type {FooterNoticesProps} from './FooterNotices';
import type {AssistantStatus} from './types';

const unavailable = {state: 'temporarily_unavailable', actions: []} as unknown as AssistantStatus;
const ready = {state: 'ready', actions: []} as unknown as AssistantStatus;
const longError = 'x'.repeat(121);

// jsdom has no layout: the row's text is "cut" when the test says its content is taller than its box,
// and the resize observer is driven by hand.
let cutText = false;
let observers: Array<() => void> = [];
const heightOf = jest.spyOn(HTMLElement.prototype, 'scrollHeight', 'get');
const boxOf = jest.spyOn(HTMLElement.prototype, 'clientHeight', 'get');
const measureCalls = jest.fn();
const resize = () => act(() => { observers.forEach((callback) => callback()); });

beforeEach(() => {
    cutText = false;
    observers = [];
    measureCalls.mockClear();
    heightOf.mockImplementation(function (this: HTMLElement) {
        measureCalls();
        return this.classList.contains('chat-error-text') && cutText ? 60 : 30;
    });
    boxOf.mockImplementation(() => 30);
    (window as unknown as {ResizeObserver: unknown}).ResizeObserver = class {
        constructor(callback: () => void) { observers.push(callback); }
        observe() {}
        disconnect() {}
    };
});

afterAll(() => {
    heightOf.mockRestore();
    boxOf.mockRestore();
    delete (window as unknown as {ResizeObserver?: unknown}).ResizeObserver;
});

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
        // The compact row is the only mode until the reader asks for more; it has no toggle.
        expect(notices()).toHaveClass('is-collapsed');
        expect(screen.queryByTestId('chat-notices-details')).not.toBeInTheDocument();
        expect(screen.getByTestId('chat-error')).toHaveTextContent('Could not send.');
    });

    test('a status notice alone is not collapsible', () => {
        setup({status: unavailable});
        expect(screen.getByTestId('assistant-status-notice')).toBeInTheDocument();
        expect(notices()).not.toHaveClass('is-collapsible');
        expect(screen.queryByTestId('chat-notices-details')).not.toBeInTheDocument();
    });

    test('Details exists only when the text is cut at this width, whatever its length', () => {
        const {rerender} = setup({error: longError});
        expect(screen.queryByTestId('chat-notices-details')).not.toBeInTheDocument();
        cutText = true;
        rerender({error: `${longError}!`});
        expect(screen.getByTestId('chat-notices-details')).toBeInTheDocument();
        expect(notices()).toHaveClass('is-collapsed');
    });

    test('a failed send whose text fits shows it whole, with Retry and no Details', () => {
        setup({error: 'Could not send.', retrying: true});
        expect(notices()).toHaveClass('is-collapsed');
        expect(notices()).toHaveClass('has-retry');
        expect(screen.getByTestId('retry-button')).toBeInTheDocument();
        expect(screen.queryByTestId('chat-notices-details')).not.toBeInTheDocument();
    });

    test('measures again when the row\'s width changes, and drops the toggle when the text no longer is cut', () => {
        cutText = true;
        setup({error: longError});
        expect(screen.getByTestId('chat-notices-details')).toBeInTheDocument();
        cutText = false;
        resize();
        expect(screen.queryByTestId('chat-notices-details')).not.toBeInTheDocument();
        cutText = true;
        resize();
        expect(screen.getByTestId('chat-notices-details')).toBeInTheDocument();
    });

    test('a fixed toggle (a status notice) is kept while the text fits, and without ResizeObserver the text is measured once', () => {
        delete (window as unknown as {ResizeObserver?: unknown}).ResizeObserver;
        setup({status: unavailable, error: 'Could not send.'});
        expect(screen.getByTestId('chat-notices-details')).toBeInTheDocument();
    });

    test('the status notice leads the collapsed row with its own title, which exists once', () => {
        setup({status: unavailable, error: 'Could not send.'});
        expect(notices()).toHaveClass('has-status');
        expect(screen.getAllByText('Assistant temporarily unavailable')).toHaveLength(1);
        expect(screen.getByText('Assistant temporarily unavailable')).toHaveClass('assistant-status-title');
        expect(screen.getAllByRole('alert').filter((el) => el.getAttribute('data-testid') === 'chat-error')).toHaveLength(1);
    });

    test('the toggle is the last row of the area and the same element in both states, the error before the status', () => {
        setup({status: unavailable, error: 'Could not send.'});
        const details = screen.getByTestId('chat-notices-details');
        const order = () => Array.from(notices().children).map((el) => el.className.split(' ')[0]);
        expect(order()).toEqual(['chat-notices-body', 'btn']);
        const body = notices().querySelector('.chat-notices-body') as HTMLElement;
        expect(Array.from(body.children).map((el) => el.getAttribute('data-testid'))).toEqual(['chat-error', 'assistant-status-notice']);
        fireEvent.click(details);
        expect(screen.getByTestId('chat-notices-details')).toBe(details);
        expect(order()).toEqual(['chat-notices-body', 'btn']);
        expect(details.parentElement).toBe(notices());
    });

    test('a status notice plus an error collapses behind Details', () => {
        setup({status: unavailable, error: 'Could not send.'});
        expect(notices()).toHaveClass('is-collapsed');
        const details = screen.getByTestId('chat-notices-details');
        expect(details).toHaveTextContent('Details');
        expect(details).toHaveAttribute('aria-expanded', 'false');
        expect(details).toHaveAttribute('aria-controls', notices().id);
    });

    test('a status that renders nothing does not collapse a short error', () => {
        setup({status: ready, error: 'Could not send.'});
        expect(notices()).not.toHaveClass('is-collapsible');
    });

    test('Details expands and Hide details collapses, keeping focus on the button', () => {
        cutText = true;
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
        cutText = true;
        const {rerender} = setup({error: longError});
        fireEvent.click(screen.getByTestId('chat-notices-details'));
        expect(notices()).not.toHaveClass('is-collapsed');
        rerender({error: `${longError} again`});
        expect(notices()).toHaveClass('is-collapsed');
        expect(screen.getByTestId('chat-notices-details')).toHaveAttribute('aria-expanded', 'false');
    });

    test('the same error text is not re-collapsed by an unrelated render', () => {
        cutText = true;
        const {rerender} = setup({error: longError});
        fireEvent.click(screen.getByTestId('chat-notices-details'));
        rerender({pending: true});
        expect(notices()).not.toHaveClass('is-collapsed');
    });

    test('the error is one alert with its whole text in both modes', () => {
        cutText = true;
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
        cutText = true;
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
