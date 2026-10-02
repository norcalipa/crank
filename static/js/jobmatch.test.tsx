// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {act, screen, waitFor} from '@testing-library/react';
import {resetWorkspaceForTests} from './workspace/store';

function mountPage(authenticated: boolean) {
    document.body.innerHTML = `
        <div id="priorities-main" data-authenticated="${authenticated}" data-sign-in-url="/accounts/login/?next=/chat/"></div>
        <div id="job-match-panel" data-authenticated="${authenticated}" data-sign-in-url="/accounts/login/?next=/chat/"></div>`;
}

async function boot() {
    jest.isolateModules(() => {
        // eslint-disable-next-line @typescript-eslint/no-var-requires
        require('./jobmatch');
    });
    await act(async () => {
        document.dispatchEvent(new Event('DOMContentLoaded'));
    });
}

beforeEach(() => {
    resetWorkspaceForTests();
    (global as any).fetch = jest.fn((url: string) => Promise.resolve(new Response(
        JSON.stringify(String(url).includes('/api/agent/preferences/')
            ? {exists: false, revision: 0, schema_version: 1, preferences: {}, fields: [], chips: [], unsupported_criteria: []}
            : {state: 'no_preferences', title: 'T', message: 'M', actions: []}),
        {status: 200, headers: {'Content-Type': 'application/json'}},
    )));
});

describe('jobmatch entry (issue #480 / #472 regression)', () => {
    test('a signed-out page shows one sign-in state in the job panel and the priorities copy only, without fetching', async () => {
        mountPage(false);
        await boot();
        const cta = await screen.findByTestId('job-match-sign-in-cta');
        expect(cta).toHaveAttribute('href', '/accounts/login/?next=/chat/');
        expect(screen.getByTestId('priorities-signed-out')).toHaveTextContent('Sign in to save your priorities.');
        expect(screen.queryByTestId('priorities-section')).not.toBeInTheDocument();
        expect((global as any).fetch).not.toHaveBeenCalled();
    });

    test('a signed-in page fetches matches status and priorities', async () => {
        mountPage(true);
        await boot();
        await waitFor(() => {
            const urls = ((global as any).fetch as jest.Mock).mock.calls.map((c) => String(c[0]));
            expect(urls.some((u) => u.includes('/api/agent/preferences/'))).toBe(true);
            expect(urls.some((u) => u.includes('match'))).toBe(true);
        });
        expect(screen.queryByTestId('job-match-signed-out')).not.toBeInTheDocument();
    });

    test('a page without the priorities mount still renders the job panel', async () => {
        document.body.innerHTML = '<div id="job-match-panel" data-authenticated="false" data-sign-in-url="/login/"></div>';
        await boot();
        expect(await screen.findByTestId('job-match-sign-in-cta')).toHaveAttribute('href', '/login/');
    });
});
