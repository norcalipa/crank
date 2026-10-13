// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {retryFrom, useHoldReaderBelow, useOwnCompanyRead} from './useOwnCompanyRead';

export interface CompanyJob {
    id: number;
    title: string;
    location_text?: string;
    is_remote?: boolean | null;
    compensation_min?: string | number | null;
    compensation_max?: string | number | null;
    compensation_currency?: string;
    compensation_interval?: string;
    canonical_url?: string;
}

export interface CompanyJobs {
    organization_id: number;
    inventory: 'available' | 'unavailable';
    count: number;
    results: CompanyJob[];
}

const isCompanyJobs = (data: unknown): data is CompanyJobs => {
    const jobs = data as CompanyJobs | null;
    return !!jobs && typeof jobs === 'object' && Array.isArray(jobs.results)
        && (jobs.inventory === 'available' || jobs.inventory === 'unavailable');
};

/** The stored address as an `href`, only when it is an http(s) URL. */
export function postingUrl(raw: string | undefined): string | null {
    if (!raw) return null;
    try {
        const url = new URL(raw);
        return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
    } catch {
        return null;
    }
}

const amount = (value: string | number | null | undefined): number | null => {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
};

/**
 * Pay as published: the stored currency, range and period, nothing inferred.
 * "Pay not published" when the listing has no amount.
 */
export function payText(job: CompanyJob): string {
    const low = amount(job.compensation_min);
    const high = amount(job.compensation_max);
    if (low === null && high === null) return 'Pay not published';
    const format = (value: number) => value.toLocaleString('en-US', {maximumFractionDigits: 0});
    const range = low !== null && high !== null && low !== high
        ? `${format(low)}–${format(high)}`
        : format((low ?? high) as number);
    const currency = job.compensation_currency ? `${job.compensation_currency} ` : '';
    const period = job.compensation_interval ? ` per ${job.compensation_interval}` : '';
    return `${currency}${range}${period}`;
}

/** Where it is, whether it is remote (once), and the pay. */
export function jobMeta(job: CompanyJob): string {
    const place = job.location_text || '';
    // A location that already says "Remote" is neither followed by the same word
    // nor by "Not listed as remote", which the same line would contradict.
    const arrangement = /\bremote\b/i.test(place) ? '' : job.is_remote ? 'Remote' : 'Not listed as remote';
    return [place, arrangement, payText(job)].filter(part => part !== '').join(' · ');
}

// The most listings the endpoint returns (crank.views.company_view.MAX_COMPANY_JOBS).
export const MAX_COMPANY_JOBS = 25;

export interface JobsSectionProps {
    organizationId: number;
    name: string;
    isAuthenticated: boolean;
    signInUrl: string | null;
}

/**
 * The open jobs at this company for a signed-in reader (issue #486): up to 25
 * active listings. "Unavailable" (no job source is enabled) is a different
 * answer from "No open roles listed". The heading takes focus from "View jobs".
 */
const JobsSection = React.forwardRef<HTMLHeadingElement, JobsSectionProps>(({
    organizationId, name, isAuthenticated, signInUrl,
}, headingRef) => {
    const {state, retry} = useOwnCompanyRead<CompanyJobs>('jobs', organizationId, isAuthenticated, isCompanyJobs);
    const sectionRef = React.useRef<HTMLElement>(null);
    useHoldReaderBelow(sectionRef, state);
    const target = name.length > 40 ? 'this company' : name;
    const titleId = 'company-jobs-title';

    let body: React.ReactNode;
    if (!isAuthenticated || state.kind === 'signed-out') {
        body = (
            <p className="mb-0" data-testid="jobs-sign-in">
                {signInUrl
                    ? <a href={signInUrl}>Sign in to see open jobs at {target}.</a>
                    : `Sign in to see open jobs at ${target}.`}
            </p>
        );
    } else if (state.kind === 'loading') {
        body = (
            <div aria-busy="true" role="status" data-testid="jobs-loading">
                <p className="text-body-secondary mb-2">Loading open jobs…</p>
                <div className="company-jobs-skeleton" aria-hidden="true"></div>
            </div>
        );
    } else if (state.kind === 'error') {
        body = (
            <div className="alert alert-warning mb-0" role="alert" data-testid="jobs-error">
                <p className="mb-2">Open jobs could not be loaded.</p>
                <button type="button" className="btn btn-outline-light" data-testid="jobs-retry" onClick={() => retryFrom(titleId, retry)}>
                    Retry<span className="visually-hidden"> loading open jobs</span>
                </button>
            </div>
        );
    } else if (state.data.inventory === 'unavailable') {
        body = (
            <p className="mb-0" data-testid="jobs-unavailable">
                Job listings are not available yet, so open roles at {target} cannot be checked. This does not mean
                {' '}{target} is not hiring.
            </p>
        );
    } else if (state.data.results.length === 0) {
        body = <p className="mb-0" data-testid="jobs-empty">No open roles listed.</p>;
    } else {
        body = (
            <>
                <ul className="list-unstyled company-jobs-list mb-0" data-testid="jobs-list">
                    {state.data.results.map(job => {
                        const href = postingUrl(job.canonical_url);
                        return (
                            <li key={job.id} className="company-job-row" data-testid={`job-row-${job.id}`}>
                                <strong className="company-job-title text-break">{job.title}</strong>
                                <span className="company-job-meta small text-body-secondary">{jobMeta(job)}</span>
                                {href ? (
                                    <a href={href} target="_blank" rel="noopener noreferrer" className="company-job-link"
                                       data-testid={`job-link-${job.id}`}>
                                        <span className="text-nowrap">
                                            View posting{' '}
                                            <i className="fa-solid fa-arrow-up-right-from-square" aria-hidden="true"></i>
                                        </span>
                                        <span className="visually-hidden"> for {job.title}</span>
                                        {' '}<span className="evidence-external-note">(opens in a new tab)</span>
                                    </a>
                                ) : (
                                    <span className="company-job-no-link small text-body-secondary"
                                          data-testid={`job-no-link-${job.id}`}>
                                        No link to the posting
                                    </span>
                                )}
                            </li>
                        );
                    })}
                </ul>
                {state.data.results.length >= MAX_COMPANY_JOBS && (
                    <p className="small text-body-secondary mt-2 mb-0" data-testid="jobs-capped">
                        Showing the first {MAX_COMPANY_JOBS} listings. {target} may have more.
                    </p>
                )}
            </>
        );
    }

    return (
        <section ref={sectionRef} className="company-jobs mt-3" data-testid="company-jobs" aria-labelledby={titleId}>
            <h3 id={titleId} ref={headingRef} tabIndex={-1} className="h5 mb-2 company-jump-heading">Open jobs</h3>
            {body}
        </section>
    );
});
JobsSection.displayName = 'JobsSection';

export default JobsSection;
