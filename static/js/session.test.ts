// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {sessionEnded} from './session';

const res = (over: Partial<Pick<Response, 'status' | 'redirected' | 'url'>>) => ({
    status: 200, redirected: false, url: '', ...over,
});

describe('sessionEnded', () => {
    it('is true for a 401', () => {
        expect(sessionEnded(res({status: 401}), '/accounts/login/')).toBe(true);
    });

    it('is true for a followed redirect that ends on the sign-in path', () => {
        expect(sessionEnded(
            res({redirected: true, url: 'http://localhost/accounts/login/'}), '/accounts/login/',
        )).toBe(true);
    });

    it('ignores the query string of the landing url and of the sign-in url', () => {
        expect(sessionEnded(
            res({redirected: true, url: 'http://localhost/accounts/login/?next=/a/'}), '/accounts/login/?x=1',
        )).toBe(true);
    });

    it('is false for a redirect to another page', () => {
        expect(sessionEnded(
            res({redirected: true, url: 'http://localhost/elsewhere/'}), '/accounts/login/',
        )).toBe(false);
    });

    it('is false for a redirect with no final url', () => {
        expect(sessionEnded(res({redirected: true, url: ''}), '/accounts/login/')).toBe(false);
    });

    it('is false for an ordinary answer', () => {
        expect(sessionEnded(res({}), '/accounts/login/')).toBe(false);
    });

    it('is false when the landing url cannot be parsed', () => {
        expect(sessionEnded(
            res({redirected: true, url: 'http://['}), '/accounts/login/',
        )).toBe(false);
    });
});
