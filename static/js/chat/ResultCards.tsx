// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import EvidenceSummary from '../evidence/EvidenceSummary';
import {EvidenceSummaryData, fundingRoundLabel, rtoPolicyLabel} from '../labels';
import type {JobResult, OrganizationResult, StructuredResults} from './types';

export function formatCompensation(comp: JobResult['compensation']): string {
    if (!comp) return '';
    const parts: string[] = [];
    const fmt = (v: number | null) => v !== null ? v.toLocaleString() : '';
    if (comp.min !== null && comp.max !== null) {
        parts.push(`${fmt(comp.min)}-${fmt(comp.max)}`);
    } else if (comp.min !== null) {
        parts.push(`${fmt(comp.min)}+`);
    } else if (comp.max !== null) {
        parts.push(`up to ${fmt(comp.max)}`);
    }
    if (comp.currency) parts.push(comp.currency);
    if (comp.interval) parts.push(comp.interval);
    return parts.join(' ');
}

export function JobCard({job}: {job: JobResult}) {
    const comp = formatCompensation(job.compensation);
    const freshness = job.observed_at
        ? new Date(job.observed_at).toLocaleDateString(undefined, {month: 'short', day: 'numeric'})
        : '';
    return (
        <article
            className="job-card border rounded p-2 mb-2"
            tabIndex={0}
            role="article"
            aria-label={`Job: ${job.title} at ${job.organization_name}`}
            style={{maxWidth: '100%', overflow: 'hidden'}}
        >
            <div className="d-flex justify-content-between align-items-start flex-wrap">
                <strong className="text-break" style={{maxWidth: '100%'}}>{job.title}</strong>
                {freshness && <small className="text-muted text-nowrap ms-2">{freshness}</small>}
            </div>
            <div className="text-muted small">
                {job.organization_name}{job.location ? ` · ${job.location}` : ''}
                {job.remote ? ' · Remote' : ''}
            </div>
            {comp && <div className="small">{comp}</div>}
            {job.canonical_url && (
                <a
                    href={job.canonical_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="small d-inline-block mt-1"
                    aria-label={`Open listing for ${job.title} (opens in a new tab)`}
                >
                    View listing ↗
                </a>
            )}
        </article>
    );
}

// Stored replies are served as persisted, so the summary is checked before
// it is rendered as counts.
const isEvidenceSummary = (value: unknown): value is EvidenceSummaryData => {
    if (!value || typeof value !== 'object') return false;
    const summary = value as Record<string, unknown>;
    return ['verified', 'stale', 'unknown', 'total', 'pending_review']
        .every((key) => Number.isInteger(summary[key]));
};

export function OrgCard({org}: {org: OrganizationResult}) {
    const funding = fundingRoundLabel(org.funding_round);
    const rto = rtoPolicyLabel(org.rto_policy);
    const evidence = isEvidenceSummary(org.evidence) ? org.evidence : null;
    return (
        <article
            className="org-card border rounded p-2 mb-2"
            tabIndex={0}
            role="article"
            aria-label={`Organization: ${org.name}`}
            style={{maxWidth: '100%', overflow: 'hidden'}}
        >
            <strong className="text-break" style={{maxWidth: '100%'}}>{org.name}</strong>
            <div className="text-muted small">
                {funding && <span>{funding}</span>}
                {funding && rto && ' · '}
                {rto && <span>{rto}</span>}
            </div>
            {evidence ? (
                <div className="org-card-evidence mt-2" data-testid={`org-evidence-${org.id}`}>
                    {/* The summary is the reply-time snapshot stored with the
                        message, so the label says so for every reader. */}
                    <div className="org-card-evidence-label" data-testid="evidence-recorded-label">Facts as of this reply</div>
                    <EvidenceSummary evidence={evidence} variant="card"/>
                </div>
            ) : (
                // A reply stored before fact status existed (or with an
                // unreadable one): say so rather than guess a status for it.
                <div className="org-card-evidence text-muted small mt-1" data-testid={`org-evidence-${org.id}`}>
                    Fact status was not recorded for this reply.
                </div>
            )}
            <div className="org-card-actions d-flex flex-wrap align-items-center column-gap-3 mt-1">
            {org.url && (
                <a
                    href={org.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={`View details for ${org.name} (opens in a new tab)`}
                >
                    Details ↗
                </a>
            )}
            <button
                type="button"
                className="btn btn-link p-0 correction-action"
                data-testid={`suggest-correction-org-${org.id}`}
                onClick={() => window.dispatchEvent(new CustomEvent('crank:suggest-company', {
                    detail: {kind: 'correction', source: 'assistant', organizationId: org.id, companyName: org.name},
                }))}
            >
                <i className="fa-solid fa-pen-to-square" aria-hidden="true"></i>
                Suggest a correction<span className="visually-hidden"> for {org.name}</span>
            </button>
            </div>
        </article>
    );
}

export function ResultCards({results}: {results: StructuredResults}) {
    const hasJobs = results.jobs && results.jobs.length > 0;
    const hasOrgs = results.organizations && results.organizations.length > 0;
    if (!hasJobs && !hasOrgs) return null;
    return (
        <div className="mt-2" data-testid="result-cards">
            {hasJobs && (
                <div>
                    <h3 className="h6 small text-muted mb-1">Job Listings</h3>
                    {results.jobs.map((job) => (
                        <JobCard key={`job-${job.id}`} job={job} />
                    ))}
                </div>
            )}
            {hasOrgs && (
                <div>
                    <h3 className="h6 small text-muted mb-1">Organizations</h3>
                    {results.organizations.map((org) => (
                        <OrgCard key={`org-${org.id}`} org={org} />
                    ))}
                </div>
            )}
        </div>
    );
}

export function hasResults(results: StructuredResults | null): boolean {
    if (!results) return false;
    return (results.jobs?.length || 0) + (results.organizations?.length || 0) > 0;
}
