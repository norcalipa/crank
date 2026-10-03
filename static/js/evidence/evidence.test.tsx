// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import {render, screen, within} from '@testing-library/react';
import * as fs from 'fs';
import * as path from 'path';
import * as React from 'react';

import EvidenceBadge from './EvidenceBadge';
import EvidenceDetails, {EvidenceData, EvidenceSkeleton} from './EvidenceDetails';
import EvidenceSummary from './EvidenceSummary';
import {EVIDENCE_STATUS_META, EvidenceStatusKey, formatEvidenceDate} from '../labels';

const field = (overrides: Record<string, unknown> = {}) => ({
    field_key: 'rto_policy', value: 'Remote first', source_domain: 'example.com',
    source_url: 'https://example.com/about', observed_at: '2025-01-10T12:00:00Z', scope: {countries: ['US']},
    last_checked_at: '2025-02-01T00:00:00Z', last_successful_fetch_at: null, last_changed_at: null,
    last_verified_at: '2025-02-01T00:00:00Z', stale: false, status: 'verified', review: 'none', policy_days: 90,
    ...overrides,
});

describe('EvidenceBadge', () => {
    test.each(Object.keys(EVIDENCE_STATUS_META) as EvidenceStatusKey[])('%s renders word, marker and name', status => {
        render(<EvidenceBadge status={status} fieldLabel="RTO Policy" lastVerifiedAt="2024-08-08T00:00:00Z"/>);
        const meta = EVIDENCE_STATUS_META[status];
        const badge = document.querySelector('.evidence-badge') as HTMLElement;
        expect(badge).toHaveTextContent(meta.label);
        expect(badge).toHaveTextContent(meta.marker);
        expect(badge.querySelector('[aria-hidden="true"]')).toHaveTextContent(meta.marker);
        expect(badge).toHaveTextContent('RTO Policy: ');
        expect(badge).toHaveTextContent('last verified August 8, 2024');
    });

    test('omits optional context', () => {
        render(<EvidenceBadge status="stale"/>);
        expect(document.querySelector('.visually-hidden')).toBeNull();
    });

    test('formatEvidenceDate handles missing and invalid dates', () => {
        expect(formatEvidenceDate(null)).toBe('Unknown');
        expect(formatEvidenceDate('nope')).toBe('Unknown');
        expect(formatEvidenceDate('2024-08-08T00:00:00Z')).toBe('Aug 8, 2024');
    });

    test('formatEvidenceDate formats in the reader\'s time zone, not UTC', () => {
        // Simulate a reader in Los Angeles whatever TZ the runner uses: 03:00Z
        // is still Aug 7 there. A formatter that pinned timeZone to UTC would
        // read Aug 8.
        const original = Date.prototype.toLocaleDateString;
        const spy = jest.spyOn(Date.prototype, 'toLocaleDateString').mockImplementation(
            function (this: Date, locale?: Intl.LocalesArgument, options?: Intl.DateTimeFormatOptions) {
                return original.call(this, locale, {timeZone: 'America/Los_Angeles', ...options});
            });
        try {
            expect(formatEvidenceDate('2024-08-08T03:00:00Z')).toBe('Aug 7, 2024');
        } finally {
            spy.mockRestore();
        }
    });

    test('no other component renders the status vocabulary', () => {
        const root = path.resolve(__dirname, '..');
        const files: string[] = [];
        const walk = (dir: string) => {
            for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
                const full = path.join(dir, entry.name);
                if (entry.isDirectory()) {
                    if (entry.name !== 'evidence' && entry.name !== 'e2e') walk(full);
                } else if (/\.tsx?$/.test(entry.name) && !/\.test\./.test(entry.name) && entry.name !== 'labels.ts') {
                    files.push(full);
                }
            }
        };
        walk(root);
        const labels = Object.values(EVIDENCE_STATUS_META).map(meta => meta.label);
        const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        // JSX text containing the word (also with extra text around it), and a
        // string or template literal that is exactly the word. "Unknown" as a
        // bare literal is also generic English (date fallbacks), so only the
        // JSX-text form is checked for it.
        // Plain-text uses that cannot render a badge (an <option>, a live-region
        // announcement, a section heading naming a different noun). Anything
        // new must be added here deliberately.
        const allowed: Record<string, string[]> = {
            'CompanyCorrectionForm.tsx': ['Verified', 'Stale', 'Pending review'],
        };
        const patterns = labels.map(label => ({
            label,
            jsx: new RegExp(`>[^<>{}]*\\b${escape(label)}\\b[^<>{}]*<`),
            literal: label === 'Unknown' ? null : new RegExp(`(['"\`])${escape(label)}\\1`),
        }));
        for (const file of files) {
            const source = fs.readFileSync(file, 'utf8').replace(/>\s+/g, '>').replace(/\s+</g, '<');
            for (const {label, jsx, literal} of patterns) {
                if (allowed[path.basename(file)]?.includes(label)) continue;
                expect({file: path.basename(file), label, jsx: jsx.test(source)})
                    .toEqual({file: path.basename(file), label, jsx: false});
                if (literal) {
                    expect({file: path.basename(file), label, literal: literal.test(source)})
                        .toEqual({file: path.basename(file), label, literal: false});
                }
            }
        }
    });

    test('the vocabulary scan catches braces, template literals and surrounding text', () => {
        const jsx = new RegExp('>[^<>{}]*\\bStale\\b[^<>{}]*<');
        expect(jsx.test('<b>Stale facts</b>')).toBe(true);
        expect(/(['"`])Stale\1/.test("const x = {'Stale'}")).toBe(true);
        expect(/(['"`])Stale\1/.test('const x = `Stale`')).toBe(true);
    });
});

describe('EvidenceSummary', () => {
    const base = {verified: 0, stale: 0, unknown: 7, total: 7, fact_coverage: 0, last_verified_at: null, pending_review: 0};

    test('missing summary reads Unknown', () => {
        render(<EvidenceSummary evidence={undefined}/>);
        expect(screen.getByTestId('evidence-summary')).toHaveTextContent('Unknown');
        expect(screen.queryByText(/Verified/)).toBeNull();
    });

    test('complete-but-stale reads stale in text', () => {
        render(<EvidenceSummary variant="card" evidence={{...base, stale: 2, unknown: 5, fact_coverage: 2,
            last_verified_at: '2024-08-08T00:00:00Z'}}/>);
        const root = screen.getByTestId('evidence-summary');
        expect(root).toHaveClass('evidence-summary-card');
        expect(root.querySelector('.evidence-badge-stale')).toHaveTextContent('Stale');
        expect(screen.getByTestId('evidence-facts')).toHaveTextContent('0 verified · 2 stale · 5 unknown');
        expect(screen.getByTestId('evidence-last-verified')).toHaveTextContent('Last verified Aug 8, 2024');
    });

    test('a partly verified profile leads with counts, never a Verified verdict', () => {
        render(<EvidenceSummary evidence={{...base, verified: 3, unknown: 4, fact_coverage: 3,
            last_verified_at: '2025-01-01T00:00:00Z', pending_review: 1}}/>);
        const root = screen.getByTestId('evidence-summary');
        expect(root.querySelector('.evidence-badge-verified')).toBeNull();
        expect(root.querySelector('.evidence-badge-unknown')).toBeNull();
        expect(screen.getByTestId('evidence-facts')).toHaveTextContent('3 verified · 0 stale · 4 unknown');
        expect(screen.getByTestId('evidence-pending')).toHaveTextContent('Pending review');
    });

    test('every fact verified reads Verified', () => {
        render(<EvidenceSummary evidence={{...base, verified: 7, unknown: 0, fact_coverage: 7,
            last_verified_at: '2025-01-01T00:00:00Z'}}/>);
        expect(screen.getByTestId('evidence-summary').querySelector('.evidence-badge-verified')).toHaveTextContent('Verified');
    });

    test('never verified', () => {
        render(<EvidenceSummary evidence={base}/>);
        expect(screen.getByTestId('evidence-last-verified')).toHaveTextContent('Never verified');
        expect(screen.queryByTestId('evidence-pending')).toBeNull();
    });
});

describe('EvidenceDetails', () => {
    const evidence = (extra: Record<string, unknown> = {}): EvidenceData => ({
        fields: [field() as never],
        unverified_fields: ['funding_round'],
        pending_review: [],
        summary: {verified: 1, stale: 0, unknown: 6, total: 7, fact_coverage: 1, last_verified_at: null, pending_review: 0},
        ...extra,
    });

    test('verified row has a validated link with safe attributes', () => {
        render(<EvidenceDetails evidence={evidence()}
                                renderFieldAction={key => <button>fix {key}</button>}/>);
        const link = screen.getByTestId('field-source-link-rto_policy');
        expect(link).toHaveAttribute('href', 'https://example.com/about');
        expect(link).toHaveAttribute('target', '_blank');
        expect(link).toHaveAttribute('rel', 'noopener noreferrer nofollow');
        expect(link).toHaveTextContent('example.com (opens in a new tab)');
        expect(screen.queryByTestId('coverage-rating')).toBeNull();
        expect(screen.getByTestId('coverage-facts')).toHaveTextContent('1 of 7 tracked facts');
        expect(screen.getByTestId('field-status-rto_policy')).toHaveTextContent('Verified');
        expect(screen.queryByTestId('field-stale-rto_policy')).toBeNull();
        expect(screen.getByTestId('field-evidence-rto_policy')).toHaveTextContent('Re-check every 90 days');
        expect(screen.getByTestId('field-evidence-rto_policy')).toHaveTextContent('Scope — countries: US');
        expect(screen.getByText('fix rto_policy')).toBeInTheDocument();
        expect(screen.getByTestId('field-unverified-funding_round')).toHaveTextContent('No accepted evidence');
    });

    test('no link without a validated source_url, long text wraps via class', () => {
        const long = 'x'.repeat(400);
        render(<EvidenceDetails evidence={evidence({fields: [field({source_url: null, value: long,
            source_domain: ''}) as never]})}/>);
        expect(screen.queryByTestId('field-source-link-rto_policy')).toBeNull();
        expect(screen.getByTestId('field-value-rto_policy')).toHaveClass('evidence-value');
        expect(screen.getByTestId('field-evidence-rto_policy')).toHaveTextContent('Source: unknown source');
    });

    test('stale row and review marker, legacy row without status derives from stale flag', () => {
        render(<EvidenceDetails evidence={evidence({fields: [
            field({status: 'stale', stale: true, review: 'conflicted', last_verified_at: null, scope: {}}) as never,
            field({field_key: 'locations', status: undefined, stale: true, review: undefined,
                policy_days: undefined, last_verified_at: '2024-01-01T00:00:00Z'}) as never,
        ]})}/>);
        expect(screen.getByTestId('field-stale-rto_policy')).toHaveTextContent('Stale');
        expect(screen.getByTestId('field-review-rto_policy')).toHaveTextContent('Conflicting observation');
        expect(screen.getByTestId('field-evidence-rto_policy')).toHaveTextContent('Last verified never');
        expect(screen.getByTestId('field-status-locations')).toHaveTextContent('Stale');
        expect(screen.queryByTestId('field-review-locations')).toBeNull();
    });

    test('check history details lists the four timestamps', () => {
        render(<EvidenceDetails evidence={evidence()}/>);
        const details = screen.getByTestId('field-evidence-rto_policy').querySelector('details') as HTMLElement;
        expect(details).toBeInTheDocument();
        for (const label of ['Last checked', 'Last successful fetch', 'Last changed', 'Last verified']) {
            expect(within(details).getByText(label)).toBeInTheDocument();
        }
    });

    test('pending review lists observed values as unverified text', () => {
        render(<EvidenceDetails evidence={evidence({pending_review: [
            {field_key: 'rto_policy', review: 'conflicted', observed_at: '2025-03-01T00:00:00Z',
                source_domain: 'other.com', observed_value: 'In office'},
            {field_key: 'locations', review: 'pending', observed_at: '2025-03-02T00:00:00Z',
                source_domain: '', observed_value: ''},
        ]})}/>);
        const section = screen.getByTestId('pending-review');
        const item = within(section).getByTestId('pending-review-rto_policy');
        expect(item).toHaveTextContent('Conflicting observation');
        expect(item).toHaveTextContent('Not yet reviewed');
        expect(item).toHaveTextContent('Observed, not verified: “In office”');
        expect(item).not.toHaveTextContent('✓');
        expect(within(section).getByTestId('pending-review-locations')).toHaveTextContent('unknown source');
    });

    test('empty state explains and offers the primary action', () => {
        render(<EvidenceDetails evidence={{fields: [], unverified_fields: [], pending_review: []}}
                                emptyAction={<button>Suggest a correction</button>}/>);
        expect(screen.getByTestId('evidence-empty')).toHaveTextContent('No accepted evidence for any tracked fact');
        expect(screen.getByRole('button', {name: 'Suggest a correction'})).toBeInTheDocument();
        expect(screen.queryByTestId('coverage-rating')).toBeNull();
    });

    test('skeleton is a polite busy status', () => {
        render(<EvidenceSkeleton/>);
        expect(screen.getByTestId('provenance-loading')).toHaveAttribute('aria-busy', 'true');
        expect(screen.getByRole('status')).toHaveTextContent('Loading evidence…');
    });
});

describe('details dialog overlay CSS (#473a)', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../../css/popup.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
    const rules = (selector: string) => css.split('}').filter(block => block.trim().startsWith(selector + ' {'));

    test('the overlay never scrolls; only the card body does', () => {
        const overlay = rules('.popup-overlay').join('\n');
        expect(overlay).not.toMatch(/overflow-y:\s*auto/);
        expect(rules('.popup-details .card-body').join('\n')).toMatch(/position:\s*relative/);
    });

    test('the dialog height accounts for the overlay top gap', () => {
        const dialog = rules('.popup-details').join('\n');
        expect(dialog).toContain('var(--popup-gap-top');
        expect(dialog).not.toContain('100dvh - 1.5rem');
    });
});
