// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.

// One rule for "the session has ended" (issue #547): a 401, or a followed
// redirect that landed on the sign-in page (its path, not its query string).
// Any other redirect is not a sign-out.

function pathOf(url: string): string | null {
    try {
        return new URL(url, window.location.href).pathname;
    } catch {
        return null;
    }
}

export function sessionEnded(res: Pick<Response, 'status' | 'redirected' | 'url'>, signInUrl: string): boolean {
    if (res.status === 401) return true;
    if (res.redirected !== true || !res.url) return false;
    const landed = pathOf(res.url);
    return landed !== null && landed === pathOf(signInUrl);
}

export const SESSION_ENDED_COPY = 'Your session has ended. Sign in again, then try again.';
