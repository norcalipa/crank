// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, within} from '@testing-library/react';
import * as React from 'react';

import {Transcript} from './Transcript';
import type {TranscriptProps} from './Transcript';
import type {ChatMessage} from './types';

const message = (id: number, role: 'user' | 'assistant'): ChatMessage => ({
    id, role, content: `${role} ${id}`, preferences_changed: false, created: null, results: null,
});

const messages = [message(1, 'user'), message(2, 'assistant'), message(3, 'user'), message(4, 'assistant')];

function renderTranscript(extra: Partial<TranscriptProps> = {}) {
    const props: TranscriptProps = {
        historyRef: React.createRef<HTMLDivElement>(),
        messages, staleNotes: {}, turnActions: {}, newestActionsId: null, lastAssistantId: 4,
        availability: null, retryKey: null, pending: false, loading: false, authenticated: true,
        workspaceMode: undefined, onAskFirstQuestion: jest.fn(), onCheckResponse: jest.fn(),
        onRetryMessage: jest.fn(), onEditAsNew: jest.fn(),
        ...extra,
    };
    return render(<Transcript {...props}/>);
}

const node = <div role="status" data-testid="preference-proposal-notice">Proposed change</div>;
const noticeAt = (afterId: number | null, kind = 'proposal', body: React.ReactNode = node) => ({kind, afterId, node: body});
const childNames = () => Array.from(screen.getByRole('log').children).map((el) => (
    el.getAttribute('data-testid') ?? el.getAttribute('aria-label')
));

describe('notices in the transcript (issue #483)', () => {
    test('a notice follows the article it is anchored to, inside the log', () => {
        renderTranscript({notices: [noticeAt(2)]});
        const log = screen.getByRole('log');
        expect(within(log).getByTestId('preference-proposal-notice')).toBeInTheDocument();
        expect(childNames()).toEqual([
            'Your message', 'Assistant message', 'transcript-notice', 'Your message', 'Assistant message',
        ]);
        expect(log).toHaveAttribute('data-notice-anchor', '2');
    });

    test('a notice with no anchor goes after the last article', () => {
        renderTranscript({notices: [noticeAt(null)]});
        expect(childNames()).toEqual([
            'Your message', 'Assistant message', 'Your message', 'Assistant message', 'transcript-notice',
        ]);
        expect(screen.getByRole('log')).not.toHaveAttribute('data-notice-anchor');
    });

    test('notices with their own anchors sit under their own replies', () => {
        renderTranscript({notices: [
            noticeAt(2, 'changes', <div data-testid="applied">Applied</div>),
            noticeAt(4, 'proposal', <div data-testid="next-proposal">Next</div>),
        ]});
        expect(childNames()).toEqual([
            'Your message', 'Assistant message', 'transcript-notice', 'Your message', 'Assistant message', 'transcript-notice',
        ]);
        const blocks = screen.getAllByTestId('transcript-notice');
        expect(within(blocks[0]).getByTestId('applied')).toBeInTheDocument();
        expect(within(blocks[1]).getByTestId('next-proposal')).toBeInTheDocument();
        expect(screen.getByRole('log')).toHaveAttribute('data-notice-anchor', '2 4');
    });

    test('notices under the same reply share one block, in the order given', () => {
        renderTranscript({notices: [
            noticeAt(2, 'proposal', <div data-testid="first">First</div>),
            noticeAt(99, 'banner', <div data-testid="second">Second</div>),
            noticeAt(4, 'search', <div data-testid="third">Third</div>),
        ]});
        const blocks = screen.getAllByTestId('transcript-notice');
        expect(blocks).toHaveLength(2);
        expect(Array.from(blocks[0].children).map((el) => el.getAttribute('data-testid'))).toEqual(['first']);
        // The banner's anchor is not in the history: it goes after the last reply, with the notice anchored there.
        expect(Array.from(blocks[1].children).map((el) => el.getAttribute('data-testid'))).toEqual(['second', 'third']);
    });

    test('a notice whose anchor is no longer in the history goes after the last article', () => {
        renderTranscript({notices: [noticeAt(99)]});
        expect(childNames()[4]).toBe('transcript-notice');
    });

    test('the pending indicator stays below the notice', () => {
        renderTranscript({notices: [noticeAt(4)], pending: true});
        expect(childNames().slice(-2)).toEqual(['transcript-notice', 'pending-status']);
    });

    test('there is no notice node without a notice', () => {
        renderTranscript({});
        expect(screen.queryByTestId('transcript-notice')).not.toBeInTheDocument();
    });

    test('a notice in an empty history still shows in the log', () => {
        renderTranscript({messages: [], notices: [noticeAt(2)]});
        expect(within(screen.getByRole('log')).getByTestId('transcript-notice')).toBeInTheDocument();
    });
});
