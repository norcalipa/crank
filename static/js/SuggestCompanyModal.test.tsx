// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import * as React from 'react';
import SuggestCompanyModal from './SuggestCompanyModal';

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
});
