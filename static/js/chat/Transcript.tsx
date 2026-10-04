// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';

import {AvailabilityNotice} from './notices';
import {hasResults, ResultCards} from './ResultCards';
import type {ScrollOwner} from './useTranscriptScroll';
import type {AvailabilityPayload, ChatMessage} from './types';

export interface TranscriptProps {
    historyRef: React.RefObject<HTMLDivElement>;
    messages: ChatMessage[];
    staleNotes: Record<number, string>;
    lastAssistantId: number | null;
    availability: AvailabilityPayload | null;
    retryKey: string | null;
    pending: boolean;
    loading: boolean;
    authenticated: boolean;
    workspaceMode: 'docked' | 'drawer' | 'sheet' | undefined;
    scrollOwner: ScrollOwner;
    onAskFirstQuestion: () => void;
    onCheckResponse: (message: ChatMessage) => void;
    onRetryMessage: (message: ChatMessage) => void;
    onEditAsNew: (message: ChatMessage) => void;
}

export function Transcript({
    historyRef, messages, staleNotes, lastAssistantId, availability, retryKey, pending, loading,
    authenticated, workspaceMode, scrollOwner, onAskFirstQuestion,
    onCheckResponse, onRetryMessage, onEditAsNew,
}: TranscriptProps) {
    return (
        <div className="d-flex flex-column flex-grow-1" style={{minHeight: 0}}>
            <div className="bg-dark chat-transcript rounded p-3 mb-3 flex-grow-1" style={scrollOwner === 'panel' ? {minHeight: '8rem', overflowY: 'visible'} : {minHeight: 0, overflowY: 'auto'}}
                 data-scroll-owner={scrollOwner}
                 ref={historyRef} role="log" aria-live="polite" aria-label="Message history" aria-busy={pending}>
                {authenticated && loading && (
                    <div className="text-muted chat-loading-status" role="status" aria-live="polite"
                         data-testid="chat-loading">
                        <i className="fa-solid fa-spinner fa-spin me-2" aria-hidden="true"></i>Loading conversation…
                    </div>
                )}
                {authenticated && messages.length === 0 && !loading && (
                    <div data-testid="empty-history">
                        <p className="empty-history-lead mb-2">
                            Ask about compensation, work location, funding, or culture to get started.
                        </p>
                        <p className="empty-history-note small mb-3">
                            <i className="fa-solid fa-circle-info me-1"></i>
                            {workspaceMode === 'sheet'
                                ? 'Tap Back to results to view job-match status and results.'
                                : 'Job-match status and results appear in the Job Matches panel.'}
                        </p>
                        <button type="button" className="btn btn-primary empty-history-cta"
                                data-testid="empty-history-cta"
                                onClick={onAskFirstQuestion}>
                            <i className="fa-solid fa-pen-to-square me-1" aria-hidden="true"></i>
                            Ask your first question
                        </button>
                    </div>
                )}
                {messages.map((m) => (
                    <article key={m.id} aria-label={m.role === 'user' ? 'Your message' : 'Assistant message'}
                             tabIndex={-1}
                             className={`d-flex flex-column ${m.role === 'user' ? 'align-items-end' : 'align-items-start'} mb-2`}>
                        <div className={`chat-bubble ${m.role === 'user' ? 'chat-bubble-user' : 'chat-bubble-assistant'}`}
                             style={{maxWidth: '80%', wordBreak: 'break-word'}}>
                            <div style={{whiteSpace: 'pre-wrap', wordBreak: 'break-word'}}>{m.content}</div>
                            {m.role === 'assistant' && staleNotes[m.id] && (
                                <div className="chat-stale-context-note small mt-1"
                                     data-testid="stale-context-note">
                                    {staleNotes[m.id]}
                                </div>
                            )}
                            {m.role === 'assistant' && m.results && (
                                <ResultCards results={m.results} />
                            )}
                            {m.role === 'assistant' && m.id === lastAssistantId && !hasResults(m.results) && availability && availability.state !== 'ok' && (
                                <AvailabilityNotice availability={availability} />
                            )}
                            {m.role === 'user' && m.delivery_state === 'pending' && retryKey !== m.idempotency_key && (
                                <div className="chat-retry-panel mt-2" data-testid="pending-turn">
                                    <div className="chat-status-row" role="status">
                                        <i className="fa-solid fa-hourglass-half chat-status-icon" aria-hidden="true"></i>
                                        <div>
                                            <div>The response has not arrived yet.</div>
                                        </div>
                                    </div>
                                    <div className="chat-actions mt-3" role="group" aria-label="Pending turn actions">
                                        <button type="button" className="chat-btn chat-btn-primary chat-focus"
                                                onClick={() => onCheckResponse(m)}
                                                aria-label="Check for response" data-testid="check-response-button">
                                            Check for response
                                        </button>
                                    </div>
                                </div>
                            )}
                            {m.role === 'user' && retryKey !== null && m.idempotency_key === retryKey && m.delivery_state !== 'completed' && (
                                <div className="chat-retry-panel mt-2" data-testid="retrying-turn">
                                    <div className="chat-status-row" role="status">
                                        <i className="fa-solid fa-spinner fa-spin chat-status-icon" aria-hidden="true"></i>
                                        <div>
                                            <div>Retrying response…</div>
                                        </div>
                                    </div>
                                    <div className="chat-actions mt-3" role="group" aria-label="Failed turn actions">
                                        <button type="button" className="chat-btn chat-btn-primary" disabled
                                                aria-label="Retrying response" data-testid="retry-response-button">
                                            Retrying…
                                        </button>
                                        <button type="button" className="chat-btn chat-btn-secondary" disabled
                                                aria-label="Edit as new message" data-testid="edit-as-new-button">
                                            Edit as new message
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>
                        {m.role === 'user' && m.delivery_state === 'failed' && retryKey !== m.idempotency_key && (
                            <div className="chat-failure-panel chat-failure-panel--outside" data-testid="failed-turn">
                                <div className="chat-status-row" role="status">
                                    <i className="fa-solid fa-triangle-exclamation chat-status-icon" aria-hidden="true"></i>
                                    <div>
                                        {m.retry_available === false ? (
                                            <div>Response failed after several retries. Your message is saved.</div>
                                        ) : (
                                            <div>Response failed. Your message is saved; retries are limited.</div>
                                        )}
                                    </div>
                                </div>
                                <div className="chat-actions mt-3" role="group" aria-label="Failed turn actions">
                                    {m.retry_available === false ? (
                                        <button type="button" className="chat-btn chat-btn-primary chat-focus" disabled
                                                aria-label="Retry limit reached" data-testid="retry-response-button">
                                            Retry limit reached
                                        </button>
                                    ) : (
                                        <button type="button" className="chat-btn chat-btn-primary chat-focus"
                                                onClick={() => onRetryMessage(m)}
                                                aria-label="Retry response" data-testid="retry-response-button">
                                            Retry response
                                        </button>
                                    )}
                                    <button type="button" className="chat-btn chat-btn-secondary chat-focus"
                                            onClick={() => onEditAsNew(m)}
                                            aria-label="Edit as new message" data-testid="edit-as-new-button">
                                        Edit as new message
                                    </button>
                                </div>
                            </div>
                        )}
                    </article>
                ))}
                {pending && (
                    <div className="text-muted" role="status" aria-live="polite" data-testid="pending-status">
                        <i className="fa-solid fa-spinner fa-spin me-1"></i>Assistant is typing…
                    </div>
                )}
            </div>
        </div>
    );
}
