// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {
    SUGGEST_COMPANY_EVENT,
    closeSuggestCompany,
    getSuggestCompanySnapshot,
    installSuggestCompanyBridge,
    openSuggestCompany,
    subscribeSuggestCompany,
} from './controller';

describe('suggestCompany controller', () => {
    afterEach(() => {
        // Reset the module-level singleton between tests.
        closeSuggestCompany();
    });

    test('initial snapshot is closed with no context', () => {
        expect(getSuggestCompanySnapshot()).toEqual({open: false, context: null});
    });

    test('openSuggestCompany with no argument opens with source assistant', () => {
        openSuggestCompany();
        expect(getSuggestCompanySnapshot()).toEqual({open: true, context: {source: 'assistant'}});
    });

    test('openSuggestCompany sets state and notifies subscribers exactly once', () => {
        const listener = jest.fn();
        const unsubscribe = subscribeSuggestCompany(listener);

        openSuggestCompany({source: 'rankings', searchTerm: 'acme', page: 2});

        expect(listener).toHaveBeenCalledTimes(1);
        expect(getSuggestCompanySnapshot()).toEqual({
            open: true,
            context: {source: 'rankings', searchTerm: 'acme', page: 2},
        });

        unsubscribe();
    });

    test('closeSuggestCompany clears open and notifies', () => {
        openSuggestCompany({source: 'rankings'});
        const listener = jest.fn();
        const unsubscribe = subscribeSuggestCompany(listener);

        closeSuggestCompany();

        expect(listener).toHaveBeenCalledTimes(1);
        expect(getSuggestCompanySnapshot().open).toBe(false);

        unsubscribe();
    });

    test('closeSuggestCompany is a no-op (no notification) when already closed', () => {
        const listener = jest.fn();
        const unsubscribe = subscribeSuggestCompany(listener);

        closeSuggestCompany();

        expect(listener).not.toHaveBeenCalled();
        unsubscribe();
    });

    test('subscribeSuggestCompany returns a working unsubscribe', () => {
        const listener = jest.fn();
        const unsubscribe = subscribeSuggestCompany(listener);
        unsubscribe();

        openSuggestCompany({source: 'rankings'});

        expect(listener).not.toHaveBeenCalled();
    });

    describe('bridge normalization', () => {
        let teardown: () => void;

        beforeEach(() => {
            teardown = installSuggestCompanyBridge();
        });

        afterEach(() => {
            teardown();
        });

        const dispatch = (detail?: unknown) => {
            window.dispatchEvent(new CustomEvent(SUGGEST_COMPANY_EVENT, {detail}));
        };

        test('no detail opens with source assistant', () => {
            dispatch(undefined);
            expect(getSuggestCompanySnapshot()).toEqual({open: true, context: {source: 'assistant'}});
        });

        test('detail: null opens with source assistant', () => {
            dispatch(null);
            expect(getSuggestCompanySnapshot()).toEqual({open: true, context: {source: 'assistant'}});
        });

        test('detail: a non-object string opens with source assistant', () => {
            dispatch('not-an-object');
            expect(getSuggestCompanySnapshot()).toEqual({open: true, context: {source: 'assistant'}});
        });

        test('an unknown key is dropped', () => {
            dispatch({source: 'rankings', bogus: 'value'});
            const context = getSuggestCompanySnapshot().context;
            expect(context).toEqual({source: 'rankings'});
            expect(context).not.toHaveProperty('bogus');
        });

        test('a non-integer page is dropped', () => {
            dispatch({source: 'rankings', page: '2'});
            expect(getSuggestCompanySnapshot().context).toEqual({source: 'rankings'});
        });

        test('a companyName longer than the cap is truncated', () => {
            const longName = 'x'.repeat(500);
            dispatch({source: 'job_results', companyName: longName});
            const companyName = getSuggestCompanySnapshot().context?.companyName;
            expect(companyName).toHaveLength(200);
            expect(companyName).toBe('x'.repeat(200));
        });

        test('a fully valid detail is preserved', () => {
            dispatch({
                source: 'company_details',
                companyName: 'Acme',
                searchTerm: 'acme',
                page: 3,
                organizationId: 42,
            });
            expect(getSuggestCompanySnapshot().context).toEqual({
                source: 'company_details',
                companyName: 'Acme',
                searchTerm: 'acme',
                page: 3,
                organizationId: 42,
            });
        });

        test('an unrecognized source falls back to assistant', () => {
            dispatch({source: 'not-a-real-source'});
            expect(getSuggestCompanySnapshot().context).toEqual({source: 'assistant'});
        });
    });

    test('installSuggestCompanyBridge teardown removes the window listener', () => {
        const teardown = installSuggestCompanyBridge();
        teardown();
        const before = getSuggestCompanySnapshot();

        window.dispatchEvent(new CustomEvent(SUGGEST_COMPANY_EVENT, {detail: {source: 'rankings'}}));

        expect(getSuggestCompanySnapshot()).toEqual(before);
    });
});
