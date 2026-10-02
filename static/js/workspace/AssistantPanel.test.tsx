// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import * as React from 'react';
import {render, screen, waitFor} from '@testing-library/react';
import AssistantPanel from './AssistantPanel';
import {WorkspaceContext} from './types';
import {fireEvent} from '@testing-library/react';
import {getWorkspaceSnapshot, resetWorkspaceForTests, setWorkspaceAccount, setWorkspaceContext} from './store';

jest.mock('../JobSearchChat', () => ({
    __esModule: true,
    default: () => <section data-testid="job-search-chat">chat</section>,
}));

beforeEach(() => {
    resetWorkspaceForTests();
});

describe('AssistantPanel', () => {
    // Must stay first: the lazy chat chunk is cached once any test resolves it.
    test('the fallback is a labelled status with visible loading copy', () => {
        render(<AssistantPanel mode="docked" context={null}/>);
        const loading = screen.getByTestId('assistant-loading');
        expect(loading).toHaveAttribute('role', 'status');
        expect(loading).toHaveTextContent('Loading conversation…');
        expect(screen.getByText('Loading conversation…')).toBeVisible();
    });

    test('renders the suspense fallback, then the chat replaces it', async () => {
        render(<AssistantPanel mode="docked" context={null}/>);
        // The lazy import resolves asynchronously even with the mock; the
        // fallback is what commits first.
        await waitFor(() => expect(screen.getByTestId('job-search-chat')).toBeInTheDocument());
        expect(screen.queryByTestId('assistant-loading')).not.toBeInTheDocument();
    });

    test.each(['docked', 'drawer', 'sheet'] as const)(
        'the header (title, Minimize, Close) renders while loading and after the chat resolves in %s mode',
        async (mode) => {
            render(<AssistantPanel mode={mode} context={null}/>);
            const assertHeader = () => {
                expect(screen.getByRole('heading', {name: /Job Search Assistant/})).toBeInTheDocument();
                expect(screen.getByRole('button', {name: 'Minimize assistant'})).toBeInTheDocument();
                expect(screen.getByRole('button', {name: 'Close assistant'})).toBeInTheDocument();
            };
            assertHeader();
            await waitFor(() => expect(screen.getByTestId('job-search-chat')).toBeInTheDocument());
            assertHeader();
        },
    );

    test.each(['docked', 'drawer', 'sheet'] as const)(
        'header controls keep accessible names with decorative visible labels in %s mode',
        (mode) => {
            render(<AssistantPanel mode={mode} context={null}/>);
            for (const [testId, name, label] of [
                ['assistant-minimize', 'Minimize assistant', 'Minimize'],
                ['assistant-close', 'Close assistant', 'Close'],
            ]) {
                const button = screen.getByTestId(testId);
                expect(button).toHaveAttribute('aria-label', name);
                const labelEl = button.querySelector('.assistant-panel-control-label');
                expect(labelEl).toHaveTextContent(label);
                expect(labelEl).toHaveAttribute('aria-hidden', 'true');
            }
        },
    );

    test('the #job-search-chat wrapper contains the chat section', async () => {
        const {container} = render(<AssistantPanel mode="docked" context={null}/>);
        await waitFor(() => expect(screen.getByTestId('job-search-chat')).toBeInTheDocument());
        const wrapper = container.querySelector('#job-search-chat');
        expect(wrapper).toBeTruthy();
        expect(wrapper?.querySelector('[data-testid="job-search-chat"]')).toBeTruthy();
    });

    test('sheet mode renders Back to results; docked and drawer do not', async () => {
        const {unmount} = render(<AssistantPanel mode="sheet" context={null}/>);
        expect(screen.getByTestId('assistant-back-to-results')).toBeInTheDocument();
        unmount();
        render(<AssistantPanel mode="docked" context={null}/>);
        expect(screen.queryByTestId('assistant-back-to-results')).not.toBeInTheDocument();
    });

    test('the lazy chunk marks the store loaded once resolved', async () => {
        expect(getWorkspaceSnapshot().loaded).toBe(false);
        render(<AssistantPanel mode="drawer" context={null}/>);
        await waitFor(() => expect(getWorkspaceSnapshot().loaded).toBe(true));
    });

    test('renders the context line when an organization name is set', () => {
        render(<AssistantPanel mode="docked" context={{surface: 'company', organizationName: 'Acme'}}/>);
        expect(screen.getByTestId('assistant-context-line')).toHaveTextContent('About Acme');
    });

    test('renders the search-term context line and the empty-context fallback', () => {
        const {unmount} = render(<AssistantPanel mode="docked" context={{surface: 'jobs', searchTerm: 'django'}}/>);
        expect(screen.getByTestId('assistant-context-line')).toHaveTextContent('Searching “django”');
        unmount();
        render(<AssistantPanel mode="docked" context={{surface: 'jobs'}}/>);
        expect(screen.queryByTestId('assistant-context-line')).not.toBeInTheDocument();
    });

    test('company, job and comparison contexts render the strip with a named Clear button', () => {
        const contexts: Array<[WorkspaceContext, string]> = [
            [{surface: 'company', organizationName: 'Acme'}, 'About Acme'],
            [{surface: 'jobs', jobId: 8}, 'About job #8'],
            [{surface: 'comparison', comparisonIds: [1, 2]}, 'Comparing 2 companies'],
        ];
        for (const [context, label] of contexts) {
            const {unmount} = render(<AssistantPanel mode="docked" context={context}/>);
            expect(screen.getByTestId('assistant-context-strip')).toBeInTheDocument();
            expect(screen.getByRole('button', {name: `Clear context: ${label}`})).toBeInTheDocument();
            unmount();
        }
    });

    test('no strip for rankings-only or search-only context', () => {
        render(<AssistantPanel mode="docked" context={{surface: 'rankings', searchTerm: 'django'}}/>);
        expect(screen.queryByTestId('assistant-context-strip')).not.toBeInTheDocument();
        expect(screen.queryByTestId('assistant-clear-context')).not.toBeInTheDocument();
    });

    test('Clear context clears the entity, keeps the surface, bumps the revision and focuses the heading', () => {
        setWorkspaceContext({surface: 'company', organizationId: 3, organizationName: 'Acme'});
        const revision = getWorkspaceSnapshot().contextRevision;
        render(<AssistantPanel mode="docked" context={getWorkspaceSnapshot().context}/>);
        fireEvent.click(screen.getByTestId('assistant-clear-context'));
        expect(getWorkspaceSnapshot().context).toEqual({surface: 'company'});
        expect(getWorkspaceSnapshot().contextRevision).toBe(revision + 1);
        expect(document.activeElement).toBe(document.getElementById('assistant-panel-title'));
    });

    test('Clear context also removes the company param so a reload does not reseed it', () => {
        window.history.replaceState({k: 1}, '', '/chat/?company=12&foo=bar#x');
        setWorkspaceContext({surface: 'chat', organizationId: 12});
        render(<AssistantPanel mode="docked" context={getWorkspaceSnapshot().context}/>);
        fireEvent.click(screen.getByTestId('assistant-clear-context'));
        expect(window.location.pathname + window.location.search + window.location.hash).toBe('/chat/?foo=bar#x');
        expect(window.history.state).toEqual({k: 1});
        window.history.replaceState(null, '', '/');
    });
});

describe('AssistantPanel priorities section (issue #480)', () => {
    const prefs = {exists: true, revision: 1, schema_version: 1, preferences: {}, fields: [],
        chips: [{path: 'culture', label: 'Culture', display: 'kind', hard: false, supported: true}],
        unsupported_criteria: []};

    beforeEach(() => {
        (global as any).fetch = jest.fn(() => Promise.resolve(new Response(JSON.stringify(prefs), {
            status: 200, headers: {'Content-Type': 'application/json'},
        })));
    });

    test('renders for an authenticated account and stays hidden for anonymous or unknown ones', async () => {
        const {container} = render(<AssistantPanel mode="docked" context={null}/>);
        expect(container.querySelector('.priorities-sidebar')).toBeNull();
        expect((global as any).fetch).not.toHaveBeenCalled();
        setWorkspaceAccount({status: 'authenticated', key: 'u'});
        expect(await screen.findAllByTestId('priority-chip')).toHaveLength(1);
        expect(container.querySelector('.assistant-panel-body > .priorities-sidebar')).not.toBeNull();
        setWorkspaceAccount({status: 'anonymous', key: ''});
        await waitFor(() => expect(container.querySelector('.priorities-sidebar')).toBeNull());
    });

    test('is outside the chat Suspense boundary, so it is present while the chat is pending', async () => {
        setWorkspaceAccount({status: 'authenticated', key: 'u'});
        render(<AssistantPanel mode="docked" context={null}/>);
        expect(await screen.findByTestId('priorities-sidebar')).toBeInTheDocument();
    });
});
