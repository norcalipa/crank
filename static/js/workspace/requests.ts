// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Latest-request guard (issue #479). Each `begin()` returns a monotonic
// token plus an AbortController and aborts the previous request, so a slower
// earlier response can never overwrite a newer one. #484/#485 reuse it.

export interface LatestRequest {
    token: number;
    signal: AbortSignal;
    // True while this request is still the newest one issued.
    isLatest: () => boolean;
}

export interface LatestGuard {
    begin: () => LatestRequest;
    // Aborts the in-flight request (unmount / purge) without issuing a new one.
    cancel: () => void;
}

export function createLatestGuard(): LatestGuard {
    let counter = 0;
    let controller: AbortController | null = null;
    return {
        begin() {
            controller?.abort();
            const own = new AbortController();
            controller = own;
            counter += 1;
            const token = counter;
            return {token, signal: own.signal, isLatest: () => token === counter};
        },
        cancel() {
            controller?.abort();
            controller = null;
            counter += 1;
        },
    };
}
