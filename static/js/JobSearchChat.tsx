// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createRoot} from 'react-dom/client';

export interface ChatMessage {
    id: number;
    role: 'user' | 'assistant';
    content: string;
    preferences_changed: boolean;
    created: string | null;
}

interface Conversation {
    id: number;
    active: boolean;
    created: string | null;
    modified: string | null;
    messages: ChatMessage[];
    preferences_changed: boolean;
}

interface SubmitResponse {
    message: ChatMessage;
    preferences_changed: boolean;
}

interface ApiError {
    error?: {type?: string; message?: string; request_id?: string};
}

function getCookie(name: string): string {
    const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match[2]) : '';
}

function newId(): string {
    const cryptoObj = typeof crypto !== 'undefined' ? crypto : null;
    if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
        return cryptoObj.randomUUID();
    }
    // Fallback for older runtimes/tests without crypto.randomUUID.
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        const v = c === 'x' ? r : (r & 0x3) | 0x8;
        return v.toString(16);
    });
}

async function csrfFetch(url: string, init: RequestInit = {}): Promise<Response> {
    const method = (init.method || 'GET').toUpperCase();
    const headers: Record<string, string> = {...(init.headers as Record<string, string> || {})};
    if (method !== 'GET' && method !== 'HEAD') {
        const token = getCookie('csrftoken');
        if (token) {
            headers['X-CSRFToken'] = token;
        }
        headers['Content-Type'] = 'application/json';
    }
    return fetch(url, {...init, headers});
}

const JobSearchChat: React.FC = () => {
    const [conversationId, setConversationId] = React.useState<number | null>(null);
    const [messages, setMessages] = React.useState<ChatMessage[]>([]);
    const [input, setInput] = React.useState('');
    const [pending, setPending] = React.useState(false);
    const [loading, setLoading] = React.useState(true);
    const [initError, setInitError] = React.useState<string | null>(null);
    const [error, setError] = React.useState<string | null>(null);
    const [retrying, setRetrying] = React.useState(false);
    const [preferencesChanged, setPreferencesChanged] = React.useState(false);
    const [prefDismissed, setPrefDismissed] = React.useState(false);

    // Ref to the last submitted turn so Retry replays the same content + idempotency key.
    const lastSent = React.useRef<{content: string; key: string} | null>(null);
    const inputRef = React.useRef<HTMLInputElement>(null);
    const statusRef = React.useRef<HTMLDivElement>(null);
    const historyRef = React.useRef<HTMLDivElement>(null);
    const nearBottomRef = React.useRef(true);
    const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);

    const prefersReducedMotion = (): boolean => (
        typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );

    const isNearBottom = (element: HTMLDivElement): boolean => (
        element.scrollHeight - element.scrollTop - element.clientHeight <= 48
    );

    const scrollToLatest = (behavior: ScrollBehavior = prefersReducedMotion() ? 'auto' : 'smooth') => {
        const history = historyRef.current;
        if (!history) return;
        if (typeof history.scrollTo === 'function') {
            history.scrollTo({top: history.scrollHeight, behavior});
        } else {
            history.scrollTop = history.scrollHeight;
        }
        nearBottomRef.current = true;
        setShowJumpToLatest(false);
    };

    // Keep the latest content visible only while the reader is already at the bottom.
    React.useEffect(() => {
        const history = historyRef.current;
        if (!history) return;
        const handleScroll = () => {
            const nearBottom = isNearBottom(history);
            nearBottomRef.current = nearBottom;
            setShowJumpToLatest(!nearBottom);
        };
        history.addEventListener('scroll', handleScroll, {passive: true});
        return () => history.removeEventListener('scroll', handleScroll);
    }, []);

    // Initial history, optimistic turns, replies, and the pending indicator all append
    // content to the same viewport. Do not interrupt someone reading older messages.
    React.useEffect(() => {
        if (loading || !nearBottomRef.current) {
            if (!nearBottomRef.current) setShowJumpToLatest(true);
            return;
        }
        scrollToLatest();
    }, [messages.length, pending, loading]);

    // Visual viewport changes cover mobile keyboards and orientation changes. Preserve
    // the reader's position when they are browsing older messages.
    React.useEffect(() => {
        const handleViewportResize = () => {
            const history = historyRef.current;
            if (!history) return;
            const nearBottom = isNearBottom(history);
            nearBottomRef.current = nearBottom;
            setShowJumpToLatest(!nearBottom);
            if (nearBottom) scrollToLatest('auto');
        };
        window.addEventListener('resize', handleViewportResize);
        window.visualViewport?.addEventListener('resize', handleViewportResize);
        return () => {
            window.removeEventListener('resize', handleViewportResize);
            window.visualViewport?.removeEventListener('resize', handleViewportResize);
        };
    }, []);

    // Resume the user's most recent conversation on load.
    React.useEffect(() => {
        let cancelled = false;
        csrfFetch('/api/agent/conversations/')
            .then(async (res) => {
                if (cancelled) return;
                if (res.status === 404) {
                    // No existing conversation — create one so the user can start chatting.
                    try {
                        const createRes = await csrfFetch('/api/agent/conversations/', {
                            method: 'POST',
                            body: JSON.stringify({create_new: true}),
                        });
                        if (cancelled) return;
                        if (!createRes.ok) throw new Error('create-failed');
                        const createData = (await createRes.json()) as Conversation;
                        if (cancelled) return;
                        setConversationId(createData.id);
                        setMessages(createData.messages);
                        setLoading(false);
                        inputRef.current?.focus();
                    } catch {
                        if (cancelled) return;
                        setInitError('Could not start a conversation. Please try again.');
                        setLoading(false);
                    }
                    return;
                }
                if (!res.ok) {
                    throw new Error(`Resume failed (${res.status})`);
                }
                const data = (await res.json()) as Conversation;
                if (cancelled) return;
                setConversationId(data.id);
                setMessages(data.messages);
                setPreferencesChanged(data.preferences_changed);
                setLoading(false);
                inputRef.current?.focus();
            })
            .catch(() => {
                if (cancelled) return;
                setInitError('Could not load your conversation. Please refresh.');
                setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    // Announce new assistant content to assistive tech.
    React.useEffect(() => {
        if (historyRef.current) {
            const last = historyRef.current.lastElementChild;
            if (last instanceof HTMLElement) {
                last.setAttribute('aria-live', 'polite');
            }
        }
    }, [messages.length]);

    const isReady = conversationId !== null && !pending && !loading;

    const ensureConversation = async (createNew: boolean): Promise<number> => {
        const body = createNew ? {create_new: true} : {};
        const res = await csrfFetch('/api/agent/conversations/', {
            method: 'POST',
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            throw new Error('start-failed');
        }
        const data = (await res.json()) as Conversation;
        setConversationId(data.id);
        setMessages(data.messages);
        return data.id;
    };

    const sendTurn = async (content: string, key: string) => {
        if (!conversationId) return;
        setPending(true);
        setError(null);
        setRetrying(false);

        // Optimistically append the user's turn so the UI reflects it immediately.
        const optimisticUser: ChatMessage = {
            id: -Date.now(),
            role: 'user',
            content,
            preferences_changed: false,
            created: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, optimisticUser]);

        try {
            const res = await csrfFetch(`/api/agent/conversations/${conversationId}/`, {
                method: 'POST',
                body: JSON.stringify({content, idempotency_key: key}),
            });
            if (!res.ok) {
                let serverMsg = `Request failed (${res.status})`;
                try {
                    const body = (await res.json()) as ApiError;
                    if (body.error && body.error.message) serverMsg = body.error.message;
                } catch {
                    // non-JSON error; keep the generic message
                }
                throw new Error(serverMsg);
            }
            const data = (await res.json()) as SubmitResponse;
            // Keep the optimistic user turn; append the persisted assistant reply.
            setMessages((prev) => [...prev, data.message]);
            if (data.preferences_changed) {
                setPreferencesChanged(true);
                setPrefDismissed(false);
            }
            lastSent.current = null;
        } catch (e) {
            // Roll back the optimistic user turn; the server persisted nothing we
            // should double-render. Retry replays the same content + idempotency key.
            setMessages((prev) => prev.filter((m) => m !== optimisticUser));
            setError(e instanceof Error ? e.message : 'Something went wrong.');
            lastSent.current = {content, key};
            setRetrying(true);
        } finally {
            setPending(false);
            setInput('');
            // Defer refocus until React flushes the re-render (the submit button
            // becoming disabled would otherwise steal focus back to <body>).
            window.setTimeout(() => inputRef.current?.focus(), 0);
        }
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        const content = input.trim();
        if (!content || !isReady) return;
        await sendTurn(content, newId());
    };

    const handleRetry = async () => {
        const sent = lastSent.current;
        if (!sent) return;
        setRetrying(false);
        await sendTurn(sent.content, sent.key);
    };

    const handleCreateConversation = async () => {
        setInitError(null);
        setLoading(true);
        try {
            await ensureConversation(true);
        } catch {
            setInitError('Could not start a conversation. Please try again.');
        } finally {
            setLoading(false);
            inputRef.current?.focus();
        }
    };

    const handleExport = async () => {
        if (!conversationId) return;
        try {
            const res = await csrfFetch(`/api/agent/conversations/${conversationId}/export/`);
            if (!res.ok) throw new Error('export-failed');
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `job-search-${conversationId}.json`;
            a.click();
            URL.revokeObjectURL(a.href);
        } catch {
            setError('Could not export your conversation.');
        }
    };

    const handleReset = async () => {
        if (!conversationId) return;  // # pragma: no cover - button is disabled without a conversation
        if (!window.confirm('Start a new conversation? Your current history will be archived.')) return;
        try {
            const res = await csrfFetch(`/api/agent/conversations/${conversationId}/reset/`, {method: 'POST'});
            if (!res.ok) throw new Error('reset-failed');
            const data = (await res.json()) as Conversation;
            setConversationId(data.id);
            setMessages([]);
            setPreferencesChanged(false);
            setPrefDismissed(false);
            setError(null);
            inputRef.current?.focus();
        } catch {
            setError('Could not reset the conversation.');
        }
    };

    const handleDelete = async () => {
        if (!conversationId) return;
        if (!window.confirm('Delete this conversation permanently? This cannot be undone.')) return;
        try {
            const res = await csrfFetch(`/api/agent/conversations/${conversationId}/delete/`, {method: 'POST'});
            if (!res.ok) throw new Error('delete-failed');
            setConversationId(null);
            setMessages([]);
            setPreferencesChanged(false);
            setPrefDismissed(false);
            setError(null);
            inputRef.current?.focus();
        } catch {
            setError('Could not delete the conversation.');
        }
    };

    const revealingPrefs = preferencesChanged && !prefDismissed;

    return (
        <section className="card bg-dark d-flex flex-column" data-testid="job-search-chat"
                 aria-labelledby="job-search-chat-title"
                 style={{height: 'calc(100dvh - 7rem)', minHeight: '20rem'}}>
            <div className="card-header d-flex justify-content-between align-items-center">
                <h2 id="job-search-chat-title" className="h6 mb-0">Conversation</h2>
                <div className="btn-group btn-group-sm flex-wrap" role="group" aria-label="Conversation controls">
                    <button type="button" className="btn btn-outline-light" onClick={handleExport}
                            disabled={!conversationId || !messages.length} aria-label="Export conversation">Export</button>
                    <button type="button" className="btn btn-outline-light" onClick={handleReset}
                            disabled={!conversationId} aria-label="Reset conversation">Reset</button>
                    <button type="button" className="btn btn-outline-danger" onClick={handleDelete}
                            disabled={!conversationId} aria-label="Delete conversation">Delete</button>
                </div>
            </div>

            <div className="card-body d-flex flex-column" style={{minHeight: 0}}>
                <div id="job-search-data-note" className="alert alert-info" role="note">
                    The assistant is automated and can be wrong. Check important details yourself. Your messages
                    and preference updates are saved to your account; use Export, Reset, or Delete above to manage them.
                </div>

                {revealingPrefs && (
                    <div className="alert alert-success d-flex justify-content-between align-items-center"
                         role="status" aria-label="Preference update" aria-describedby="preference-update-help">
                        <span>
                            <i className="fa-solid fa-circle-check me-1"></i>
                            Your saved preferences were updated based on this conversation.
                        </span>
                        <span id="preference-update-help" className="visually-hidden">
                            You can correct or remove a preference by telling the assistant what to change.
                        </span>
                        <button type="button" className="btn-close" aria-label="Dismiss preference notice"
                                onClick={() => setPrefDismissed(true)}></button>
                    </div>
                )}

                {initError && (
                    <div className="alert alert-danger" role="alert">
                        {initError}
                        <div className="mt-2">
                            <button type="button" className="btn btn-sm btn-primary" onClick={handleCreateConversation}>
                                Start a conversation
                            </button>
                        </div>
                    </div>
                )}

                <div className="position-relative d-flex flex-column flex-grow-1" style={{minHeight: 0}}>
                    <div className="bg-dark border rounded p-3 mb-3 flex-grow-1" style={{minHeight: 0, overflowY: 'auto'}}
                         ref={historyRef} role="log" aria-live="polite" aria-label="Message history" aria-busy={pending}>
                        {messages.length === 0 && !loading && (
                            <p className="text-muted mb-0" data-testid="empty-history">
                                Ask about compensation, work location, funding, or culture to get started.
                            </p>
                        )}
                        {messages.map((m) => (
                            <article key={m.id} aria-label={m.role === 'user' ? 'Your message' : 'Assistant message'}
                                     className={`d-flex ${m.role === 'user' ? 'justify-content-end' : 'justify-content-start'} mb-2`}>
                                <div className={`rounded p-2 ${m.role === 'user' ? 'bg-primary' : 'bg-secondary'}`}
                                     style={{maxWidth: '80%', whiteSpace: 'pre-wrap', wordBreak: 'break-word'}}>
                                    {m.content}
                                </div>
                            </article>
                        ))}
                        {pending && (
                            <div className="text-muted" role="status" aria-live="polite" data-testid="pending-status">
                                <i className="fa-solid fa-spinner fa-spin me-1"></i>Assistant is typing…
                            </div>
                        )}
                    </div>
                    {showJumpToLatest && (
                        <button type="button" className="btn btn-sm btn-primary position-absolute bottom-0 end-0 mb-4 me-2"
                                onClick={() => scrollToLatest('auto')} aria-label="Jump to latest message"
                                data-testid="jump-to-latest">
                            New messages · Jump to latest
                        </button>
                    )}
                </div>

                <div className="flex-shrink-0" style={{paddingBottom: 'calc(0.25rem + env(safe-area-inset-bottom))'}}>
                    {error && (
                        <div className="alert alert-danger d-flex justify-content-between align-items-center"
                             role="alert" data-testid="chat-error">
                            <span>{error}</span>
                            {retrying && (
                                <button type="button" className="btn btn-sm btn-outline-danger ms-2"
                                        onClick={handleRetry} data-testid="retry-button">
                                    Retry
                                </button>
                            )}
                        </div>
                    )}

                    <form onSubmit={handleSubmit} aria-busy={pending}>
                        <div className="input-group">
                            <input
                                ref={inputRef}
                                type="text"
                                className="form-control"
                                placeholder="Type your message…"
                                aria-label="Message"
                                value={input}
                                onChange={(e) => setInput(e.target.value)}
                                disabled={!conversationId || pending}
                                autoComplete="off"
                            />
                            <button type="submit" className="btn btn-primary" disabled={!conversationId || pending || !input.trim()}
                                    aria-label="Send message">
                                <i className="fa-solid fa-paper-plane"></i>
                            </button>
                        </div>
                    </form>
                </div>

                {/* Screen-reader-only live region for pending/error transitions. */}
                <div ref={statusRef} className="visually-hidden" role="status" aria-live="assertive">
                    {pending ? 'Sending message.' : ''}
                    {!pending && error ? 'Your message could not be sent.' : ''}
                </div>
            </div>
        </section>
    );
};

export default JobSearchChat;

document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('job-search-chat');
    if (container) {
        const root = createRoot(container);
        root.render(<JobSearchChat/>);
    }
});
