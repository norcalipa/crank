// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Bounded background poller (issue #485). One timer, armed only while the
// document is visible; a hidden tab sends nothing and a tab that returns asks
// once if the interval has passed. One request at a time, with its OWN latest
// guard: sharing a load's guard would abort the load (`begin()` aborts the
// previous request). A failure backs off; a "stop" answer ends polling until
// `start()` is called again (a purge, then the next hydration).

import {createLatestGuard} from './requests';

export const MIN_POLL_SECONDS = 30;
export const MAX_POLL_SECONDS = 600;

/** Thrown by `request` when polling must stop until `start()` (a redirect to
 * sign-in, a 401): never data, never a retry. */
export class PollStop extends Error {}

export interface PollOutcome<T> {
    value: T;
    /** Seconds until the next ask; null stops polling (nothing to watch). */
    pollAfterSeconds: number | null;
}

export interface PollInfo {
    /** True for the first answer after the tab became visible again. */
    afterReturn: boolean;
}

export interface PollerOptions<T> {
    request: (signal: AbortSignal) => Promise<PollOutcome<T>>;
    onResult: (value: T, info: PollInfo) => void;
    /** Seconds before the first ask and until the server names its own. */
    intervalSeconds: number;
}

export interface Poller {
    /** Starts polling, or starts again after `stop()` or a stopping answer. */
    start: () => void;
    /** Clears the timer, aborts the request in flight and drops its answer. */
    stop: () => void;
}

export function clampPollSeconds(seconds: number): number {
    return Math.min(MAX_POLL_SECONDS, Math.max(MIN_POLL_SECONDS, seconds));
}

const isVisible = (): boolean => document.visibilityState === 'visible';

export function createPoller<T>(options: PollerOptions<T>): Poller {
    const guard = createLatestGuard();
    let running = false;
    let inFlight = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let intervalMs = clampPollSeconds(options.intervalSeconds) * 1000;
    let delayMs = intervalMs;
    let failures = 0;
    let lastCheckAt = 0;
    let returned = false;

    const clearTimer = () => {
        if (timer !== null) {
            clearTimeout(timer);
            timer = null;
        }
    };

    const arm = (waitMs: number) => {
        clearTimer();
        if (running && isVisible()) {
            timer = setTimeout(() => {
                timer = null;
                void check();
            }, waitMs);
        }
    };

    // Ends the run without cancelling a request: used by a stopping answer.
    const halt = () => {
        running = false;
        clearTimer();
        document.removeEventListener('visibilitychange', onVisibility);
    };

    async function check(): Promise<void> {
        if (!running || inFlight || !isVisible()) return;
        inFlight = true;
        lastCheckAt = Date.now();
        const afterReturn = returned;
        const request = guard.begin();
        let outcome: PollOutcome<T>;
        try {
            outcome = await options.request(request.signal);
        } catch (error) {
            if (!request.isLatest()) return;
            inFlight = false;
            if (error instanceof PollStop) {
                halt();
                return;
            }
            failures += 1;
            delayMs = Math.min(MAX_POLL_SECONDS * 1000, intervalMs * 2 ** failures);
            arm(delayMs);
            return;
        }
        if (!request.isLatest()) return;
        inFlight = false;
        failures = 0;
        returned = false;
        if (outcome.pollAfterSeconds === null) {
            halt();
        } else {
            intervalMs = clampPollSeconds(outcome.pollAfterSeconds) * 1000;
            delayMs = intervalMs;
        }
        options.onResult(outcome.value, {afterReturn});
        if (running) arm(delayMs);
    }

    const onVisibility = () => {
        if (!isVisible()) {
            // Nothing is sent while hidden; an answer already on its way is
            // still taken, but it arms no timer (`arm` checks visibility).
            clearTimer();
            return;
        }
        returned = true;
        if (inFlight) return;
        const waited = Date.now() - lastCheckAt;
        if (waited >= delayMs) {
            void check();
        } else {
            arm(delayMs - waited);
        }
    };

    return {
        start() {
            if (running) return;
            running = true;
            inFlight = false;
            failures = 0;
            returned = false;
            intervalMs = clampPollSeconds(options.intervalSeconds) * 1000;
            delayMs = intervalMs;
            lastCheckAt = Date.now();
            document.addEventListener('visibilitychange', onVisibility);
            arm(delayMs);
        },
        stop() {
            inFlight = false;
            halt();
            guard.cancel();
        },
    };
}
