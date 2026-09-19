// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
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

    test('context.companyName prefills the company-name input', () => {
        render(<SuggestCompanyModal visible={true} onClose={jest.fn()}
                                     context={{source: 'job_results', companyName: 'Acme Corp'}} />);
        expect((screen.getByLabelText(/Company name/) as HTMLInputElement).value).toBe('Acme Corp');
    });

    test('no context leaves the company-name input empty', () => {
        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        expect((screen.getByLabelText(/Company name/) as HTMLInputElement).value).toBe('');
    });

    test('prefills on reopen when context arrives with the same visible transition', () => {
        const { rerender } = render(
            <SuggestCompanyModal visible={false} onClose={jest.fn()} context={null} />
        );
        rerender(
            <SuggestCompanyModal visible={true} onClose={jest.fn()}
                                  context={{source: 'company_details', companyName: 'Beta Inc'}} />
        );
        expect((screen.getByLabelText(/Company name/) as HTMLInputElement).value).toBe('Beta Inc');
    });

    test('the pre-submission review notice is present in the form body (AC-9)', () => {
        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        expect(screen.getByTestId('suggest-review-notice')).toHaveTextContent(/review queue/);
    });

    test('submitted payload contains exactly company_name, website_url, careers_url, reason', async () => {
        render(<SuggestCompanyModal visible={true} onClose={jest.fn()}
                                     context={{source: 'rankings', searchTerm: 'acme', page: 2}} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme Corp'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        await waitFor(() => {
            expect(screen.getByTestId('suggest-success')).toBeInTheDocument();
        });
        const call = (global.fetch as jest.Mock).mock.calls[0];
        const body = JSON.parse(call[1].body);
        expect(Object.keys(body).sort()).toEqual(['careers_url', 'company_name', 'reason', 'website_url']);
        expect(body).toEqual({
            company_name: 'Acme Corp',
            website_url: 'https://acme.com',
            careers_url: '',
            reason: '',
        });
    });

    test('double submit in one tick calls fetch exactly once (AC-7)', async () => {
        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});

        const submitBtn = screen.getByTestId('suggest-submit-btn') as HTMLButtonElement;
        // Two native clicks inside a single `act` callback, rather than two
        // separate `fireEvent.click` calls (each of which flushes React's
        // state update — including `disabled` — before the next dispatch):
        // this reproduces the real race the synchronous `submitInFlight`
        // guard exists for (AC-7), where `disabled={submitting}` alone
        // would let both reach `fetch`.
        act(() => {
            submitBtn.click();
            submitBtn.click();
        });

        expect(global.fetch).toHaveBeenCalledTimes(1);

        await waitFor(() => {
            expect(screen.getByTestId('suggest-success')).toBeInTheDocument();
        });
    });

    test('a later submit after a completed one is not blocked by the in-flight guard', async () => {
        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));
        await waitFor(() => {
            expect(screen.getByTestId('suggest-success')).toBeInTheDocument();
        });

        fireEvent.click(screen.getByText('Close'));
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme 2'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme2.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        expect(global.fetch).toHaveBeenCalledTimes(2);
    });

    test('shows a sign-in message and link on a 401 response (AC-8, AC-12)', async () => {
        global.fetch = jest.fn().mockImplementation(() => {
            return Promise.resolve({
                ok: false,
                status: 401,
                json: () => Promise.resolve({error: 'Sign in to suggest a company.'}),
            });
        });

        render(<SuggestCompanyModal visible={true} onClose={jest.fn()}
                                     context={{source: 'assistant'}} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        await waitFor(() => {
            expect(screen.getByTestId('suggest-auth-required')).toBeInTheDocument();
        });
        expect(screen.getByText(/Sign in to suggest a company/)).toBeInTheDocument();
        expect(screen.getByTestId('suggest-sign-in-link')).toHaveAttribute('href', '/accounts/login/');
    });

    test('shows a duplicate-in-catalog message on a 409 with company_name field errors', async () => {
        global.fetch = jest.fn().mockImplementation(() => {
            return Promise.resolve({
                ok: false,
                status: 409,
                json: () => Promise.resolve({
                    error: 'This company is already in the catalog.',
                    field_errors: {company_name: ['An organization with this identity already exists.']},
                }),
            });
        });

        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        await waitFor(() => {
            expect(screen.getByTestId('suggest-error')).toHaveTextContent('already in the catalog');
        });
        expect(screen.getByText('An organization with this identity already exists.')).toBeInTheDocument();
    });

    test('shows a pending-duplicate-request message on a 409 with duplicate_request', async () => {
        global.fetch = jest.fn().mockImplementation(() => {
            return Promise.resolve({
                ok: false,
                status: 409,
                json: () => Promise.resolve({
                    error: 'A pending suggestion for this company already exists.',
                    duplicate_request: {id: 7, status: 'pending'},
                }),
            });
        });

        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        await waitFor(() => {
            expect(screen.getByTestId('suggest-error')).toHaveTextContent('pending suggestion for this company already exists');
        });
    });

    test('shows the rate-limit message on a 429 response', async () => {
        global.fetch = jest.fn().mockImplementation(() => {
            return Promise.resolve({
                ok: false,
                status: 429,
                json: () => Promise.resolve({
                    error: 'You have reached the suggestion limit. Please try again later.',
                }),
            });
        });

        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        await waitFor(() => {
            expect(screen.getByTestId('suggest-error')).toHaveTextContent('reached the suggestion limit');
        });
    });

    test('400 response retains all four field values (AC-6)', async () => {
        global.fetch = jest.fn().mockImplementation(() => {
            return Promise.resolve({
                ok: false,
                status: 400,
                json: () => Promise.resolve({
                    error: 'Please correct the highlighted fields.',
                    field_errors: {website_url: ['Enter a valid URL.']},
                }),
            });
        });

        render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);
        fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme Corp'}});
        fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.example'}});
        fireEvent.change(screen.getByLabelText(/Careers page/), {target: {value: 'https://acme.com/careers'}});
        fireEvent.change(screen.getByLabelText(/Why should CRank/), {target: {value: 'Great company'}});
        fireEvent.click(screen.getByTestId('suggest-submit-btn'));

        await waitFor(() => {
            expect(screen.getByTestId('suggest-error')).toBeInTheDocument();
        });
        expect((screen.getByLabelText(/Company name/) as HTMLInputElement).value).toBe('Acme Corp');
        expect((screen.getByLabelText(/Public website/) as HTMLInputElement).value).toBe('https://acme.example');
        expect((screen.getByLabelText(/Careers page/) as HTMLInputElement).value).toBe('https://acme.com/careers');
        expect((screen.getByLabelText(/Why should CRank/) as HTMLTextAreaElement).value).toBe('Great company');
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
            // Stand up the page structure the isolation module targets,
            // including the modal-external skip link (review r2: it used to
            // stay a focusable page-level target while the modal was open).
            const shell = document.createElement('aside');
            shell.className = 'app-shell';
            document.body.appendChild(shell);
            const content = document.createElement('main');
            content.className = 'app-content';
            document.body.appendChild(content);
            const skipLink = document.createElement('a');
            skipLink.className = 'skip-to-content';
            skipLink.href = '#main-content';
            document.body.appendChild(skipLink);
            backgroundRoots = [shell, content, skipLink];
            document.body.style.overflow = 'auto';
            document.documentElement.style.overflow = 'auto';
        });

        afterEach(() => {
            for (const root of backgroundRoots) {
                root.remove();
            }
            document.body.style.overflow = '';
            document.documentElement.style.overflow = '';
        });

        test('locks document scrolling and inert-hides the background while open', () => {
            const {rerender} = render(<SuggestCompanyModal visible={false} onClose={jest.fn()} />);
            rerender(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);

            expect(document.body.style.overflow).toBe('hidden');
            // The root element carries the viewport overflow under the
            // stylesheet's `html, body { overflow-x: hidden }` rule — locking
            // body alone leaves the actual document scroller free (review r2).
            expect(document.documentElement.style.overflow).toBe('hidden');
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
            expect(document.documentElement.style.overflow).toBe('auto');
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

        test('submit failure keeps the isolation held; closing afterwards releases it (no leak on error)', async () => {
            // Error path (review r2): a failed submit must not wedge or leak
            // isolation — the dialog stays open with an error, so the
            // background must stay isolated, and the eventual close must
            // release everything cleanly.
            global.fetch = jest.fn().mockImplementation(() => Promise.reject(new Error('Network error')));
            const {rerender} = render(<SuggestCompanyModal visible={true} onClose={jest.fn()} />);

            fireEvent.change(screen.getByLabelText(/Company name/), {target: {value: 'Acme Corp'}});
            fireEvent.change(screen.getByLabelText(/Public website/), {target: {value: 'https://acme.com'}});
            fireEvent.click(screen.getByTestId('suggest-submit-btn'));

            await waitFor(() => {
                expect(screen.getByTestId('suggest-error')).toBeInTheDocument();
            });
            expect(document.body.style.overflow).toBe('hidden');
            expect(document.documentElement.style.overflow).toBe('hidden');
            for (const root of backgroundRoots) {
                expect(root).toHaveAttribute('inert');
                expect(root).toHaveAttribute('aria-hidden', 'true');
            }

            rerender(<SuggestCompanyModal visible={false} onClose={jest.fn()} />);
            expect(document.body.style.overflow).toBe('auto');
            expect(document.documentElement.style.overflow).toBe('auto');
            for (const root of backgroundRoots) {
                expect(root).not.toHaveAttribute('inert');
                expect(root).not.toHaveAttribute('aria-hidden');
            }
        });
    });
});
