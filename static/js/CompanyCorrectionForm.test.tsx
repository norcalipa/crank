// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {act, cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import * as React from 'react';
import CompanyCorrectionForm, {describeCurrentValue} from './CompanyCorrectionForm';
import {clearProvenanceCache, getCachedProvenance, setCachedProvenance} from './provenanceCache';
import type {SuggestCompanyContext} from './suggestCompany/controller';
import {closeAssistant, getWorkspaceSnapshot, openAssistant, resetWorkspaceForTests} from './workspace/store';

const PROVENANCE = {
    fields: [
        {field_key: 'rto_policy', value: 'Remote-first', stale: false, last_verified_at: '2025-01-10T12:00:00Z'},
        {field_key: 'locations', value: 'Austin', stale: true, last_verified_at: '2024-01-10T12:00:00Z'},
    ],
    unverified_fields: ['funding_round'],
};

const jsonResponse = (status: number, body: unknown, headers: Record<string, string> = {}) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: {get: (name: string) => headers[name] ?? null},
    json: () => Promise.resolve(body),
});

const SAVED = {
    id: 5, field_label: 'RTO policy', proposed_value: 'Hybrid', current_value: 'Remote-first',
    status: 'pending', status_label: 'Pending review',
};

let postResponse: () => Promise<unknown>;
let provenanceResponse: () => Promise<unknown>;

const posts = () => (global.fetch as jest.Mock).mock.calls.filter(call => call[0] === '/api/company-corrections/');

const baseContext: SuggestCompanyContext = {
    kind: 'correction', source: 'company_details', organizationId: 1, companyName: 'Acme', fieldKey: 'rto_policy',
};

const renderForm = (context: SuggestCompanyContext = baseContext, onClose = jest.fn()) => {
    const view = render(<CompanyCorrectionForm context={context} onClose={onClose}/>);
    return {...view, onClose};
};

const fillAndSubmit = async (proposed = 'Hybrid', url = 'https://example.com/policy') => {
    await screen.findByTestId('correction-current-text');
    fireEvent.change(screen.getByTestId('correction-proposed-value'), {target: {value: proposed}});
    fireEvent.change(screen.getByTestId('correction-evidence-url'), {target: {value: url}});
    fireEvent.click(screen.getByTestId('correction-submit'));
};

describe('CompanyCorrectionForm', () => {
    beforeEach(() => {
        clearProvenanceCache();
        postResponse = () => Promise.resolve(jsonResponse(201, SAVED));
        provenanceResponse = () => Promise.resolve(jsonResponse(200, PROVENANCE));
        global.fetch = jest.fn().mockImplementation((url: string) =>
            url.includes('/provenance/') ? provenanceResponse() : postResponse());
        document.cookie = 'csrftoken=tok123';
    });

    afterEach(() => {
        jest.useRealTimers();
    });

    describe('current value states', () => {
        test('shows a busy skeleton while loading', async () => {
            provenanceResponse = () => new Promise(() => {});
            renderForm();
            const skeleton = screen.getByTestId('correction-current-loading');
            expect(skeleton).toHaveAttribute('aria-busy', 'true');
            expect(skeleton).toHaveTextContent('Loading the current value');
        });

        test('shows the verified value with its last verified date', async () => {
            renderForm({...baseContext, fieldKey: 'rto_policy'});
            const text = await screen.findByTestId('correction-current-text');
            expect(text).toHaveTextContent(/Remote-first.*last verified/);
            expect(text).not.toHaveTextContent(/stale/i);
        });

        test('marks a stale value in text', async () => {
            renderForm({...baseContext, fieldKey: 'locations'});
            expect(await screen.findByTestId('correction-current-text')).toHaveTextContent(/Austin.*last verified.*Stale/);
        });

        test('says so when there is no verified value and invites a first suggestion', async () => {
            renderForm({...baseContext, fieldKey: 'funding_round'});
            expect(await screen.findByTestId('correction-current-text')).toHaveTextContent('No verified value on record');
            expect(screen.getByTestId('correction-current-value')).toHaveTextContent('could be the first');
        });

        test('changing the field updates the current value', async () => {
            renderForm({...baseContext, fieldKey: 'rto_policy'});
            await screen.findByTestId('correction-current-text');
            fireEvent.change(screen.getByTestId('correction-field'), {target: {value: 'funding_round'}});
            expect(screen.getByTestId('correction-current-text')).toHaveTextContent('No verified value on record');
        });

        test('defaults to the first field when the context names none', async () => {
            renderForm();
            await screen.findByTestId('correction-current-text');
            expect(screen.getByTestId('correction-field')).toHaveValue('rto_policy');
        });

        test.each([
            ['a failed request', () => Promise.resolve(jsonResponse(500, {}))],
            ['a network error', () => Promise.reject(new Error('offline'))],
        ])('shows "Current value unavailable" with a retry on %s', async (_name, respond) => {
            provenanceResponse = respond;
            renderForm();
            expect(await screen.findByTestId('correction-current-text')).toHaveTextContent('Current value unavailable');
            provenanceResponse = () => Promise.resolve(jsonResponse(200, PROVENANCE));
            fireEvent.click(screen.getByTestId('correction-current-retry'));
            await waitFor(() => expect(screen.getByTestId('correction-current-text')).toHaveTextContent(/Remote-first/));
            expect(screen.queryByTestId('correction-current-retry')).toBeNull();
        });

        test('ignores a provenance response that arrives after unmount', async () => {
            let resolve: (value: unknown) => void = () => {};
            provenanceResponse = () => new Promise(r => { resolve = r; });
            const {unmount} = renderForm();
            unmount();
            await act(async () => {
                resolve(jsonResponse(200, PROVENANCE));
            });
        });

        test('ignores a provenance failure that arrives after unmount', async () => {
            let reject: (reason: unknown) => void = () => {};
            provenanceResponse = () => new Promise((_, r) => { reject = r; });
            const {unmount} = renderForm();
            unmount();
            await act(async () => {
                reject(new Error('late'));
            });
        });

        test('keeps the cached current value when the background refresh fails', async () => {
            setCachedProvenance(1, PROVENANCE);
            provenanceResponse = () => Promise.reject(new Error('offline'));
            renderForm();
            await waitFor(() => expect(global.fetch).toHaveBeenCalledWith('/api/organizations/1/provenance/'));
            await act(async () => {});
            expect(screen.getByTestId('correction-current-text')).toHaveTextContent('Remote-first');
            expect(screen.queryByTestId('correction-current-retry')).toBeNull();
        });

        test('describeCurrentValue treats a ready payload without the field or arrays as unavailable', () => {
            expect(describeCurrentValue({status: 'ready', data: {}}, 'rto_policy').kind).toBe('unavailable');
            expect(describeCurrentValue({status: 'unavailable'}, 'rto_policy').kind).toBe('unavailable');
            expect(describeCurrentValue({status: 'loading'}, 'rto_policy').kind).toBe('loading');
        });

        test('a verified row with no verification date reads "Unknown"', () => {
            const result = describeCurrentValue({status: 'ready', data: {fields: [
                {field_key: 'rto_policy', value: 'X', stale: false, last_verified_at: null},
            ]}}, 'rto_policy');
            expect(result.text).toBe('X · last verified Unknown');
        });
    });

    describe('form structure', () => {
        test('is a labelled modal dialog with every required test id', async () => {
            renderForm();
            await screen.findByTestId('correction-current-text');
            const dialog = screen.getByRole('dialog', {name: 'Suggest a correction Acme'});
            expect(dialog).toHaveAttribute('aria-modal', 'true');
            expect(screen.getByRole('heading', {name: 'Suggest a correction'})).toBeInTheDocument();
            for (const id of ['correction-form', 'correction-field', 'correction-current-value',
                'correction-proposed-value', 'correction-evidence-url', 'correction-scope-level',
                'correction-submit']) {
                expect(screen.getByTestId(id)).toBeInTheDocument();
            }
            expect(screen.getByLabelText('Suggested value')).toBe(screen.getByTestId('correction-proposed-value'));
            expect(screen.queryByTestId('correction-scope-value')).toBeNull();
        });

        test('falls back to a generic title when the company name is unknown', async () => {
            renderForm({kind: 'correction', source: 'assistant', organizationId: 1});
            expect(await screen.findByRole('dialog', {name: 'Suggest a correction this company'})).toBeInTheDocument();
        });

        test('pins Cancel and Submit in a footer outside the scrolling body, tied to the form', async () => {
            renderForm();
            await screen.findByTestId('correction-current-text');
            const submit = screen.getByTestId('correction-submit');
            const footer = submit.closest('.modal-footer');
            expect(footer).not.toBeNull();
            expect(submit.closest('.modal-body')).toBeNull();
            expect(screen.getByTestId('correction-form').closest('.modal-body')).not.toBeNull();
            expect(submit).toHaveAttribute('type', 'submit');
            expect(submit).toHaveAttribute('form', 'correction-form');
            expect(footer).toContainElement(screen.getByRole('button', {name: 'Cancel'}));
        });

        test('the evidence field has help text instead of a placeholder URL', async () => {
            renderForm();
            await screen.findByTestId('correction-current-text');
            const url = screen.getByTestId('correction-evidence-url');
            expect(url).not.toHaveAttribute('placeholder');
            expect(url).toHaveAttribute('aria-describedby', 'correction-evidence-help');
            expect(document.getElementById('correction-evidence-help')).toHaveTextContent(/Must start with https:\/\//);
        });

        test('without a field in the context the select starts on a prompt and submit demands a choice', async () => {
            renderForm({kind: 'correction', source: 'assistant', organizationId: 1, companyName: 'Acme'});
            await screen.findByTestId('correction-current-text');
            const select = screen.getByTestId('correction-field');
            expect(select).toHaveValue('');
            expect(screen.getByRole('option', {name: 'Choose what to correct…'})).toBeDisabled();
            expect(screen.getByTestId('correction-current-text')).toHaveTextContent('Choose a field to see its current value');
            await waitFor(() => expect(screen.getByTestId('correction-close')).toHaveFocus());
            fireEvent.click(screen.getByTestId('correction-submit'));
            expect(await screen.findByTestId('correction-error')).toHaveTextContent('Please correct the highlighted fields.');
            expect(select).toHaveClass('is-invalid');
            expect(select).toHaveFocus();
            expect(screen.getByTestId('correction-proposed-value')).toHaveClass('is-invalid');
            expect(screen.getByTestId('correction-evidence-url')).toHaveClass('is-invalid');
            expect(document.getElementById('correction-proposed_value-error')).toHaveTextContent('Enter the corrected value.');
            expect(document.getElementById('correction-evidence_url-error'))
                .toHaveTextContent('Add a public link that starts with https://.');
            expect(document.getElementById('correction-field_key-error')).toHaveTextContent('Choose what to correct.');
            expect(posts()).toHaveLength(0);
        });

        test('shows where the current value came from', async () => {
            provenanceResponse = () => Promise.resolve(jsonResponse(200, {fields: [
                {field_key: 'rto_policy', value: 'Remote-first', stale: false,
                 last_verified_at: '2025-01-10T12:00:00Z', source_domain: 'acme.example'},
            ]}));
            renderForm();
            expect(await screen.findByTestId('correction-current-text')).toHaveTextContent(
                /Remote-first\s*acme\.example, last verified/);
            expect(screen.getByText('acme.example,')).toHaveClass('text-nowrap');
            expect(screen.getByText(/^last verified/)).toHaveClass('text-nowrap');
            expect(screen.getByTestId('correction-current-text').textContent).not.toContain('·');
        });

        test('scope detail appears for non-company scopes only', async () => {
            renderForm();
            await screen.findByTestId('correction-current-text');
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'location'}});
            expect(screen.getByTestId('correction-scope-value')).toBeInTheDocument();
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'company'}});
            expect(screen.queryByTestId('correction-scope-value')).toBeNull();
        });

        test('moves focus to Close after the next frame', async () => {
            renderForm();
            await waitFor(() => expect(screen.getByTestId('correction-close')).toHaveFocus());
        });

        test('a late frame does not steal focus already inside the dialog', async () => {
            const frames: FrameRequestCallback[] = [];
            const raf = jest.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
                frames.push(callback);
                return frames.length;
            });
            renderForm();
            screen.getByTestId('correction-proposed-value').focus();
            frames.forEach(callback => callback(0));
            expect(screen.getByTestId('correction-proposed-value')).toHaveFocus();
            expect(screen.getByTestId('correction-close')).not.toHaveFocus();
            raf.mockRestore();
        });

        test('cancels the pending focus when unmounted before the frame runs', async () => {
            const cancel = jest.spyOn(window, 'cancelAnimationFrame');
            const {unmount} = renderForm();
            unmount();
            expect(cancel).toHaveBeenCalled();
            cancel.mockRestore();
        });

        test('starts from the cached provenance without a loading state and refreshes it', async () => {
            renderForm();
            await screen.findByTestId('correction-current-text');
            expect(getCachedProvenance(1)).toEqual(PROVENANCE);
            cleanup();
            provenanceResponse = () => new Promise(() => {});
            renderForm();
            expect(screen.queryByTestId('correction-current-loading')).toBeNull();
            expect(screen.getByTestId('correction-current-text')).toHaveTextContent('Remote-first');
        });
    });

    describe('submitting', () => {
        test('posts the draft with the CSRF token and an idempotency key, then shows a Pending review status', async () => {
            renderForm({...baseContext, fieldKey: 'rto_policy'});
            await screen.findByTestId('correction-current-text');
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'role'}});
            fireEvent.change(screen.getByTestId('correction-scope-value'), {target: {value: 'Engineering'}});
            fireEvent.change(screen.getByLabelText('Note (optional)'), {target: {value: 'see page 2'}});
            fireEvent.change(screen.getByTestId('correction-proposed-value'), {target: {value: 'Hybrid'}});
            fireEvent.change(screen.getByTestId('correction-evidence-url'), {target: {value: 'https://example.com/p'}});
            fireEvent.click(screen.getByTestId('correction-submit'));

            const status = await screen.findByTestId('correction-status');
            expect(status).not.toHaveAttribute('role');
            expect(screen.getByTestId('correction-live')).toHaveAttribute('role', 'status');
            expect(status).toHaveTextContent('Pending review');
            expect(screen.getByTestId('correction-saved-proposed')).toHaveTextContent('Hybrid');
            expect(status).toHaveTextContent('Still in effect until review');
            expect(screen.getByTestId('correction-saved-current')).toHaveTextContent('Remote-first');
            expect(status).not.toHaveTextContent(/has been verified|is verified/i);
            expect(status).toHaveTextContent('Suggestion submitted');
            expect(status).toHaveTextContent('Your suggestion · RTO policy');
            expect(status.querySelector('.fa-circle-check')).not.toBeNull();
            expect(status.querySelector('.evidence-badge-pending')).toHaveTextContent('Pending review');

            const [url, init] = posts()[0];
            expect(url).toBe('/api/company-corrections/');
            expect(init.headers['X-CSRFToken']).toBe('tok123');
            const body = JSON.parse(init.body);
            expect(body).toEqual({
                organization_id: 1, field_key: 'rto_policy', proposed_value: 'Hybrid',
                evidence_url: 'https://example.com/p', scope: {level: 'role', value: 'Engineering'},
                note: 'see page 2', idempotency_key: expect.any(String),
            });
            expect(body.idempotency_key.length).toBeGreaterThan(8);
        });

        test('shows the no-verified-value line when the saved snapshot is empty', async () => {
            postResponse = () => Promise.resolve(jsonResponse(200, {...SAVED, current_value: ''}));
            renderForm();
            await fillAndSubmit();
            expect(await screen.findByTestId('correction-saved-current')).toHaveTextContent('No verified value on record');
        });

        test('falls back to a generated key when crypto.randomUUID is unavailable', async () => {
            const original = crypto.randomUUID;
            Object.defineProperty(crypto, 'randomUUID', {value: undefined, configurable: true});
            try {
                renderForm();
                await fillAndSubmit();
                await screen.findByTestId('correction-status');
                expect(JSON.parse(posts()[0][1].body).idempotency_key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
            } finally {
                Object.defineProperty(crypto, 'randomUUID', {value: original, configurable: true});
            }
        });

        test('client validation rejects a non-https link and a missing scope detail without posting', async () => {
            renderForm();
            await screen.findByTestId('correction-current-text');
            await waitFor(() => expect(screen.getByTestId('correction-close')).toHaveFocus());
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'team'}});
            fireEvent.change(screen.getByTestId('correction-proposed-value'), {target: {value: 'Hybrid'}});
            fireEvent.change(screen.getByTestId('correction-evidence-url'), {target: {value: 'http://example.com'}});
            fireEvent.click(screen.getByTestId('correction-submit'));
            expect(await screen.findByTestId('correction-error')).toBeInTheDocument();
            expect(document.getElementById('correction-evidence_url-error'))
                .toHaveTextContent('Use a link that starts with https://.');
            expect(document.getElementById('correction-scope_value-error'))
                .toHaveTextContent('Say which one this applies to.');
            expect(posts()).toHaveLength(0);
        });

        test('moves focus to the Back button after success', async () => {
            renderForm();
            await fillAndSubmit();
            await screen.findByTestId('correction-status');
            await waitFor(() => expect(screen.getByTestId('correction-back')).toHaveFocus());
        });

        test('two synchronous submits produce exactly one request and a Submitting… label', async () => {
            let finish: (value: unknown) => void = () => {};
            postResponse = () => new Promise(r => { finish = r; });
            renderForm();
            await screen.findByTestId('correction-current-text');
            const form = screen.getByTestId('correction-form');
            fireEvent.change(screen.getByTestId('correction-proposed-value'), {target: {value: 'Hybrid'}});
            fireEvent.change(screen.getByTestId('correction-evidence-url'), {target: {value: 'https://example.com/p'}});
            fireEvent.submit(form);
            fireEvent.submit(form);
            expect(posts()).toHaveLength(1);
            const submit = screen.getByTestId('correction-submit');
            expect(submit).toHaveAttribute('aria-disabled', 'true');
            expect(submit).not.toBeDisabled();
            expect(submit).toHaveTextContent('Submitting…');
            await act(async () => {
                finish(jsonResponse(201, SAVED));
            });
            expect(await screen.findByTestId('correction-status')).toBeInTheDocument();
        });

        test('field errors are linked to their inputs, focus the first invalid one and keep the draft', async () => {
            postResponse = () => Promise.resolve(jsonResponse(400, {
                error: 'Please correct the highlighted fields.',
                field_errors: {evidence_url: ['Use an https:// link.'], proposed_value: ['Enter the corrected value.']},
            }));
            renderForm();
            await fillAndSubmit('Hybrid', 'https://example.com');

            expect(await screen.findByTestId('correction-error')).toHaveAttribute('role', 'alert');
            const url = screen.getByTestId('correction-evidence-url');
            expect(url).toHaveAttribute('aria-invalid', 'true');
            expect(url).toHaveClass('is-invalid');
            expect(screen.getByTestId('correction-proposed-value')).toHaveClass('is-invalid');
            expect(screen.getByTestId('correction-field')).not.toHaveClass('is-invalid');
            expect(screen.getByTestId('correction-error').closest('.modal-footer')).not.toBeNull();
            expect(url).toHaveAttribute('aria-describedby', 'correction-evidence_url-error correction-evidence-help');
            expect(document.getElementById('correction-evidence_url-error')).toHaveTextContent('Use an https:// link.');
            expect(screen.getByTestId('correction-proposed-value')).toHaveFocus();
            expect(screen.getByTestId('correction-proposed-value')).toHaveValue('Hybrid');
            expect(url).toHaveValue('https://example.com');
            expect(screen.getByTestId('correction-submit')).not.toBeDisabled();
        });

        test('fixing a field clears its error and the summary without moving focus', async () => {
            renderForm();
            await screen.findByTestId('correction-current-text');
            fireEvent.click(screen.getByTestId('correction-submit'));
            const proposed = screen.getByTestId('correction-proposed-value');
            const url = screen.getByTestId('correction-evidence-url');
            await waitFor(() => expect(proposed).toHaveFocus());
            expect(screen.getByTestId('correction-error')).toHaveTextContent('Please correct the highlighted fields.');

            url.focus();
            fireEvent.change(url, {target: {value: 'http://example.com'}});
            expect(url).toHaveClass('is-invalid');
            fireEvent.change(proposed, {target: {value: '   '}});
            expect(proposed).toHaveClass('is-invalid');
            fireEvent.change(proposed, {target: {value: 'Hybrid'}});
            expect(proposed).not.toHaveClass('is-invalid');
            expect(document.getElementById('correction-proposed_value-error')).toBeNull();
            expect(screen.getByTestId('correction-error')).toBeInTheDocument();
            expect(url).toHaveFocus();
            fireEvent.change(url, {target: {value: 'https://example.com/p'}});
            expect(url).not.toHaveClass('is-invalid');
            expect(screen.queryByTestId('correction-error')).toBeNull();
            expect(url).toHaveFocus();
        });

        test('choosing a field and un-scoping clear their errors', async () => {
            renderForm({...baseContext, fieldKey: ''});
            await screen.findByTestId('correction-current-text');
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'team'}});
            fireEvent.click(screen.getByTestId('correction-submit'));
            expect(document.getElementById('correction-field_key-error')).toBeInTheDocument();
            expect(document.getElementById('correction-scope_value-error')).toBeInTheDocument();
            fireEvent.change(screen.getByTestId('correction-field'), {target: {value: 'funding_round'}});
            expect(document.getElementById('correction-field_key-error')).toBeNull();
            fireEvent.change(screen.getByTestId('correction-scope-value'), {target: {value: 'Platform'}});
            expect(document.getElementById('correction-scope_value-error')).toBeNull();
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'company'}});
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'team'}});
            fireEvent.change(screen.getByTestId('correction-scope-value'), {target: {value: ''}});
            fireEvent.click(screen.getByTestId('correction-submit'));
            expect(document.getElementById('correction-scope_value-error')).toBeInTheDocument();
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'company'}});
            expect(document.getElementById('correction-scope_value-error')).toBeNull();
        });

        test('shows only the first message per field', async () => {
            postResponse = () => Promise.resolve(jsonResponse(400, {
                error: 'Please correct the highlighted fields.',
                field_errors: {evidence_url: ['Add a public link that starts with https://.', 'Second message.']},
            }));
            renderForm();
            await fillAndSubmit();
            await screen.findByTestId('correction-error');
            const message = document.getElementById('correction-evidence_url-error');
            expect(message).toHaveTextContent('Add a public link that starts with https://.');
            expect(message).not.toHaveTextContent('Second message.');
        });

        test('errors on every field are announced', async () => {
            postResponse = () => Promise.resolve(jsonResponse(400, {
                error: 'Please correct the highlighted fields.',
                field_errors: {
                    field_key: ['a'], scope_level: ['b'], scope_value: ['c'], note: ['d'],
                },
            }));
            renderForm();
            await screen.findByTestId('correction-current-text');
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'team'}});
            fireEvent.change(screen.getByTestId('correction-proposed-value'), {target: {value: 'Hybrid'}});
            fireEvent.change(screen.getByTestId('correction-evidence-url'), {target: {value: 'https://example.com/p'}});
            fireEvent.change(screen.getByTestId('correction-scope-value'), {target: {value: 'Platform'}});
            fireEvent.click(screen.getByTestId('correction-submit'));
            await screen.findByTestId('correction-error');
            for (const name of ['field_key', 'scope_level', 'scope_value', 'note']) {
                expect(document.getElementById(`correction-${name}-error`)).toBeInTheDocument();
            }
            expect(screen.getByTestId('correction-field')).toHaveFocus();
        });

        test('a 400 without details shows the generic message', async () => {
            postResponse = () => Promise.resolve(jsonResponse(400, {}));
            renderForm();
            await fillAndSubmit();
            expect(await screen.findByTestId('correction-error')).toHaveTextContent('Something went wrong. Please try again.');
        });

        test('a rate limit with Retry-After locks Submit, focuses the alert and keeps the draft', async () => {
            postResponse = () => Promise.resolve(jsonResponse(429, {error: 'Too many.'}, {'Retry-After': '1800'}));
            renderForm();
            await fillAndSubmit();
            const alert = await screen.findByTestId('correction-error');
            expect(alert).toHaveTextContent('Hourly limit reached. Submit again in 30 min — keep this open.');
            expect(alert).toHaveClass('alert-warning');
            expect(screen.getByTestId('correction-submit')).toBeDisabled();
            expect(screen.getByTestId('correction-proposed-value')).toHaveValue('Hybrid');
            await waitFor(() => expect(alert).toHaveFocus());
        });

        test('Submit is re-enabled once the Retry-After time passes, with the draft intact', async () => {
            jest.useFakeTimers();
            postResponse = () => Promise.resolve(jsonResponse(429, {}, {'Retry-After': '90'}));
            renderForm();
            await act(async () => {
                await jest.advanceTimersByTimeAsync(0);
            });
            await fillAndSubmit();
            expect(await screen.findByTestId('correction-error')).toHaveTextContent('in 2 min');
            expect(screen.getByTestId('correction-submit')).toBeDisabled();
            await act(async () => {
                await jest.advanceTimersByTimeAsync(89000);
            });
            expect(screen.getByTestId('correction-submit')).toBeDisabled();
            await act(async () => {
                await jest.advanceTimersByTimeAsync(1500);
            });
            expect(screen.getByTestId('correction-submit')).not.toBeDisabled();
            expect(screen.queryByTestId('correction-error')).toBeNull();
            expect(screen.getByTestId('correction-proposed-value')).toHaveValue('Hybrid');
        });

        test('a rate limit without a usable Retry-After keeps Submit enabled', async () => {
            postResponse = () => Promise.resolve(jsonResponse(429, {}));
            renderForm();
            await fillAndSubmit();
            expect(await screen.findByTestId('correction-error'))
                .toHaveTextContent('Hourly limit reached. Please try again later.');
            expect(screen.getByTestId('correction-submit')).not.toBeDisabled();
            expect(screen.getByTestId('correction-error')).toHaveClass('alert-warning');
        });

        test('a generic failure focuses the alert and Submit keeps no stale lock', async () => {
            postResponse = () => Promise.resolve(jsonResponse(500, {}));
            renderForm();
            await fillAndSubmit();
            const alert = await screen.findByTestId('correction-error');
            await waitFor(() => expect(alert).toHaveFocus());
            expect(screen.getByTestId('correction-submit')).not.toBeDisabled();
        });

        test('Submit keeps focus while a submit is in flight', async () => {
            let finish: (value: unknown) => void = () => {};
            postResponse = () => new Promise(r => { finish = r; });
            renderForm();
            await screen.findByTestId('correction-current-text');
            fireEvent.change(screen.getByTestId('correction-proposed-value'), {target: {value: 'Hybrid'}});
            fireEvent.change(screen.getByTestId('correction-evidence-url'), {target: {value: 'https://example.com/p'}});
            const submit = screen.getByTestId('correction-submit');
            submit.focus();
            fireEvent.click(submit);
            expect(submit).toHaveFocus();
            await act(async () => {
                finish(jsonResponse(201, SAVED));
            });
        });

        test('a network failure keeps the draft and re-enables submit', async () => {
            postResponse = () => Promise.reject(new Error('offline'));
            renderForm();
            await fillAndSubmit();
            expect(await screen.findByTestId('correction-error')).toHaveTextContent('Network error');
            expect(screen.getByTestId('correction-evidence-url')).toHaveValue('https://example.com/policy');
            expect(screen.getByTestId('correction-submit')).not.toBeDisabled();
        });

        test('a signed-out submit shows the sign-in prompt', async () => {
            postResponse = () => Promise.resolve(jsonResponse(401, {error: 'Sign in'}));
            renderForm();
            await fillAndSubmit();
            expect(await screen.findByTestId('correction-auth-required')).toHaveTextContent('Sign in to suggest a correction.');
            expect(screen.getByRole('link', {name: 'Sign in'})).toHaveAttribute('href', '/accounts/login/');
        });

        test('a 409 shows the already-pending state', async () => {
            postResponse = () => Promise.resolve(jsonResponse(409, {error: 'exists', existing: {status: 'pending'}}));
            renderForm();
            await fillAndSubmit();
            const status = await screen.findByTestId('correction-status');
            expect(status).toHaveTextContent('You already suggested a change to this field — Pending review');
            expect(screen.getByTestId('correction-duplicate')).toBeInTheDocument();
        });
    });

    describe('navigation and focus', () => {
        const succeed = async (context: SuggestCompanyContext) => {
            const view = renderForm(context);
            await fillAndSubmit();
            await screen.findByTestId('correction-status');
            return view;
        };

        test('"Back to <Company>" closes then asks the list to reopen that company', async () => {
            const handler = jest.fn();
            window.addEventListener('crank:company-open', handler);
            const {onClose} = await succeed(baseContext);
            expect(screen.getByTestId('correction-back')).toHaveTextContent('Back to Acme');
            jest.useFakeTimers();
            fireEvent.click(screen.getByTestId('correction-back'));
            expect(onClose).toHaveBeenCalledTimes(1);
            expect(handler).not.toHaveBeenCalled();
            act(() => {
                jest.runAllTimers();
            });
            expect((handler.mock.calls[0][0] as CustomEvent).detail).toEqual({organizationId: 1});
            window.removeEventListener('crank:company-open', handler);
        });

        test.each(['job_results', 'assistant'] as const)('%s entries get "Back to results" and reopen nothing', async (source) => {
            const handler = jest.fn();
            window.addEventListener('crank:company-open', handler);
            const {onClose} = await succeed({...baseContext, source});
            expect(screen.getByTestId('correction-back')).toHaveTextContent('Back to results');
            jest.useFakeTimers();
            fireEvent.click(screen.getByTestId('correction-back'));
            act(() => {
                jest.runAllTimers();
            });
            expect(onClose).toHaveBeenCalledTimes(1);
            expect(handler).not.toHaveBeenCalled();
            window.removeEventListener('crank:company-open', handler);
        });

        test('the 409 state also offers a back action', async () => {
            postResponse = () => Promise.resolve(jsonResponse(409, {}));
            renderForm({...baseContext, source: 'company_evidence'});
            await fillAndSubmit();
            expect(await screen.findByTestId('correction-back')).toHaveTextContent('Back to Acme');
        });

        test('Escape, Close and Cancel each call onClose', async () => {
            const {onClose} = renderForm();
            await screen.findByTestId('correction-current-text');
            fireEvent.keyDown(document, {key: 'Escape'});
            fireEvent.click(screen.getByTestId('correction-close'));
            fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
            expect(onClose).toHaveBeenCalledTimes(3);
        });

        test('Escape does not reach document-level handlers such as the assistant drawer', async () => {
            const behind = jest.fn();
            document.addEventListener('keydown', behind);
            try {
                const {onClose} = renderForm();
                await screen.findByTestId('correction-current-text');
                fireEvent.keyDown(document, {key: 'Escape'});
                expect(onClose).toHaveBeenCalledTimes(1);
                expect(behind).not.toHaveBeenCalled();
            } finally {
                document.removeEventListener('keydown', behind);
            }
        });

        test('Tab and Shift+Tab are trapped inside the dialog', async () => {
            renderForm();
            await screen.findByTestId('correction-current-text');
            const close = screen.getByTestId('correction-close');
            const submit = screen.getByTestId('correction-submit');

            submit.focus();
            fireEvent.keyDown(document, {key: 'Tab'});
            expect(close).toHaveFocus();

            fireEvent.keyDown(document, {key: 'Tab', shiftKey: true});
            expect(submit).toHaveFocus();

            (document.activeElement as HTMLElement).blur();
            fireEvent.keyDown(document, {key: 'Tab'});
            expect(close).toHaveFocus();

            (document.activeElement as HTMLElement).blur();
            fireEvent.keyDown(document, {key: 'Tab', shiftKey: true});
            expect(submit).toHaveFocus();

            // a Tab in the middle is left to the browser
            screen.getByTestId('correction-proposed-value').focus();
            const event = new KeyboardEvent('keydown', {key: 'Tab', bubbles: true, cancelable: true});
            document.dispatchEvent(event);
            expect(event.defaultPrevented).toBe(false);
        });

        test('other keys are ignored', async () => {
            const {onClose} = renderForm();
            fireEvent.keyDown(document, {key: 'a'});
            expect(onClose).not.toHaveBeenCalled();
        });

        test('locks the background while open and restores focus to the opener on unmount', async () => {
            const opener = document.createElement('button');
            document.body.appendChild(opener);
            opener.focus();
            const {unmount} = renderForm();
            expect(document.body.style.overflow).toBe('hidden');
            unmount();
            expect(document.body.style.overflow).not.toBe('hidden');
            expect(opener).toHaveFocus();
        });

        test('falls back to the company entry when the opener is gone', async () => {
            const opener = document.createElement('button');
            document.body.appendChild(opener);
            const hidden = document.createElement('div');
            hidden.setAttribute('data-organization-id', '1');
            hidden.tabIndex = 0;
            document.body.appendChild(hidden);
            const row = document.createElement('div');
            row.setAttribute('data-organization-id', '1');
            row.tabIndex = 0;
            row.getBoundingClientRect = () => ({height: 20} as DOMRect);
            document.body.appendChild(row);
            opener.focus();
            const {unmount} = renderForm();
            opener.remove();
            unmount();
            expect(row).toHaveFocus();
            row.remove();
            hidden.remove();
        });

        test('blurs when neither the opener nor a company entry exists', async () => {
            const opener = document.createElement('button');
            document.body.appendChild(opener);
            opener.focus();
            const {unmount} = renderForm();
            opener.remove();
            const stray = document.createElement('input');
            document.body.appendChild(stray);
            stray.focus();
            expect(() => unmount()).not.toThrow();
            expect(opener).not.toHaveFocus();
            stray.remove();
        });

        test('cleanup with no active element does not throw', async () => {
            const {unmount} = renderForm();
            const spy = jest.spyOn(document, 'activeElement', 'get').mockReturnValue(null);
            expect(() => unmount()).not.toThrow();
            spy.mockRestore();
        });
    });

    describe('review round 1', () => {
        afterEach(() => resetWorkspaceForTests());

        test('reopens the phone assistant sheet and refocuses the opener on unmount', async () => {
            const opener = document.createElement('button');
            opener.setAttribute('data-testid', 'sheet-opener');
            opener.getBoundingClientRect = () => ({height: 20} as DOMRect);
            document.body.appendChild(opener);
            opener.focus();
            openAssistant();
            const {unmount} = renderForm();
            closeAssistant();
            opener.blur();
            expect(getWorkspaceSnapshot().visibility).toBe('closed');
            const raf = jest.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => {
                cb(0);
                return 0;
            });
            unmount();
            expect(getWorkspaceSnapshot().visibility).toBe('open');
            expect(opener).toHaveFocus();
            raf.mockRestore();
            opener.remove();
        });

        test('gives up refocusing when the opener never becomes visible', async () => {
            const opener = document.createElement('button');
            opener.setAttribute('data-testid', 'sheet-opener-2');
            document.body.appendChild(opener);
            opener.focus();
            openAssistant();
            const {unmount} = renderForm();
            closeAssistant();
            opener.remove();
            const raf = jest.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => {
                cb(0);
                return 0;
            });
            expect(() => unmount()).not.toThrow();
            expect(getWorkspaceSnapshot().visibility).toBe('open');
            raf.mockRestore();
        });

        describe('with two cards for the same company', () => {
            const makeCards = () => {
                const cards = [0, 1].map(() => {
                    const card = document.createElement('button');
                    card.setAttribute('data-testid', 'suggest-correction-org-1');
                    card.getBoundingClientRect = () => ({height: 20} as DOMRect);
                    document.body.appendChild(card);
                    return card;
                });
                return cards;
            };
            let raf: jest.SpyInstance;
            beforeEach(() => {
                raf = jest.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => {
                    cb(0);
                    return 0;
                });
            });
            afterEach(() => raf.mockRestore());

            test('refocuses the card that opened the form, not the first one', async () => {
                const cards = makeCards();
                cards[1].focus();
                openAssistant();
                const {unmount} = renderForm();
                closeAssistant();
                cards[1].blur();
                unmount();
                expect(cards[1]).toHaveFocus();
                cards.forEach(card => card.remove());
            });

            test('falls back to the first visible card once the opener cannot be found', async () => {
                const cards = makeCards();
                cards[1].focus();
                openAssistant();
                const {unmount} = renderForm();
                closeAssistant();
                cards[1].remove();
                unmount();
                expect(cards[0]).toHaveFocus();
                cards[0].remove();
            });
        });

        test('does not reopen a sheet that was not open', async () => {
            const {unmount} = renderForm();
            unmount();
            expect(getWorkspaceSnapshot().visibility).not.toBe('open');
        });

        test('shows the card value next to the verified evidence and notes a difference', async () => {
            provenanceResponse = () => Promise.resolve(jsonResponse(200, {
                ...PROVENANCE, displayed_values: {rto_policy: 'Hybrid (3 days)', locations: 'Austin'},
            }));
            renderForm({...baseContext, fieldKey: 'rto_policy'});
            const shown = await screen.findByTestId('correction-displayed-value');
            expect(shown).toHaveTextContent('Hybrid (3 days)');
            expect(shown).toHaveTextContent(/differ/i);
            fireEvent.change(screen.getByTestId('correction-field'), {target: {value: 'locations'}});
            expect(screen.getByTestId('correction-displayed-value')).not.toHaveTextContent(/differ/i);
        });

        test('offers no team scope', async () => {
            renderForm();
            await screen.findByTestId('correction-current-text');
            const options = Array.from(screen.getByTestId('correction-scope-level').querySelectorAll('option'))
                .map(o => (o as HTMLOptionElement).value);
            expect(options).not.toContain('team');
        });

        test('keeps the key for a retry of the same draft and rotates it when the draft changes', async () => {
            postResponse = () => Promise.resolve(jsonResponse(500, {}));
            renderForm();
            await fillAndSubmit('Hybrid');
            await waitFor(() => expect(posts()).toHaveLength(1));
            await waitFor(() => expect(screen.getByTestId('correction-submit')).not.toBeDisabled());
            fireEvent.click(screen.getByTestId('correction-submit'));
            await waitFor(() => expect(posts()).toHaveLength(2));
            await waitFor(() => expect(screen.getByTestId('correction-submit')).not.toBeDisabled());
            fireEvent.change(screen.getByTestId('correction-proposed-value'), {target: {value: 'Onsite'}});
            fireEvent.click(screen.getByTestId('correction-submit'));
            await waitFor(() => expect(posts()).toHaveLength(3));
            const keys = posts().map(call => JSON.parse(call[1].body).idempotency_key);
            expect(keys[0]).toBe(keys[1]);
            expect(keys[2]).not.toBe(keys[0]);
        });

        test('falls back to the field name when the response has no field_label', async () => {
            postResponse = () => Promise.resolve(jsonResponse(201, {...SAVED, field_label: ''}));
            renderForm();
            await fillAndSubmit();
            expect(await screen.findByTestId('correction-status')).toHaveTextContent('Your suggestion · RTO Policy');
        });

        test('mounting with no focused element still unmounts cleanly', async () => {
            const spy = jest.spyOn(document, 'activeElement', 'get').mockReturnValue(null);
            const {unmount} = renderForm();
            expect(() => unmount()).not.toThrow();
            spy.mockRestore();
        });

        test('labels the saved suggestion from the response field_label', async () => {
            postResponse = () => Promise.resolve(jsonResponse(201, {...SAVED, field_label: 'Funding stage'}));
            renderForm();
            await fillAndSubmit();
            expect(await screen.findByTestId('correction-status')).toHaveTextContent('Your suggestion · Funding stage');
        });

        test('announces the saved state once, in the live region', async () => {
            renderForm();
            await fillAndSubmit();
            await screen.findByTestId('correction-status');
            await waitFor(() => expect(screen.getByTestId('correction-live')).toHaveTextContent(/submitted/i));
            expect(screen.getByTestId('correction-back')).not.toHaveAttribute('aria-describedby');
        });

        test('blocks a second suggestion for a field that already has one pending', async () => {
            global.fetch = jest.fn().mockImplementation((url: string) => {
                if (url.includes('/provenance/')) {
                    return provenanceResponse();
                }
                if (url.includes('/api/company-corrections/?organization=')) {
                    return Promise.resolve(jsonResponse(200, {corrections: [
                        {id: 9, field_key: 'rto_policy', proposed_value: 'Onsite', status: 'pending'},
                        {id: 8, field_key: 'locations', proposed_value: 'Rome', status: 'accepted'},
                    ]}));
                }
                return postResponse();
            });
            renderForm({...baseContext, fieldKey: 'rto_policy'});
            const notice = await screen.findByTestId('correction-pending-notice');
            expect(notice).toHaveTextContent('Onsite');
            const submit = screen.getByTestId('correction-submit');
            expect(submit).toHaveAttribute('aria-disabled', 'true');
            expect(submit).not.toBeDisabled();
            expect(submit).toHaveAttribute('aria-describedby', 'correction-pending-notice');
            expect(notice).toHaveAttribute('role', 'status');
            const before = posts().length;
            fireEvent.click(submit);
            expect(posts()).toHaveLength(before);
            fireEvent.change(screen.getByTestId('correction-field'), {target: {value: 'locations'}});
            expect(screen.queryByTestId('correction-pending-notice')).toBeNull();
            expect(screen.getByTestId('correction-submit')).not.toHaveAttribute('aria-describedby');
        });
        test('offers role and location only when the field has no company-wide fact', async () => {
            renderForm({...baseContext, fieldKey: 'rto_policy'});
            await screen.findByTestId('correction-current-text');
            const level = screen.getByTestId('correction-scope-level') as HTMLSelectElement;
            expect(within(level).getByRole('option', {name: 'A role'})).toBeDisabled();
            expect(within(level).getByRole('option', {name: 'A location'})).toBeDisabled();
            expect(screen.getByTestId('correction-scope-help')).toHaveTextContent('company-wide fact');
            expect(level).toHaveAttribute('aria-describedby', 'correction-scope-help');
            fireEvent.change(screen.getByTestId('correction-field'), {target: {value: 'funding_round'}});
            expect(within(level).getByRole('option', {name: 'A role'})).not.toBeDisabled();
            expect(screen.queryByTestId('correction-scope-help')).toBeNull();
        });

        test('a scoped fact leaves scoped suggestions available and a blocked pick resets to company', async () => {
            provenanceResponse = () => Promise.resolve(jsonResponse(200, {
                fields: [{field_key: 'rto_policy', value: 'Hybrid', stale: false, last_verified_at: null,
                    scope: {countries: ['UK']}}],
                unverified_fields: ['funding_round'],
            }));
            renderForm({...baseContext, fieldKey: 'funding_round'});
            await screen.findByTestId('correction-current-text');
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'role'}});
            fireEvent.change(screen.getByTestId('correction-scope-value'), {target: {value: 'Engineer'}});
            fireEvent.change(screen.getByTestId('correction-field'), {target: {value: 'rto_policy'}});
            expect(screen.getByTestId('correction-scope-level')).toHaveValue('role');
            expect(screen.queryByTestId('correction-scope-help')).toBeNull();
            provenanceResponse = () => Promise.resolve(jsonResponse(200, PROVENANCE));
        });

        test('switching to a field with a company-wide fact resets a role scope to company', async () => {
            renderForm({...baseContext, fieldKey: 'funding_round'});
            await screen.findByTestId('correction-current-text');
            fireEvent.change(screen.getByTestId('correction-scope-level'), {target: {value: 'role'}});
            fireEvent.change(screen.getByTestId('correction-scope-value'), {target: {value: 'Engineer'}});
            fireEvent.change(screen.getByTestId('correction-field'), {target: {value: 'rto_policy'}});
            await waitFor(() => expect(screen.getByTestId('correction-scope-level')).toHaveValue('company'));
            expect(screen.queryByTestId('correction-scope-value')).toBeNull();
        });
    });
});
