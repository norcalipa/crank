// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import * as React from 'react';
import SuggestCompanyModal from './SuggestCompanyModal';
import {lockBackground, unlockBackground} from './modalIsolation';

describe('SuggestCompanyModal', () => {
    beforeEach(() => {
        document.cookie = 'csrftoken=testtoken';
        global.fetch = jest.fn().mockImplementation(() => {
            return Promise.resolve({
                ok: true,
                json: () => Promise.resolve({id: 1, company_name: 'Test Co', status: 'pending'}),
            });
        });
    });

    afterEach(() => {
        jest.clearAllMocks();
    });

    test('renders nothing when not visible', () => {
        render(<SuggestCompanyModal visible={false} onClose={jest.fn()} />);
        expect(screen.queryByTestId('suggest-company-modal')).not.toBeInTheDocument();
    });

    test('renders form when visible', () => {
        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        expect(screen.getByTestId('suggest-company-modal')).toBeInTheDocument();
        expect(screen.getByLabelText(/Company name/)).toBeInTheDocument();
        expect(screen.getByLabelText(/Public website/)).toBeInTheDocument();
        expect(screen.getByLabelText(/Careers page/)).toBeInTheDocument();
        expect(screen.getByLabelText(/Why should CRank/)).toBeInTheDocument();
        expect(screen.getByTestId('suggest-submit-btn')).toBeInTheDocument();
    });

    test('submits form and shows success message', async () => {
        const onClose = jest.fn();
        render(<SuggestCompanyModal visible={true} onClose={onClose} />);

        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme Corp'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        await waitFor(() => {
            expect(screen.getByTestId('suggest-success')).toBeInTheDocument();
        });
        expect(screen.getByText(/review queue/)).toBeInTheDocument();
        expect(global.fetch).toHaveBeenCalledWith('/api/company-requests/', expect.objectContaining({
            method: 'POST',
        }));
    });

    test('shows field errors on validation failure', async () => {
        global.fetch = jest.fn().mockImplementation(() => {
            return Promise.resolve({
                ok: false,
                json: () => Promise.resolve({
                    error: 'Please correct the highlighted fields.',
                    field_errors: {company_name: ['Enter a company name.']},
                }),
            });
        });

        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        await waitFor(() => {
            expect(screen.getByTestId('suggest-error')).toHaveTextContent('Please correct');
        });
        expect(screen.getByText('Enter a company name.')).toBeInTheDocument();
    });

    test('shows network error on fetch failure', async () => {
        global.fetch = jest.fn().mockImplementation(() => {
            return Promise.reject(new Error('Network error'));
        });

        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        await waitFor(() => {
            expect(screen.getByTestId('suggest-error')).toHaveTextContent('Network error');
        });
    });

    test('close button resets form and calls onClose', () => {
        const onClose = jest.fn();
        render(<SuggestCompanyModal visible={true} onClose={onClose} />);
        fireEvent.click(screen.getByTestId('suggest-close-btn'));
        expect(onClose).toHaveBeenCalled();
    });

    test('cancel button resets and calls onClose', () => {
        const onClose = jest.fn();
        render(<SuggestCompanyModal visible={true} onClose={onClose} />);
        fireEvent.click(screen.getByText('Cancel'));
        expect(onClose).toHaveBeenCalled();
    });

    test('submit button is disabled while submitting', async () => {
        let resolveFetch: (value: any) => void;
        global.fetch = jest.fn().mockImplementation(() => {
            return new Promise(resolve => {
                resolveFetch = resolve;
            });
        });

        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Co'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://co.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        await waitFor(() => {
            expect(screen.getByTestId('suggest-submit-btn')).toBeDisabled();
            expect(screen.getByTestId('suggest-submit-btn')).toHaveTextContent('Submitting');
        });

        resolveFetch!({ok: true, json: () => Promise.resolve({id: 1})});

        await waitFor(() => {
            expect(screen.getByTestId('suggest-success')).toBeInTheDocument();
        });
    });

    describe('blocking dialog behavior (issue #464)', () => {
        test('renders with the blocking-modal class above navigation', () => {
            render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
            const modal = screen.getByTestId('suggest-company-modal');
            expect(modal).toHaveClass('blocking-modal');
        });

        test('moves focus to the close button when it opens', () => {
            const { rerender } = render(<SuggestCompanyModal visible={false} onClose={jest.fn()} />);
            rerender(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
            expect(screen.getByTestId('suggest-close-btn')).toHaveFocus();
        });

        test('Escape closes the modal and returns focus to the opener', () => {
            const onClose = jest.fn();
            const opener = document.createElement('button');
            document.body.appendChild(opener);
            opener.focus();

            const { rerender } = render(<SuggestCompanyModal visible={false} onClose={onClose} />);
            rerender(<SuggestCompanyModal visible={true} onClose={onClose} />);

            fireEvent.keyDown(document, {key: 'Escape'});

            expect(onClose).toHaveBeenCalledTimes(1);
            expect(document.activeElement).toBe(opener);

            document.body.removeChild(opener);
        });

        test('close button returns focus to the opener', () => {
            const onClose = jest.fn();
            const opener = document.createElement('button');
            document.body.appendChild(opener);
            opener.focus();

            const { rerender } = render(<SuggestCompanyModal visible={false} onClose={onClose} />);
            rerender(<SuggestCompanyModal visible={true} onClose={onClose} />);

            fireEvent.click(screen.getByTestId('suggest-close-btn'));

            expect(onClose).toHaveBeenCalledTimes(1);
            expect(document.activeElement).toBe(opener);

            document.body.removeChild(opener);
        });

        test('returns focus when the parent closes the modal while focus is inside it', () => {
            const onClose = jest.fn();
            const opener = document.createElement('button');
            document.body.appendChild(opener);
            opener.focus();

            const { rerender } = render(<SuggestCompanyModal visible={false} onClose={onClose} />);
            rerender(<SuggestCompanyModal visible={true} onClose={onClose} />);
            expect(screen.getByTestId('suggest-close-btn')).toHaveFocus();

            // Parent closes the modal directly (no handleClose call).
            rerender(<SuggestCompanyModal visible={false} onClose={onClose} />);

            expect(document.activeElement).toBe(opener);

            document.body.removeChild(opener);
        });

        test('Tab from the last focusable element wraps to the first', () => {
            render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);

            const modal = screen.getByTestId('suggest-company-modal');
            const focusables = Array.from(
                modal.querySelectorAll<HTMLElement>(
                    'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
                )
            );
            expect(focusables.length).toBeGreaterThan(1);
            const first = focusables[0];
            const last = focusables[focusables.length - 1];

            last.focus();
            fireEvent.keyDown(document, {key: 'Tab'});

            expect(document.activeElement).toBe(first);
        });

        test('Shift+Tab from the first focusable element wraps to the last', () => {
            render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);

            const modal = screen.getByTestId('suggest-company-modal');
            const focusables = Array.from(
                modal.querySelectorAll<HTMLElement>(
                    'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
                )
            );
            const first = focusables[0];
            const last = focusables[focusables.length - 1];

            first.focus();
            fireEvent.keyDown(document, {key: 'Tab', shiftKey: true});

            expect(document.activeElement).toBe(last);
        });

        test('Escape does nothing when the modal is not visible', () => {
            const onClose = jest.fn();
            render(<SuggestCompanyModal visible={false} onClose={onClose} />);
            fireEvent.keyDown(document, {key: 'Escape'});
            expect(onClose).not.toHaveBeenCalled();
        });
    });

    describe('background isolation (issue #464 review)', () => {
        let backgroundRoots: HTMLElement[];

        beforeEach(() => {
            // Stand up the page structure the isolation module targets.
            const shell = document.createElement('aside');
            shell.className = 'app-shell';
            document.body.appendChild(shell);
            const content = document.createElement('main');
            content.className = 'app-content';
            document.body.appendChild(content);
            backgroundRoots = [shell, content];
            document.body.style.overflow = 'auto';
        });

        afterEach(() => {
            for (const root of backgroundRoots) {
                root.remove();
            }
            document.body.style.overflow = '';
        });

        test('locks document scrolling and inert-hides the background while open', () => {
            const {rerender} = render(<SuggestCompanyModal visible={false} onClose={jest.fn()} />);
            rerender(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);

            expect(document.body.style.overflow).toBe('hidden');
            for (const root of backgroundRoots) {
                expect(root).toHaveAttribute('inert');
                expect(root).toHaveAttribute('aria-hidden', 'true');
            }
        });

        test('locks the background when mounted already open', () => {
            render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);

            expect(document.body.style.overflow).toBe('hidden');
            for (const root of backgroundRoots) {
                expect(root).toHaveAttribute('inert');
            }
        });

        test('releases the isolation when the modal closes', () => {
            const {rerender} = render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
            rerender(<SuggestCompanyModal visible={false} onClose={jest.fn()} />);

            expect(document.body.style.overflow).toBe('auto');
            for (const root of backgroundRoots) {
                expect(root).not.toHaveAttribute('inert');
                expect(root).not.toHaveAttribute('aria-hidden');
            }
        });

        test('releases isolation when unmounted while open', () => {
            const {unmount} = render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
            unmount();

            expect(document.body.style.overflow).toBe('auto');
            for (const root of backgroundRoots) {
                expect(root).not.toHaveAttribute('inert');
                expect(root).not.toHaveAttribute('aria-hidden');
            }
        });

        test('is reference-counted: a second holder keeps the background isolated', () => {
            // Simulate the mutual-exclusion handoff: another dialog claims
            // the isolation while this modal is open, so this modal's close
            // must not release it early.
            const {rerender} = render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
            expect(document.body.style.overflow).toBe('hidden');

            lockBackground();
            rerender(<SuggestCompanyModal visible={false} onClose={jest.fn()} />);

            // Still held by the second dialog.
            expect(document.body.style.overflow).toBe('hidden');
            for (const root of backgroundRoots) {
                expect(root).toHaveAttribute('inert');
            }

            unlockBackground();
            expect(document.body.style.overflow).toBe('auto');
            for (const root of backgroundRoots) {
                expect(root).not.toHaveAttribute('inert');
                expect(root).not.toHaveAttribute('aria-hidden');
            }
        });
    });
});
