// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.

export function getCookie(name: string): string {
    const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match[2]) : '';
}

// Typed error envelopes that mean the request never persisted a turn: the
// server has no trace of it, so the UI must never claim "your message is
// saved" for these — the text stays an unsent draft instead (issue #458).
export const PRE_PERSISTENCE_ERROR_TYPES = new Set([
    'rate_limited',
    'invalid_message',
    'malformed_json',
    'payload_too_large',
    'invalid_request',
    'not_found',
]);
// Typed envelopes the server returns only AFTER the user turn is persisted;
// for these the failed-turn UI ("your message is saved; retry") is honest.
export const POST_PERSISTENCE_ERROR_TYPES = new Set([
    'assistant_unavailable',
    'provider_timeout',
    'cost_limit',
    'invalid_output',
    'service_error',
    'unexpected_error',
]);

export function newId(): string {
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

export async function csrfFetch(url: string, init: RequestInit = {}): Promise<Response> {
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
