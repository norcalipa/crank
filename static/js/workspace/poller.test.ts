// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {clampPollSeconds, createPoller, Poller, PollOutcome, PollStop} from './poller';

let visibility: 'visible' | 'hidden' = 'visible';

function setVisibility(next: 'visible' | 'hidden') {
    visibility = next;
    document.dispatchEvent(new Event('visibilitychange'));
}

const answer = (pollAfterSeconds: number | null = 60, value = 1): PollOutcome<number> =>
    ({value, pollAfterSeconds});

beforeEach(() => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date('2026-10-14T12:00:00Z'));
    visibility = 'visible';
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => visibility});
});

afterEach(() => {
    jest.useRealTimers();
});

function make(request: jest.Mock, intervalSeconds = 60) {
    const results: Array<{value: number; afterReturn: boolean}> = [];
    const poller: Poller = createPoller<number>({
        request,
        onResult: (value, info) => results.push({value, afterReturn: info.afterReturn}),
        intervalSeconds,
    });
    return {poller, results};
}

const tick = async (ms: number) => {
    await jest.advanceTimersByTimeAsync(ms);
};

describe('clampPollSeconds', () => {
    it('keeps the server value between 30 and 600 seconds', () => {
        expect(clampPollSeconds(5)).toBe(30);
        expect(clampPollSeconds(60)).toBe(60);
        expect(clampPollSeconds(9000)).toBe(600);
    });
});

describe('createPoller', () => {
    it('asks once per interval, not at start', async () => {
        const request = jest.fn().mockResolvedValue(answer(60));
        const {poller, results} = make(request);
        poller.start();
        expect(request).not.toHaveBeenCalled();
        await tick(59_000);
        expect(request).not.toHaveBeenCalled();
        await tick(1_000);
        expect(request).toHaveBeenCalledTimes(1);
        expect(results).toEqual([{value: 1, afterReturn: false}]);
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(2);
        poller.stop();
    });

    it('follows the interval the server names, clamped', async () => {
        const request = jest.fn().mockResolvedValue(answer(5));
        const {poller} = make(request);
        poller.start();
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(1);
        await tick(29_000);
        expect(request).toHaveBeenCalledTimes(1);
        await tick(1_000);
        expect(request).toHaveBeenCalledTimes(2);
        poller.stop();
    });

    it('sends at most 10 requests in 10 simulated visible minutes, 0 hidden, 1 on return', async () => {
        const request = jest.fn().mockResolvedValue(answer(60));
        const {poller, results} = make(request);
        poller.start();
        await tick(10 * 60_000);
        expect(request.mock.calls.length).toBeLessThanOrEqual(10);
        request.mockClear();
        setVisibility('hidden');
        await tick(10 * 60_000);
        expect(request).not.toHaveBeenCalled();
        setVisibility('visible');
        await tick(0);
        expect(request).toHaveBeenCalledTimes(1);
        expect(results[results.length - 1].afterReturn).toBe(true);
        poller.stop();
    });

    it('a flip back within seconds waits for the timer, and that check is not the return check', async () => {
        const request = jest.fn().mockResolvedValue(answer(60));
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(1);
        await tick(2_000);
        setVisibility('hidden');
        await tick(3_000);
        setVisibility('visible');
        await tick(0);
        expect(request).toHaveBeenCalledTimes(1);
        await tick(55_000);
        expect(request).toHaveBeenCalledTimes(2);
        // The delayed check is not "the first check after the return" (issue
        // #485 review): it never carries the flag.
        expect(results.map((r) => r.afterReturn)).toEqual([false, false]);
        poller.stop();
    });

    it('asks at once on a return after 10 s or more, even before the interval has passed', async () => {
        const request = jest.fn().mockResolvedValue(answer(60));
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        await tick(10_000);
        setVisibility('hidden');
        await tick(15_000);
        setVisibility('visible');
        await tick(0);
        expect(request).toHaveBeenCalledTimes(2);
        // Only that one check carries the flag; the next does not.
        await tick(60_000);
        expect(results.map((r) => r.afterReturn)).toEqual([false, true, false]);
        poller.stop();
    });

    it('a failed check at the return consumes the flag: the retry is not an after-return check', async () => {
        const request = jest.fn()
            .mockResolvedValueOnce(answer(60))
            .mockRejectedValueOnce(new Error('down'))
            .mockResolvedValue(answer(60, 2));
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        setVisibility('hidden');
        await tick(5 * 60_000);
        setVisibility('visible');
        await tick(0);
        expect(request).toHaveBeenCalledTimes(2);
        await tick(120_000);
        expect(request).toHaveBeenCalledTimes(3);
        expect(results.map((r) => r.afterReturn)).toEqual([false, false]);
        poller.stop();
    });

    it('a return while a request is out starts no second one and flags nothing', async () => {
        let resolve!: (value: PollOutcome<number>) => void;
        const request = jest.fn().mockImplementation(() => new Promise((r) => { resolve = r; }));
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        setVisibility('hidden');
        await tick(20_000);
        setVisibility('visible');
        resolve(answer(60, 6));
        await tick(0);
        expect(request).toHaveBeenCalledTimes(1);
        expect(results).toEqual([{value: 6, afterReturn: false}]);
        poller.stop();
    });

    it('start() on a running poller with a request out sends nothing and keeps the back-off', async () => {
        let resolve!: (value: PollOutcome<number>) => void;
        const request = jest.fn().mockImplementation(() => new Promise((r) => { resolve = r; }));
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        poller.start();
        await tick(5 * 60_000);
        expect(request).toHaveBeenCalledTimes(1);
        resolve(answer(60, 7));
        await tick(0);
        expect(results).toHaveLength(1);
        poller.stop();
    });

    it('sends nothing while hidden even when a timer was due', async () => {
        const request = jest.fn().mockResolvedValue(answer(60));
        const {poller} = make(request);
        poller.start();
        await tick(30_000);
        setVisibility('hidden');
        await tick(120_000);
        expect(request).not.toHaveBeenCalled();
        expect(jest.getTimerCount()).toBe(0);
        poller.stop();
    });

    it('does not start a timer when started in a hidden tab', async () => {
        visibility = 'hidden';
        const request = jest.fn().mockResolvedValue(answer(60));
        const {poller} = make(request);
        poller.start();
        expect(jest.getTimerCount()).toBe(0);
        await tick(300_000);
        expect(request).not.toHaveBeenCalled();
        setVisibility('visible');
        await tick(0);
        expect(request).toHaveBeenCalledTimes(1);
        poller.stop();
    });

    it('takes an answer that was already on its way when the tab hid, and arms nothing', async () => {
        let resolve!: (value: PollOutcome<number>) => void;
        const request = jest.fn().mockImplementation(() => new Promise((r) => { resolve = r; }));
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        setVisibility('hidden');
        resolve(answer(60, 9));
        await tick(0);
        expect(results).toEqual([{value: 9, afterReturn: false}]);
        expect(jest.getTimerCount()).toBe(0);
        poller.stop();
    });

    it('is single flight: a return while a request is out sends no second one', async () => {
        let resolve!: (value: PollOutcome<number>) => void;
        const request = jest.fn().mockImplementation(() => new Promise((r) => { resolve = r; }));
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(1);
        setVisibility('hidden');
        setVisibility('visible');
        await tick(500_000 - 60_000);
        expect(request).toHaveBeenCalledTimes(1);
        resolve(answer(60, 3));
        await tick(0);
        expect(results).toHaveLength(1);
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(2);
        poller.stop();
    });

    it('backs off by doubling to 10 minutes after failures and recovers on success', async () => {
        const request = jest.fn()
            .mockRejectedValueOnce(new Error('boom'))
            .mockRejectedValueOnce(new Error('boom'))
            .mockRejectedValueOnce(new Error('boom'))
            .mockResolvedValue(answer(60));
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(1);
        await tick(119_000);
        expect(request).toHaveBeenCalledTimes(1);
        await tick(1_000);
        expect(request).toHaveBeenCalledTimes(2);
        await tick(239_000);
        expect(request).toHaveBeenCalledTimes(2);
        await tick(1_000);
        expect(request).toHaveBeenCalledTimes(3);
        // Third failure: 60 s * 2^3 = 480 s.
        await tick(479_000);
        expect(request).toHaveBeenCalledTimes(3);
        await tick(1_000);
        expect(request).toHaveBeenCalledTimes(4);
        expect(results).toHaveLength(1);
        // Success resets the wait.
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(5);
        poller.stop();
    });

    it('caps the back-off at 10 minutes', async () => {
        const request = jest.fn().mockRejectedValue(new Error('down'));
        const {poller} = make(request);
        poller.start();
        let elapsed = 60_000;
        await tick(elapsed);
        // 120, 240, 480, then 600 (capped), 600.
        for (const wait of [120_000, 240_000, 480_000, 600_000, 600_000]) {
            const before = request.mock.calls.length;
            await tick(wait - 1_000);
            expect(request.mock.calls.length).toBe(before);
            await tick(1_000);
            expect(request.mock.calls.length).toBe(before + 1);
            elapsed += wait;
        }
        poller.stop();
    });

    it('stops until the next start on a PollStop and sends no more', async () => {
        const request = jest.fn().mockRejectedValue(new PollStop());
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(1);
        expect(jest.getTimerCount()).toBe(0);
        await tick(600_000);
        setVisibility('hidden');
        setVisibility('visible');
        await tick(600_000);
        expect(request).toHaveBeenCalledTimes(1);
        expect(results).toEqual([]);
        request.mockResolvedValue(answer(60, 4));
        poller.start();
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(2);
        expect(results).toEqual([{value: 4, afterReturn: false}]);
        poller.stop();
    });

    it('stops when the server answers null, after delivering that answer', async () => {
        const request = jest.fn().mockResolvedValue(answer(null, 8));
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        expect(results).toEqual([{value: 8, afterReturn: false}]);
        expect(jest.getTimerCount()).toBe(0);
        await tick(600_000);
        expect(request).toHaveBeenCalledTimes(1);
        poller.stop();
    });

    it('leaves no timer and no listener after stop()', async () => {
        const removed = jest.spyOn(document, 'removeEventListener');
        const request = jest.fn().mockResolvedValue(answer(60));
        const {poller} = make(request);
        poller.start();
        expect(jest.getTimerCount()).toBe(1);
        poller.stop();
        expect(jest.getTimerCount()).toBe(0);
        expect(removed).toHaveBeenCalledWith('visibilitychange', expect.any(Function));
        setVisibility('hidden');
        setVisibility('visible');
        await tick(600_000);
        expect(request).not.toHaveBeenCalled();
        removed.mockRestore();
    });

    it('drops the answer to a request that was out when stop() was called, and aborts it', async () => {
        let resolve!: (value: PollOutcome<number>) => void;
        let signal!: AbortSignal;
        const request = jest.fn().mockImplementation((s: AbortSignal) => {
            signal = s;
            return new Promise((r) => { resolve = r; });
        });
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        poller.stop();
        expect(signal.aborted).toBe(true);
        resolve(answer(60, 5));
        await tick(0);
        expect(results).toEqual([]);
        expect(jest.getTimerCount()).toBe(0);
    });

    it('drops a failure that arrives after stop() without arming a timer', async () => {
        let reject!: (reason: Error) => void;
        const request = jest.fn().mockImplementation(() => new Promise((_, r) => { reject = r; }));
        const {poller} = make(request);
        poller.start();
        await tick(60_000);
        poller.stop();
        reject(new Error('late'));
        await tick(0);
        expect(jest.getTimerCount()).toBe(0);
    });

    it('can be started again after stop() and a second start() is a no-op', async () => {
        const request = jest.fn().mockResolvedValue(answer(60));
        const {poller} = make(request);
        poller.start();
        poller.start();
        expect(jest.getTimerCount()).toBe(1);
        poller.stop();
        poller.start();
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(1);
        poller.stop();
    });

    it('ignores visibility changes while stopped', async () => {
        const request = jest.fn().mockResolvedValue(answer(60));
        const {poller} = make(request);
        poller.start();
        poller.stop();
        // The listener is gone; a stray call path must not start anything.
        setVisibility('hidden');
        setVisibility('visible');
        expect(jest.getTimerCount()).toBe(0);
    });

    // Issue #485 review (rounds 2 and 3): AC 9 amended by the owner on
    // 2026-10-17: at most one scheduled check per interval plus at most one
    // early check on return per interval (no early check during back-off).
    describe('the bound on the check at the return', () => {
        async function flip(times: number, hiddenMs = 1_000, visibleMs = 10_000) {
            for (let i = 0; i < times; i += 1) {
                setVisibility('hidden');
                await tick(hiddenMs);
                setVisibility('visible');
                await tick(visibleMs);
            }
        }

        it('tab flips every 11 s for 10 minutes send at most two checks per interval', async () => {
            const request = jest.fn().mockResolvedValue(answer(60));
            const {poller} = make(request);
            poller.start();
            await flip(Math.floor((10 * 60_000) / 11_000));
            // One timer check and at most one early check per 60 s: 10 + 10 at
            // most (the unbounded rule sent 54).
            expect(request.mock.calls.length).toBeLessThanOrEqual(20);
            expect(request.mock.calls.length).toBeGreaterThanOrEqual(10);
            poller.stop();
        });

        it('a larger interval named by the server limits the early checks too', async () => {
            const request = jest.fn().mockResolvedValue(answer(600));
            const {poller} = make(request, 600);
            poller.start();
            await flip(Math.floor((10 * 60_000) / 11_000));
            // 600 s interval: one timer check and one early check in 10 minutes.
            expect(request.mock.calls.length).toBeLessThanOrEqual(2);
            poller.stop();
        });

        it('a failing server gets no early check while the back-off is pending', async () => {
            const request = jest.fn().mockRejectedValue(new Error('503'));
            const {poller} = make(request);
            poller.start();
            await tick(60_000);
            expect(request).toHaveBeenCalledTimes(1);
            // The wait is now 120 s; 5 minutes of flips may send only what the
            // doubling allows (at 180 s and at 420 s), never one per return.
            await flip(Math.floor((5 * 60_000) / 11_000));
            expect(request.mock.calls.length).toBeLessThanOrEqual(3);
            poller.stop();
        });

        it('a second early check waits for a whole interval after the first', async () => {
            const request = jest.fn().mockResolvedValue(answer(60));
            const {poller, results} = make(request);
            poller.start();
            await tick(15_000);
            setVisibility('hidden');
            await tick(1_000);
            setVisibility('visible');
            await tick(0);
            expect(request).toHaveBeenCalledTimes(1);
            await tick(15_000);
            setVisibility('hidden');
            await tick(1_000);
            setVisibility('visible');
            await tick(0);
            // 16 s after the first early check: no second one.
            expect(request).toHaveBeenCalledTimes(1);
            expect(results.map((r) => r.afterReturn)).toEqual([true]);
            poller.stop();
        });

        it('a second early check waits a whole interval, not half of one', async () => {
            const request = jest.fn().mockResolvedValue(answer(60));
            const {poller} = make(request);
            poller.start();
            await tick(15_000);
            setVisibility('hidden');
            await tick(1_000);
            setVisibility('visible');
            await tick(0);
            expect(request).toHaveBeenCalledTimes(1);
            // 35 s after the first early check, 35 s after the last check: still inside the interval.
            await tick(34_000);
            setVisibility('hidden');
            await tick(1_000);
            setVisibility('visible');
            await tick(0);
            expect(request).toHaveBeenCalledTimes(1);
            // The timer's check, a whole interval after the early one.
            await tick(25_000);
            expect(request).toHaveBeenCalledTimes(2);
            poller.stop();
        });
    });

    it('start() forgets the early check of an earlier run', async () => {
        const request = jest.fn().mockResolvedValue(answer(600));
        const {poller} = make(request, 600);
        poller.start();
        await tick(20_000);
        setVisibility('hidden');
        await tick(1_000);
        setVisibility('visible');
        await tick(0);
        expect(request).toHaveBeenCalledTimes(1);
        poller.stop();
        // A new run (a sign-in, a header refresh) is a fresh start: its first
        // return after 10 s may ask early again.
        poller.start();
        await tick(20_000);
        setVisibility('hidden');
        await tick(1_000);
        setVisibility('visible');
        await tick(0);
        expect(request).toHaveBeenCalledTimes(2);
        poller.stop();
    });

    it('a success resets the back-off: the next failure waits 2 minutes again', async () => {
        const request = jest.fn()
            .mockRejectedValueOnce(new Error('boom'))
            .mockRejectedValueOnce(new Error('boom'))
            .mockResolvedValueOnce(answer(60))
            .mockRejectedValueOnce(new Error('boom'))
            .mockResolvedValue(answer(60));
        const {poller} = make(request);
        poller.start();
        await tick(60_000 + 120_000 + 240_000);
        expect(request).toHaveBeenCalledTimes(3);
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(4);
        await tick(119_000);
        expect(request).toHaveBeenCalledTimes(4);
        await tick(1_000);
        expect(request).toHaveBeenCalledTimes(5);
        poller.stop();
    });

    it('a flip back within seconds of start() waits for the timer', async () => {
        const request = jest.fn().mockResolvedValue(answer(60));
        const {poller} = make(request);
        await tick(5 * 60_000);
        poller.start();
        await tick(2_000);
        setVisibility('hidden');
        await tick(3_000);
        setVisibility('visible');
        await tick(0);
        expect(request).not.toHaveBeenCalled();
        await tick(55_000);
        expect(request).toHaveBeenCalledTimes(1);
        poller.stop();
    });

    it('a return after the back-off delay has passed asks at once', async () => {
        const request = jest.fn()
            .mockRejectedValueOnce(new Error('boom'))
            .mockResolvedValue(answer(60));
        const {poller, results} = make(request);
        poller.start();
        await tick(60_000);
        expect(request).toHaveBeenCalledTimes(1);
        setVisibility('hidden');
        await tick(140_000);
        setVisibility('visible');
        await tick(0);
        expect(request).toHaveBeenCalledTimes(2);
        expect(results).toEqual([{value: 1, afterReturn: true}]);
        poller.stop();
    });
});
