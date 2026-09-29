// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {act, screen, waitFor} from '@testing-library/react';
import {SUGGEST_COMPANY_EVENT, closeSuggestCompany} from './controller';
// Registers the guarded DOMContentLoaded mount once at import time, mirroring
// how webpack's `main` entry loads it on every real page.
import './mount';

describe('suggestCompany mount', () => {
    afterEach(() => {
        act(() => {
            closeSuggestCompany();
        });
        document.body.innerHTML = '';
    });

    test('mounts the host and opens the modal on a subsequent dispatch when #suggest-company-host is present', async () => {
        const host = document.createElement('div');
        host.id = 'suggest-company-host';
        document.body.appendChild(host);

        document.dispatchEvent(new Event('DOMContentLoaded'));
        window.dispatchEvent(new CustomEvent(SUGGEST_COMPANY_EVENT, {detail: {source: 'rankings'}}));

        // The modal renders through a portal onto <body>, not into the host
        // anchor itself (see SuggestCompanyModal's createPortal call).
        await waitFor(() => {
            expect(screen.getByTestId('suggest-company-modal')).toBeInTheDocument();
        });
    });

    test('does nothing and throws no error when #suggest-company-host is absent', () => {
        expect(document.getElementById('suggest-company-host')).toBeNull();

        expect(() => {
            document.dispatchEvent(new Event('DOMContentLoaded'));
        }).not.toThrow();

        expect(document.getElementById('suggest-company-host')).toBeNull();
    });

    test('opens the correction form for a correction context and the company form otherwise', async () => {
        global.fetch = jest.fn().mockResolvedValue({ok: true, json: () => Promise.resolve({fields: [], unverified_fields: []})});
        const host = document.createElement('div');
        host.id = 'suggest-company-host';
        document.body.appendChild(host);

        document.dispatchEvent(new Event('DOMContentLoaded'));
        window.dispatchEvent(new CustomEvent(SUGGEST_COMPANY_EVENT, {
            detail: {kind: 'correction', source: 'assistant', organizationId: 3, companyName: 'Acme'},
        }));

        await waitFor(() => {
            expect(screen.getByTestId('company-correction-modal')).toBeInTheDocument();
        });
        expect(screen.queryByTestId('suggest-company-modal')).toBeNull();
    });

    test('a correction context without an organization id falls back to the company form', async () => {
        const host = document.createElement('div');
        host.id = 'suggest-company-host';
        document.body.appendChild(host);

        document.dispatchEvent(new Event('DOMContentLoaded'));
        window.dispatchEvent(new CustomEvent(SUGGEST_COMPANY_EVENT, {
            detail: {kind: 'correction', source: 'assistant'},
        }));

        expect((await screen.findAllByTestId('suggest-company-modal')).length).toBeGreaterThan(0);
        expect(screen.queryByTestId('company-correction-modal')).toBeNull();
    });
});
