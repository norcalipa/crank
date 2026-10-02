// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Minimal CSRF-aware fetch for the priorities bundle (issue #480). Kept apart
// from the chat chunk so the non-blocking `jobmatch` bundle never imports it.

function getCookie(name: string): string | null {
    const match = document.cookie.split('; ').find((row) => row.startsWith(`${name}=`));
    return match ? decodeURIComponent(match.slice(name.length + 1)) : null;
}

export function csrfFetch(url: string, init: RequestInit = {}): Promise<Response> {
    const method = (init.method || 'GET').toUpperCase();
    const headers: Record<string, string> = {...((init.headers as Record<string, string>) || {})};
    if (method !== 'GET' && method !== 'HEAD') {
        const token = getCookie('csrftoken');
        if (token) {
            headers['X-CSRFToken'] = token;
        }
        headers['Content-Type'] = 'application/json';
    }
    return fetch(url, {...init, headers, credentials: 'same-origin'});
}
