// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {screen, waitFor} from '@testing-library/react';
import {SUGGEST_COMPANY_EVENT} from './controller';
// Registers the guarded DOMContentLoaded mount once at import time, mirroring
// how webpack's `main` entry loads it on every real page.
import './mount';

describe('suggestCompany mount', () => {
    afterEach(() => {
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
});
