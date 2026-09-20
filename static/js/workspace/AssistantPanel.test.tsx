// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import * as React from 'react';
import {render, screen, waitFor} from '@testing-library/react';
import AssistantPanel from './AssistantPanel';
import {getWorkspaceSnapshot, resetWorkspaceForTests} from './store';

jest.mock('../JobSearchChat', () => ({
    __esModule: true,
    default: () => <section data-testid="job-search-chat">chat</section>,
}));

beforeEach(() => {
    resetWorkspaceForTests();
});

describe('AssistantPanel', () => {
    test('renders the suspense fallback, then the chat replaces it', async () => {
        render(<AssistantPanel mode="docked" context={null}/>);
        // The lazy import resolves asynchronously even with the mock; the
        // fallback is what commits first.
        await waitFor(() => expect(screen.getByTestId('job-search-chat')).toBeInTheDocument());
        expect(screen.queryByTestId('assistant-loading')).not.toBeInTheDocument();
    });

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
});
