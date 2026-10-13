// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {act, cleanup, fireEvent, render, screen} from '@testing-library/react';
import * as React from 'react';

import * as store from '../workspace/store';
import JobsSection, {CompanyJob, CompanyJobs, jobMeta, payText, postingUrl} from './JobsSection';

const job = (overrides: Partial<CompanyJob> = {}): CompanyJob => ({
    id: 1, title: 'Staff Engineer', location_text: 'Remote (US)', is_remote: true,
    compensation_min: '150000.00', compensation_max: '190000.00', compensation_currency: 'USD',
    compensation_interval: 'year', canonical_url: 'https://jobs.example.test/1', ...overrides,
});

const jobs = (overrides: Partial<CompanyJobs> = {}): CompanyJobs => ({
    organization_id: 4, inventory: 'available', count: 1, results: [job()], ...overrides,
});

const answer = (body: unknown, extra: Record<string, unknown> = {}) => ({
    ok: true, status: 200, redirected: false, json: () => Promise.resolve(body), ...extra,
});

const renderJobs = (props: Partial<React.ComponentProps<typeof JobsSection>> = {}) => render(
    <JobsSection organizationId={4} name="Acme" isAuthenticated={true} signInUrl="/accounts/login/?next=x"
                 {...props}/>);

describe('JobsSection (issue #486, AC 17)', () => {
    beforeEach(() => {
        store.resetWorkspaceForTests();
        global.fetch = jest.fn().mockResolvedValue(answer(jobs()));
    });
    afterEach(cleanup);

    test('signed out: the sign-in prompt and no request', () => {
        renderJobs({isAuthenticated: false});
        expect(screen.getByTestId('jobs-sign-in')).toHaveTextContent('Sign in to see open jobs at Acme.');
        expect(screen.getByRole('link')).toHaveAttribute('href', '/accounts/login/?next=x');
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('signed out without an address, and with a long name', () => {
        renderJobs({isAuthenticated: false, signInUrl: null, name: 'y'.repeat(50)});
        expect(screen.getByTestId('jobs-sign-in')).toHaveTextContent('Sign in to see open jobs at this company.');
        expect(screen.queryByRole('link')).toBeNull();
    });

    test('loading, then a list with title, location, arrangement, pay and a posting link', async () => {
        let resolve: (value: unknown) => void = () => {};
        global.fetch = jest.fn().mockReturnValue(new Promise(r => {
            resolve = r;
        }));
        renderJobs();
        expect(screen.getByTestId('jobs-loading')).toHaveAttribute('aria-busy', 'true');
        expect(global.fetch).toHaveBeenCalledWith('/api/organizations/4/jobs/', expect.objectContaining({credentials: 'same-origin'}));
        await act(async () => {
            resolve(answer(jobs()));
        });
        const row = await screen.findByTestId('job-row-1');
        expect(row).toHaveTextContent('Staff Engineer');
        expect(row).toHaveTextContent('Remote (US) · USD 150,000–190,000 per year');
        const link = screen.getByTestId('job-link-1');
        expect(link).toHaveAttribute('href', 'https://jobs.example.test/1');
        expect(link).toHaveAttribute('target', '_blank');
        expect(link).toHaveAttribute('rel', 'noopener noreferrer');
        expect(link).toHaveTextContent('View posting');
        expect(screen.getByRole('heading', {level: 3, name: 'Open jobs'})).toBeInTheDocument();
    });

    test('a listing with no pay, no location and not remote', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(jobs({results: [job({
            compensation_min: null, compensation_max: null, location_text: '', is_remote: false,
        })]})));
        renderJobs();
        const row = await screen.findByTestId('job-row-1');
        expect(row).toHaveTextContent('Not listed as remote · Pay not published');
    });

    test('a javascript: address is shown without a link', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(jobs({results: [
            job({id: 1, canonical_url: 'javascript:alert(1)'}),
            job({id: 2, canonical_url: 'data:text/html,hi'}),
            job({id: 3, canonical_url: ''}),
            job({id: 4, canonical_url: 'http://ok.example.test/x'}),
        ]})));
        renderJobs();
        await screen.findByTestId('job-row-1');
        expect(screen.queryByTestId('job-link-1')).toBeNull();
        expect(screen.queryByTestId('job-link-2')).toBeNull();
        expect(screen.queryByTestId('job-link-3')).toBeNull();
        expect(screen.getByTestId('job-link-4')).toHaveAttribute('href', 'http://ok.example.test/x');
        expect(screen.getByTestId('job-row-1')).toHaveTextContent('Staff Engineer');
    });

    test('an empty list says no open roles, unavailable inventory says something else', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(jobs({count: 0, results: []})));
        renderJobs();
        expect(await screen.findByTestId('jobs-empty')).toHaveTextContent('No open roles listed.');
        cleanup();
        global.fetch = jest.fn().mockResolvedValue(answer(jobs({inventory: 'unavailable', count: 0, results: []})));
        renderJobs();
        const unavailable = await screen.findByTestId('jobs-unavailable');
        expect(unavailable).toHaveTextContent('Job listings are not available yet');
        expect(unavailable).toHaveTextContent('does not mean Acme is not hiring');
        expect(screen.queryByTestId('jobs-empty')).toBeNull();
    });

    test('an error is a fixed sentence with Retry that clears it', async () => {
        const fetchMock = jest.fn().mockResolvedValueOnce(answer({detail: 'secret'}, {ok: false, status: 500}))
            .mockResolvedValue(answer(jobs()));
        global.fetch = fetchMock;
        renderJobs();
        const error = await screen.findByTestId('jobs-error');
        expect(error).toHaveTextContent('Open jobs could not be loaded.');
        expect(document.body.textContent).not.toContain('secret');
        fireEvent.click(screen.getByTestId('jobs-retry'));
        await screen.findByTestId('job-row-1');
        expect(screen.queryByTestId('jobs-error')).toBeNull();
    });

    test('a redirect and a 401 read as sign in', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer('<html>', {redirected: true}));
        renderJobs();
        expect(await screen.findByTestId('jobs-sign-in')).toBeInTheDocument();
        cleanup();
        global.fetch = jest.fn().mockResolvedValue(answer({}, {ok: false, status: 401}));
        renderJobs();
        expect(await screen.findByTestId('jobs-sign-in')).toBeInTheDocument();
        expect(screen.queryByTestId('jobs-error')).toBeNull();
    });

    test('the heading is a focus target for View jobs', () => {
        const ref = React.createRef<HTMLHeadingElement>();
        render(<JobsSection ref={ref} organizationId={4} name="Acme" isAuthenticated={false} signInUrl={null}/>);
        expect(ref.current).toBe(screen.getByRole('heading', {name: 'Open jobs'}));
        ref.current!.focus();
        expect(ref.current).toHaveFocus();
    });

    test('an answer for company A is dropped after a switch to B, and a purge empties the list', async () => {
        const waiting: ((value: unknown) => void)[] = [];
        global.fetch = jest.fn().mockImplementation(() => new Promise(resolve => waiting.push(resolve)));
        const view = renderJobs({organizationId: 4});
        view.rerender(<JobsSection organizationId={5} name="B" isAuthenticated={true} signInUrl={null}/>);
        await act(async () => {
            waiting[0](answer(jobs({organization_id: 4, results: [job({id: 40, title: 'A job'})]})));
        });
        expect(screen.queryByText('A job')).toBeNull();
        await act(async () => {
            waiting[1](answer(jobs({organization_id: 5, results: [job({id: 50, title: 'B job'})]})));
        });
        expect(await screen.findByText('B job')).toBeInTheDocument();
        act(() => {
            document.dispatchEvent(new CustomEvent('crank:private-state-purged'));
        });
        expect(screen.queryByText('B job')).toBeNull();
        expect(screen.getByTestId('jobs-sign-in')).toBeInTheDocument();
    });

    test('an account change drops a late answer', async () => {
        store.setWorkspaceAccount({status: 'authenticated', key: 'ann'});
        let late: (value: unknown) => void = () => {};
        global.fetch = jest.fn().mockImplementation(() => new Promise(resolve => {
            late = resolve;
        }));
        renderJobs();
        act(() => store.setWorkspaceAccount({status: 'authenticated', key: 'bob'}));
        await act(async () => {
            late(answer(jobs()));
        });
        expect(screen.queryByTestId('job-row-1')).toBeNull();
        expect(screen.getByTestId('jobs-sign-in')).toBeInTheDocument();
    });
});

describe('postingUrl and payText', () => {
    test('only http(s) addresses become links', () => {
        expect(postingUrl('https://a.example.test/x')).toBe('https://a.example.test/x');
        expect(postingUrl('http://a.example.test/')).toBe('http://a.example.test/');
        expect(postingUrl('javascript:alert(1)')).toBeNull();
        expect(postingUrl('ftp://a.example.test')).toBeNull();
        expect(postingUrl('not a url')).toBeNull();
        expect(postingUrl(undefined)).toBeNull();
    });

    test('pay is shown as published and nothing is inferred', () => {
        expect(payText(job())).toBe('USD 150,000–190,000 per year');
        expect(payText(job({compensation_max: null}))).toBe('USD 150,000 per year');
        expect(payText(job({compensation_min: null}))).toBe('USD 190,000 per year');
        expect(payText(job({compensation_min: '100', compensation_max: '100', compensation_currency: '', compensation_interval: ''}))).toBe('100');
        expect(payText(job({compensation_min: null, compensation_max: null}))).toBe('Pay not published');
        expect(payText(job({compensation_min: 'abc', compensation_max: ''}))).toBe('Pay not published');
    });
});

describe('a job row says each thing once (issue #486, round 2)', () => {
    afterEach(cleanup);

    test('the arrangement is not printed twice', () => {
        expect(jobMeta(job())).toBe('Remote (US) · USD 150,000–190,000 per year');
        expect(jobMeta(job({location_text: 'Remote'}))).toBe('Remote · USD 150,000–190,000 per year');
        expect(jobMeta(job({location_text: 'Austin, TX'}))).toBe('Austin, TX · Remote · USD 150,000–190,000 per year');
        expect(jobMeta(job({location_text: '', is_remote: true}))).toBe('Remote · USD 150,000–190,000 per year');
        expect(jobMeta(job({location_text: 'Remote', is_remote: false, compensation_min: null, compensation_max: null})))
            .toBe('Remote · Not listed as remote · Pay not published');
    });

    test('a listing without an http(s) address says there is no link; one with an address names the new tab', async () => {
        global.fetch = jest.fn().mockResolvedValue(answer(jobs({count: 2, results: [
            job({id: 1}), job({id: 2, title: 'Designer', canonical_url: 'javascript:alert(1)'}),
        ]})));
        renderJobs();
        await screen.findByTestId('job-row-2');
        expect(screen.getByTestId('job-no-link-2')).toHaveTextContent('No link to the posting');
        expect(screen.queryByTestId('job-link-2')).toBeNull();
        expect(screen.queryByTestId('job-no-link-1')).toBeNull();
        const link = screen.getByTestId('job-link-1');
        expect(link).toHaveTextContent('View posting for Staff Engineer (opens in a new tab)');
        expect(link.querySelector('i.fa-arrow-up-right-from-square')).toHaveAttribute('aria-hidden', 'true');
        expect(link.textContent).not.toContain('↗');
    });

    test('the failure alert names what Retry retries, and the heading is a jump target', async () => {
        global.fetch = jest.fn().mockRejectedValue(new Error('boom'));
        renderJobs();
        const alert = await screen.findByTestId('jobs-error');
        expect(alert).toHaveTextContent('Retry loading open jobs');
        expect(document.getElementById('company-jobs-title')).toHaveClass('company-jump-heading');
    });
});
