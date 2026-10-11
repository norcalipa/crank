// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import * as React from 'react';
import {act, fireEvent, render, screen} from '@testing-library/react';
import PriorityChips from './PriorityChips';
import ReviewChanges, {MORE_BELOW_PX} from './ReviewChanges';
import AppliedChanges from './AppliedChanges';
import {chipValueLabel, preferencePathLabel, preferenceValueLabel, prioritiesSummary, prioritiesSummaryParts} from './format';

const changes = [{path: 'compensation.minimum_salary', old: 0, new: 150000}];

describe('ReviewChanges', () => {
    test('account scope: Apply / Edit / This search only / Cancel, heading focused', () => {
        const h = {onApply: jest.fn(), onCancel: jest.fn(), onEdit: jest.fn(), onApplySearchOnly: jest.fn()};
        render(<ReviewChanges changes={changes} {...h}/>);
        expect(screen.getByRole('heading', {name: 'Review your changes'})).toHaveFocus();
        expect(screen.getByText('Compensation › minimum salary')).toBeInTheDocument();
        for (const [name, fn] of [['Apply to account', h.onApply], ['Edit', h.onEdit],
            ['This search only', h.onApplySearchOnly], ['Cancel', h.onCancel]] as const) {
            fireEvent.click(screen.getByRole('button', {name}));
            expect(fn).toHaveBeenCalledTimes(1);
        }
    });

    test('search scope, empty diff, pending and stale variants', () => {
        const base = {onApply: jest.fn(), onCancel: jest.fn(), onApplySearchOnly: jest.fn(), onReviewLatest: jest.fn()};
        const {rerender} = render(<ReviewChanges changes={[]} scope="search" {...base}/>);
        expect(screen.getByTestId('priorities-review-empty')).toBeInTheDocument();
        expect(screen.getByRole('button', {name: 'Apply to this search'})).toBeDisabled();
        expect(screen.queryByRole('button', {name: 'This search only'})).not.toBeInTheDocument();
        rerender(<ReviewChanges changes={changes} pending {...base}/>);
        expect(screen.getByRole('button', {name: /Applying/})).toHaveAttribute('aria-busy', 'true');
        rerender(<ReviewChanges changes={changes} stale error="old" {...base}/>);
        expect(screen.getByRole('alert')).toHaveTextContent('old');
        fireEvent.click(screen.getByRole('button', {name: 'Review latest'}));
        expect(base.onReviewLatest).toHaveBeenCalled();
        expect(screen.queryByRole('button', {name: 'Apply to account'})).not.toBeInTheDocument();
    });
});

describe('AppliedChanges', () => {
    test('shows summary, undo and done; undone and no-undo variants', () => {
        const h = {onUndo: jest.fn(), onDismiss: jest.fn()};
        const {rerender} = render(<AppliedChanges changes={changes} summary="Saved." canUndo {...h}/>);
        expect(screen.getByRole('heading', {name: 'Saved.'})).toHaveFocus();
        fireEvent.click(screen.getByRole('button', {name: 'Undo'}));
        fireEvent.click(screen.getByRole('button', {name: 'Done'}));
        expect(h.onUndo).toHaveBeenCalled();
        expect(h.onDismiss).toHaveBeenCalled();
        rerender(<AppliedChanges changes={changes} summary="Saved." canUndo undoPending undoError="bad" {...h}/>);
        expect(screen.getByRole('button', {name: 'Undoing…'})).toBeDisabled();
        expect(screen.getByRole('alert')).toHaveTextContent('bad');
        rerender(<AppliedChanges changes={[]} summary="Saved." canUndo={false} {...h}/>);
        expect(screen.queryByRole('button', {name: 'Undo'})).not.toBeInTheDocument();
        rerender(<AppliedChanges changes={changes} summary="Saved." canUndo undone {...h}/>);
        expect(screen.getByRole('heading', {name: 'Change undone.'})).toBeInTheDocument();
        expect(screen.queryByRole('button', {name: 'Undo'})).not.toBeInTheDocument();
    });
});

describe('format', () => {
    test('labels and values', () => {
        expect(preferencePathLabel('work_location.modes')).toBe('Work location › modes');
        expect(preferenceValueLabel(null)).toBe('Not set');
        expect(preferenceValueLabel(true)).toBe('Yes');
        expect(preferenceValueLabel('')).toBe('Not set');
        expect(preferenceValueLabel('x')).toBe('x');
        expect(preferenceValueLabel(1500)).toBe('1,500');
        expect(preferenceValueLabel(Infinity)).toBe('Infinity');
        expect(preferenceValueLabel([])).toBe('None');
        expect(preferenceValueLabel(['a', 1])).toBe('a, 1');
        expect(preferenceValueLabel({a: 1})).toBe('A: 1');
        expect(preferenceValueLabel({})).toBe('None');
        expect(preferenceValueLabel({'work_location.modes': 1, industry: 0}, 'importance'))
            .toBe('Work location › modes: Requirement, Industry: Preference');
        const cyclic: any = {};
        cyclic.self = cyclic;
        expect(preferenceValueLabel(cyclic)).toBe('Self: [object Object]');
    });
});

describe('preferenceValueLabel with a path', () => {
    test('uses the chip formatting for money, percent and enum words', () => {
        expect(preferenceValueLabel(150000, 'compensation.minimum_salary')).toBe('$150,000');
        expect(preferenceValueLabel(0.5, 'compensation.equity_minimum_percent')).toBe('0.5%');
        expect(preferenceValueLabel(Infinity, 'compensation.minimum_salary')).toBe('Infinity');
        expect(preferenceValueLabel('series_b', 'funding_stage')).toBe('Series B');
        expect(preferenceValueLabel(['remote', 'hybrid'], 'work_location.modes')).toBe('Remote, Hybrid');
        expect(preferenceValueLabel('', 'culture')).toBe('Not set');
    });
});

describe('PriorityChips', () => {
    const make = (n: number, over: Record<string, unknown> = {}) => Array.from({length: n}, (_, i) => ({
        path: `p${i}`, label: `Label ${i}`, display: `v${i}`, hard: false, supported: true, ...over,
    }));

    test('caps a long list at five with a +N more toggle', () => {
        render(<PriorityChips chips={make(8)} onEdit={jest.fn()}/>);
        expect(screen.getAllByTestId('priority-chip')).toHaveLength(5);
        const more = screen.getByRole('button', {name: '+3 more'});
        expect(more).toHaveAttribute('aria-expanded', 'false');
        fireEvent.click(more);
        expect(screen.getAllByTestId('priority-chip')).toHaveLength(8);
        fireEvent.click(screen.getByRole('button', {name: 'Show less'}));
        expect(screen.getAllByTestId('priority-chip')).toHaveLength(5);
    });

    test('a smaller collapsed count and requirement-first ordering; unsupported chips sort last', () => {
        const chips = [
            ...make(2, {supported: false}).map((c, i) => ({...c, path: `u${i}`, label: `Unused ${i}`})),
            ...make(4).map((c, i) => ({...c, path: `s${i}`, label: `Soft ${i}`})),
            {path: 'h', label: 'Hard one', display: 'v', hard: true, supported: true},
        ];
        render(<PriorityChips chips={chips} collapsedCount={3} onEdit={jest.fn()}/>);
        const shown = screen.getAllByTestId('priority-chip').map((li) => li.textContent);
        expect(shown).toHaveLength(3);
        expect(shown[0]).toContain('Hard one');
        expect(shown.join(' ')).not.toContain('Unused');
        expect(screen.getByRole('button', {name: '+4 more'})).toBeInTheDocument();
    });

    test('short list has no toggle; the legend shows only for requirement or unsupported chips', () => {
        const {rerender} = render(<PriorityChips chips={make(5)} onEdit={jest.fn()}/>);
        expect(screen.queryByRole('button', {name: /more|Show less/})).not.toBeInTheDocument();
        expect(screen.queryByTestId('priority-chip-legend')).not.toBeInTheDocument();
        rerender(<PriorityChips chips={make(2, {hard: true})} onEdit={jest.fn()}/>);
        expect(screen.getByTestId('priority-chip-legend')).toBeInTheDocument();
        rerender(<PriorityChips chips={make(2, {supported: false})} onEdit={jest.fn()}/>);
        // After the list, the key always names both marks.
        expect(screen.getByTestId('priority-chip-legend')).toHaveTextContent('RequirementNot used yet');
        expect(screen.getByTestId('priority-chip-legend').closest('.priority-chips-meta')).not.toBeNull();
    });

    test('legend={false} leaves the key to the caller', () => {
        render(<PriorityChips chips={make(2, {hard: true})} legend={false} onEdit={jest.fn()}/>);
        expect(screen.getAllByTestId('priority-chip')).toHaveLength(2);
        expect(screen.queryByTestId('priority-chip-legend')).not.toBeInTheDocument();
    });
});

describe('ChangeList values', () => {
    test('formats money like the chip and marks an empty old value', () => {
        render(<ReviewChanges changes={[
            {path: 'compensation.minimum_salary', old: null, new: 150000},
            {path: 'culture', old: [], new: ['kind']},
            {path: 'compensation.currency', old: 'EUR', new: 'USD'},
        ]} onApply={jest.fn()} onCancel={jest.fn()}/>);
        expect(screen.getByText('$150,000')).toBeInTheDocument();
        expect(screen.getAllByText('Not set')[0]).toHaveClass('is-empty');
        expect(screen.getByText('None')).toHaveClass('is-empty');
        expect(screen.getByText('EUR')).not.toHaveClass('is-empty');
    });
});

describe('chip list entries (issue #480 review)', () => {
    test('an entry containing a comma stays whole: entries are joined with a semicolon', () => {
        expect(chipValueLabel('exclusions.locations', 'San Francisco, CA, Austin', undefined, ['San Francisco, CA', 'Austin']))
            .toBe('San Francisco, CA; Austin');
        expect(chipValueLabel('culture', 'kind, open', undefined, ['kind', 'open'])).toBe('kind, open');
        expect(chipValueLabel('funding_stage', 'series_a, seed', undefined, ['series_a', 'seed'], true)).toBe('Series A, Seed');
    });

    test('a chip renders one whole entry per saved item', () => {
        render(<PriorityChips chips={[{path: 'exclusions.locations', label: 'Excluded locations', display: 'x',
            hard: false, supported: true, items: ['San Francisco, CA', 'Austin']}]}
                              collapsedCount={5} onEdit={jest.fn()}/>);
        expect(screen.getByTestId('priority-chip')).toHaveTextContent('San Francisco, CA; Austin');
    });
});

describe('money in one edit that changes the currency', () => {
    test('old values use the old currency and new values the proposed one', () => {
        render(<ReviewChanges changes={[
            {path: 'compensation.currency', old: 'USD', new: 'EUR'},
            {path: 'compensation.minimum_salary', old: 150000, new: 140000},
        ]} currency="USD" onApply={jest.fn()} onCancel={jest.fn()}/>);
        const row = screen.getByText('Compensation › minimum salary').closest('li') as HTMLElement;
        expect(row).toHaveTextContent('$150,000');
        expect(row).toHaveTextContent('EUR 140,000');
    });

    test('after Apply the saved currency is the new one, the change still carries the old', () => {
        render(<AppliedChanges changes={[
            {path: 'compensation.currency', old: 'USD', new: 'EUR'},
            {path: 'compensation.minimum_salary', old: 150000, new: 140000},
        ]} currency="EUR" summary="Saved" canUndo={false} onUndo={jest.fn()} onDismiss={jest.fn()}/>);
        const row = screen.getByText('Compensation › minimum salary').closest('li') as HTMLElement;
        expect(row).toHaveTextContent('$150,000');
        expect(row).toHaveTextContent('EUR 140,000');
    });
});

describe('Review latest while it runs', () => {
    test('is announced as busy, ignores a second activation and keeps focusability', () => {
        const onReviewLatest = jest.fn();
        render(<ReviewChanges changes={changes} stale pending onApply={jest.fn()}
                              onCancel={jest.fn()} onReviewLatest={onReviewLatest}/>);
        const button = screen.getByRole('button', {name: 'Checking…'});
        expect(button).toHaveAttribute('aria-disabled', 'true');
        expect(button).not.toBeDisabled();
        fireEvent.click(button);
        expect(onReviewLatest).not.toHaveBeenCalled();
        expect(screen.getByRole('status')).toHaveTextContent('1 change to review');
    });
});

describe('free text is shown as typed', () => {
    test('enum tokens read as labels but a sentence is left alone', () => {
        expect(preferenceValueLabel('series_b', 'funding_stage')).toBe('Series B');
        expect(preferenceValueLabel('prefers remote-first teams', 'notes')).toBe('prefers remote-first teams');
    });
});

describe('chipValueLabel', () => {
    test('groups numbers, adds a currency symbol for money, leaves text alone', () => {
        expect(chipValueLabel('compensation.minimum_salary', '150000')).toBe('$150,000');
        expect(chipValueLabel('compensation.minimum_total_compensation', '200000')).toBe('$200,000');
        expect(chipValueLabel('vesting.max_cliff_months', '12')).toBe('12');
        expect(chipValueLabel('culture', 'kind, curious')).toBe('Kind, Curious');
        expect(chipValueLabel('compensation.equity_minimum_percent', '0.5')).toBe('0.5%');
        expect(chipValueLabel('funding_stage', 'series_b')).toBe('Series B');
        expect(chipValueLabel('work_location.countries', 'US, CA')).toBe('US, CA');
        expect(chipValueLabel('roles.titles', 'Staff Engineer')).toBe('Staff Engineer');
    });
});

describe('ReviewChanges copy', () => {
    test('offers the save-or-search note and announces plural change counts', () => {
        const h = {onApply: jest.fn(), onCancel: jest.fn(), onApplySearchOnly: jest.fn()};
        const two = [...changes, {path: 'culture', old: [], new: ['kind']}];
        render(<ReviewChanges changes={two} {...h}/>);
        expect(screen.getByRole('status')).toHaveTextContent('2 changes to review');
        expect(screen.getByText('Save these to your account, or use them for this search only.')).toBeInTheDocument();
    });
});

describe('review wording (issue #480 round 1)', () => {
    const {ChangeList} = require('./ReviewChanges');
    const {preferenceValueLabel, chipValueLabel} = require('./format');

    test('importance toggles review as words, one row per changed key, never JSON', () => {
        render(<ChangeList label="Changes"
            changes={[{path: 'importance', old: {'compensation.minimum_salary': 0.5}, new: {'compensation.minimum_salary': 1}}]}
            labels={{'compensation.minimum_salary': 'Minimum base salary'}}/>);
        const text = document.body.textContent || '';
        expect(text).toContain('Minimum base salary');
        expect(text).toContain('Requirement');
        expect(text).not.toContain('{');
        expect(text).toContain('Minimum base salary importance');
    });

    test('a map that appears or disappears whole expands to one row per key, named without labels', () => {
        const {expandChanges} = require('./format');
        const rows = expandChanges([
            {path: 'priorities', old: null, new: {culture: 0.5}},
            {path: 'importance', old: {'work_location.modes': 1}, new: null},
        ]);
        expect(rows.map((r: {label: string}) => r.label)).toEqual([
            expect.stringContaining('culture'.charAt(0).toUpperCase() + 'ulture'),
            expect.stringContaining('importance'),
        ]);
    });

    test('a weight that is no longer saved reads as a preference, not as "Default"', () => {
        render(<ChangeList label="Changes"
            changes={[{path: 'importance', old: {'compensation.minimum_salary': 1}, new: {}}]}
            labels={{'compensation.minimum_salary': 'Minimum base salary'}}/>);
        const row = screen.getByText('Minimum base salary importance').closest('li') as HTMLElement;
        expect(row).toHaveTextContent('Requirement');
        expect(row).toHaveTextContent('Preference');
        expect(row).not.toHaveTextContent('Default');
    });

    test('currency prefixes follow the saved currency', () => {
        expect(chipValueLabel('compensation.minimum_salary', '150000', 'EUR')).toContain('EUR');
        expect(chipValueLabel('compensation.minimum_salary', '150000', 'USD')).toContain('$');
        expect(preferenceValueLabel(null)).toBeDefined();
    });

    test('values outside the known shapes fall back to String()', () => {
        expect(preferenceValueLabel(BigInt(7))).toBe('7');
    });
});

describe('prioritiesSummary (collapsed sidebar row)', () => {
    const chipOf = (over: Record<string, unknown>) => ({
        path: 'culture', label: 'Culture', display: 'kind', hard: false, supported: true, ...over,
    });
    const salary = chipOf({path: 'compensation.minimum_salary', label: 'Minimum base salary', display: '150000', hard: true});
    const modes = chipOf({path: 'work_location.modes', label: 'Work arrangement', display: 'remote', items: ['remote'], hard: true});

    test('nothing saved, one preference and several preferences', () => {
        expect(prioritiesSummary([])).toBe('');
        expect(prioritiesSummary([chipOf({})])).toBe('1 preference');
        expect(prioritiesSummary([chipOf({}), chipOf({path: 'industry'})])).toBe('2 preferences');
    });

    test('requirements come first with the chip formatting, then the preference count', () => {
        expect(prioritiesSummary([salary])).toBe('Requires: $150,000');
        expect(prioritiesSummary([chipOf({}), salary, modes], 'EUR', new Set(['work_location.modes'])))
            .toBe('Requires: EUR 150,000, +1 \u00b7 1 preference');
        expect(prioritiesSummary([chipOf({}), salary, modes], undefined, new Set(['work_location.modes'])))
            .toBe('Requires: $150,000, Remote \u00b7 1 preference');
        expect(prioritiesSummary([salary, modes])).toBe('Requires: $150,000, remote');
    });

    test('more than two requirements end with +k; one the matcher does not use still counts, named last', () => {
        const unused = chipOf({path: 'compensation.equity_liquidity_required', label: 'Equity liquidity', display: 'Yes', hard: true, supported: false});
        const chips = [unused, salary, modes, chipOf({})];
        expect(prioritiesSummary(chips)).toBe('Requires: $150,000, remote, +1 \u00b7 1 preference');
        expect(chips[0]).toBe(unused);
        // A second requirement that would make the lead long is counted instead of named.
        expect(prioritiesSummary([unused, salary])).toBe('Requires: $150,000, +1');
    });

    test('a value that says nothing alone is named by its field', () => {
        const days = chipOf({path: 'work_location.max_in_office_days', label: 'In-office days', display: '2', hard: true});
        const flag = chipOf({path: 'compensation.require_public_company', label: 'Public company', display: 'No', hard: true});
        expect(prioritiesSummary([days, flag])).toBe('Requires: In-office days: 2, +1');
        expect(prioritiesSummary([flag])).toBe('Requires: Public company: No');
        const short = chipOf({path: 'work_location.max_in_office_days', label: 'Days', display: '2', hard: true});
        expect(prioritiesSummary([short, chipOf({...flag, label: 'Pub'})])).toBe('Requires: Days: 2, Pub: No');
    });

    test('a number with its unit is named by its field, a list of values shows them', () => {
        const equity = chipOf({path: 'compensation.minimum_equity_percent', label: 'Minimum equity (%)', display: '0.5%', hard: true});
        const countries = chipOf({path: 'work_location.countries', label: 'Work countries', display: 'US, CA', items: ['US', 'CA'], hard: true});
        expect(prioritiesSummary([equity])).toBe('Requires: Minimum equity (%): 0.5%');
        // A list of required values shows its values; one entry stands for itself.
        expect(prioritiesSummary([countries])).toBe('Requires: US, CA');
        expect(prioritiesSummary([modes])).toBe('Requires: remote');
    });
});

describe('prioritiesSummaryParts (lead, separator, counts)', () => {
    const chipOf = (over: Record<string, unknown>) => ({
        path: 'culture', label: 'Culture', display: 'kind', hard: false, supported: true, ...over,
    });
    const req = (n: number) => chipOf({path: `r${n}`, display: `Need${n}`, hard: true});

    const days = (label: string) => chipOf({path: 'work_location.max_in_office_days', label, display: '2', hard: true});

    test.each([
        ['nothing saved', [], {lead: '', keep: '', sep: '', tail: ''}],
        ['preferences only', [chipOf({}), chipOf({path: 'industry'})], {lead: '2 preferences', keep: '', sep: '', tail: ''}],
        ['requirements only', [req(1), req(2)], {lead: 'Requires: Need1, Need2', keep: '', sep: '', tail: ''}],
        ['requirements and preferences', [req(1), chipOf({})], {lead: 'Requires: Need1', keep: '', sep: ' \u00b7 ', tail: '1 preference'}],
        // The comma rides on the lead: when the lead is cut, no stray ", " is left before the counts.
        ['more requirements than named', [req(1), req(2), req(3), req(4)], {lead: 'Requires: Need1, Need2,', keep: '', sep: ' ', tail: '+2'}],
        ['more requirements and preferences', [req(1), req(2), req(3), chipOf({})],
            {lead: 'Requires: Need1, Need2,', keep: '', sep: ' ', tail: '+1 \u00b7 1 preference'}],
        // A value named by its field is the part that is kept whole; only the field name gives way.
        ['a field-named value alone', [days('Maximum office days per week')],
            {lead: 'Requires: Maximum office days per week', keep: ': 2', sep: '', tail: ''}],
        ['a long first requirement counts the second', [days('Maximum office days per week'), req(1), chipOf({})],
            {lead: 'Requires: Maximum office days per week', keep: ': 2,', sep: ' ', tail: '+1 \u00b7 1 preference'}],
        ['a field-named value second', [req(1), days('Days'), chipOf({})],
            {lead: 'Requires: Need1, Days', keep: ': 2', sep: ' \u00b7 ', tail: '1 preference'}],
        ['a field-named value first of two', [days('Days'), req(1), req(2)],
            {lead: 'Requires: Days: 2, Need1,', keep: '', sep: ' ', tail: '+1'}],
        ['a second requirement one character too long', [req(1), chipOf({path: 'r2', display: 'Tencharsxx', hard: true})],
            {lead: 'Requires: Need1,', keep: '', sep: ' ', tail: '+1'}],
        // A list shows its values; an excluded value alone would read as wanted, so it keeps its field's name.
        ['a list first shows its values and is cut with the lead', [chipOf({path: 'work_location.modes', label: 'Work arrangement', display: 'Remote, Hybrid', items: ['Remote', 'Hybrid'], hard: true}), chipOf({})],
            {lead: 'Requires: Remote, Hybrid', keep: '', sep: ' \u00b7 ', tail: '1 preference'}],
        ['a long list first counts the second', [chipOf({path: 'work_location.countries', label: 'Work countries', display: 'United States, Canada, United Kingdom', items: ['United States', 'Canada', 'United Kingdom'], hard: true}), req(1)],
            {lead: 'Requires: United States, Canada, United Kingdom,', keep: '', sep: ' ', tail: '+1'}],
        ['a list second that fits', [req(1), chipOf({path: 'work_location.countries', label: 'W', display: 'US, CA', items: ['US', 'CA'], hard: true})],
            {lead: 'Requires: Need1, US, CA', keep: '', sep: '', tail: ''}],
        ['a number with its unit keeps the number whole', [chipOf({path: 'compensation.minimum_equity_percent', label: 'Minimum equity (%)', display: '0.5%', hard: true})],
            {lead: 'Requires: Minimum equity (%)', keep: ': 0.5%', sep: '', tail: ''}],
        ['a number with a word as its unit keeps the number whole', [chipOf({path: 'x.years', label: 'Experience', display: '5 Years', hard: true})],
            {lead: 'Requires: Experience', keep: ': 5 Years', sep: '', tail: ''}],
        ['a one-entry list stands for itself', [chipOf({path: 'work_location.modes', label: 'Work arrangement', display: 'Remote', items: ['Remote'], hard: true})],
            {lead: 'Requires: Remote', keep: '', sep: '', tail: ''}],
        ['an excluded value alone is not read as wanted', [chipOf({path: 'exclusions.industries', label: 'Excluded industries', display: 'Gambling', items: ['Gambling'], hard: true})],
            {lead: 'Requires: Excluded industries: Gambling', keep: '', sep: '', tail: ''}],
        ['excluded values are named by their field, a list too', [chipOf({path: 'exclusions.companies', label: 'Excluded companies', display: 'Acme Corp, Globex', items: ['Acme Corp', 'Globex'], hard: true})],
            {lead: 'Requires: Excluded companies: Acme Corp, Globex', keep: '', sep: '', tail: ''}],
        ['an excluded value after a wanted one is named too', [req(1), chipOf({path: 'exclusions.titles', label: 'Ex', display: 'Mgr', hard: true})],
            {lead: 'Requires: Need1, Ex: Mgr', keep: '', sep: '', tail: ''}],
        ['a second requirement that just fits', [req(1), chipOf({path: 'r2', display: 'Ninechars', hard: true})],
            {lead: 'Requires: Need1, Ninechars', keep: '', sep: '', tail: ''}],
    ])('%s', (_name, chips, parts) => {
        expect(prioritiesSummaryParts(chips)).toEqual(parts);
        expect(prioritiesSummary(chips)).toBe(`${parts.lead}${parts.keep}${parts.sep}${parts.tail}`);
    });
});

describe('ReviewChanges compact (sidebar)', () => {
    const changes = [{path: 'compensation.minimum_salary', old: 0, new: 150000}];
    const handlers = () => ({onApply: jest.fn(), onCancel: jest.fn(), onEdit: jest.fn(), onApplySearchOnly: jest.fn()});
    const labels = () => screen.getAllByRole('button').map((b) => b.textContent);

    test('the change comes before the note and Cancel follows the primary action; every handler still fires', () => {
        const h = handlers();
        render(<ReviewChanges changes={changes} compact conflicts={['Salary']} {...h}/>);
        expect(screen.getByTestId('priorities-review')).toHaveClass('priorities-review-compact');
        expect(labels()).toEqual(['Apply to account', 'Cancel', 'Edit', 'This search only']);
        const order = Array.from(screen.getByTestId('priorities-review').children).map((el) => el.className.split(' ').pop());
        expect(order.indexOf('pref-change-list')).toBeLessThan(order.indexOf('priorities-scope-note'));
        expect(order.indexOf('pref-change-conflict')).toBeLessThan(order.indexOf('pref-change-list'));
        expect(order[order.length - 1]).toBe('chat-actions');
        labels().forEach((name) => fireEvent.click(screen.getByRole('button', {name: name!})));
        expect(h.onApply).toHaveBeenCalledTimes(1);
        expect(h.onCancel).toHaveBeenCalledTimes(1);
        expect(h.onEdit).toHaveBeenCalledTimes(1);
        expect(h.onApplySearchOnly).toHaveBeenCalledTimes(1);
    });

    test('by default the note comes first and Cancel is last; compact without the optional actions is Apply then Cancel', () => {
        const {unmount} = render(<ReviewChanges changes={changes} {...handlers()}/>);
        expect(screen.getByTestId('priorities-review')).not.toHaveClass('priorities-review-compact');
        expect(labels()).toEqual(['Apply to account', 'Edit', 'This search only', 'Cancel']);
        const order = Array.from(screen.getByTestId('priorities-review').children).map((el) => el.className.split(' ').pop());
        expect(order.indexOf('priorities-scope-note')).toBeLessThan(order.indexOf('pref-change-list'));
        unmount();
        render(<ReviewChanges changes={[]} compact onApply={jest.fn()} onCancel={jest.fn()}/>);
        expect(labels()).toEqual(['Apply to account', 'Cancel']);
        expect(screen.getByTestId('priorities-review-empty')).toBeInTheDocument();
    });
});

describe('final fixes after visual round 3 (issue #480)', () => {
    const one = [{path: 'compensation.minimum_salary', old: 0, new: 150000}];

    test('in the bounded block Apply keeps the focus through its request and ignores a second activation', () => {
        const onApply = jest.fn();
        const {rerender} = render(<ReviewChanges changes={one} compact onApply={onApply} onCancel={jest.fn()}/>);
        const apply = screen.getByRole('button', {name: 'Apply to account'});
        apply.focus();
        fireEvent.click(apply);
        expect(onApply).toHaveBeenCalledTimes(1);
        rerender(<ReviewChanges changes={one} compact pending onApply={onApply} onCancel={jest.fn()}/>);
        const busy = screen.getByRole('button', {name: 'Applying…'});
        expect(busy).toBe(apply);
        expect(busy).not.toBeDisabled();
        expect(busy).toHaveAttribute('aria-disabled', 'true');
        expect(busy).toHaveAttribute('aria-busy', 'true');
        expect(busy).toHaveFocus();
        fireEvent.click(busy);
        expect(onApply).toHaveBeenCalledTimes(1);
        // The request failed: the same button, still focused, applies again.
        rerender(<ReviewChanges changes={one} compact error="Could not apply." onApply={onApply} onCancel={jest.fn()}/>);
        expect(apply).toHaveFocus();
        expect(apply).not.toHaveAttribute('aria-disabled');
        fireEvent.click(apply);
        expect(onApply).toHaveBeenCalledTimes(2);
        // With nothing to apply it is disabled, as before.
        rerender(<ReviewChanges changes={[]} compact onApply={onApply} onCancel={jest.fn()}/>);
        expect(apply).toBeDisabled();
    });

    test('outside the bounded block Apply is disabled while its request runs, as before', () => {
        const onApply = jest.fn();
        render(<ReviewChanges changes={one} pending onApply={onApply} onCancel={jest.fn()}/>);
        const busy = screen.getByRole('button', {name: 'Applying…'});
        expect(busy).toBeDisabled();
        expect(busy).not.toHaveAttribute('aria-disabled');
    });

    describe('More in the pinned action row', () => {
        // jsdom lays nothing out: the scroller's sizes are given.
        const sizes = {scrollHeight: 300, clientHeight: 160};
        const realScroll = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollHeight');
        const realClient = Object.getOwnPropertyDescriptor(Element.prototype, 'clientHeight');
        const observers: Array<{run: () => void; observed: Element[]; disconnected: boolean}> = [];
        beforeEach(() => {
            observers.length = 0;
            Object.assign(sizes, {scrollHeight: 300, clientHeight: 160});
            Object.defineProperty(Element.prototype, 'scrollHeight', {configurable: true, get() { return sizes.scrollHeight; }});
            Object.defineProperty(Element.prototype, 'clientHeight', {configurable: true, get() { return sizes.clientHeight; }});
        });
        afterEach(() => {
            delete (global as any).ResizeObserver;
            Object.defineProperty(Element.prototype, 'scrollHeight', realScroll!);
            Object.defineProperty(Element.prototype, 'clientHeight', realClient!);
        });
        const names = () => screen.getAllByRole('button').map((b) => b.getAttribute('aria-label') || b.textContent);
        const inBlock = (node: React.ReactNode) => <div className="priorities-scroll" data-testid="scroller">{node}</div>;
        const all = () => ({onApply: jest.fn(), onCancel: jest.fn(), onEdit: jest.fn(), onApplySearchOnly: jest.fn()});

        test('is offered while the second row is out of sight, leads to it, and goes once that row shows', () => {
            const h = all();
            render(inBlock(<ReviewChanges changes={one} compact {...h}/>));
            const scroller = screen.getByTestId('scroller');
            expect(names()).toEqual(['Apply to account', 'Cancel', 'More options', 'Edit', 'This search only']);
            const more = screen.getByRole('button', {name: 'More options'});
            expect(more).toHaveTextContent('More');
            expect(more.closest('[role="group"]')).toHaveAccessibleName('Review actions');

            // Exactly the threshold left below: the row is as good as shown. One pixel more: More is back.
            scroller.scrollTop = 300 - 160 - MORE_BELOW_PX;
            fireEvent.scroll(scroller);
            expect(screen.queryByRole('button', {name: 'More options'})).not.toBeInTheDocument();
            scroller.scrollTop -= 1;
            fireEvent.scroll(scroller);

            fireEvent.click(screen.getByRole('button', {name: 'More options'}));
            expect(scroller.scrollTop).toBe(300);
            expect(screen.getByRole('button', {name: 'Edit'})).toHaveFocus();
            fireEvent.scroll(scroller);
            expect(names()).toEqual(['Apply to account', 'Cancel', 'Edit', 'This search only']);
            expect(h.onEdit).not.toHaveBeenCalled();
        });

        test('a second-row button that takes the focus by Tab shows its row', () => {
            const inside = render(inBlock(<ReviewChanges changes={one} compact {...all()}/>));
            const scroller = screen.getByTestId('scroller');
            expect(scroller.scrollTop).toBe(0);
            act(() => screen.getByRole('button', {name: 'Edit'}).focus());
            expect(scroller.scrollTop).toBe(300);
            scroller.scrollTop = 0;
            act(() => screen.getByRole('button', {name: 'This search only'}).focus());
            expect(scroller.scrollTop).toBe(300);
            // Outside the bounded block there is no pinned group to reveal: nothing scrolls.
            scroller.scrollTop = 0;
            inside.unmount();
            const outside = render(<ReviewChanges changes={one} {...all()}/>);
            act(() => outside.getByRole('button', {name: 'Edit'}).focus());
            expect(scroller.scrollTop).toBe(0);
            // The main block's review sits in a list that scrolls too, and is not compact: focusing Edit does not scroll it.
            outside.unmount();
            render(inBlock(<ReviewChanges changes={one} {...all()}/>));
            act(() => screen.getByRole('button', {name: 'Edit'}).focus());
            expect(screen.getByTestId('scroller').scrollTop).toBe(0);
            act(() => screen.getByRole('button', {name: 'This search only'}).focus());
            expect(screen.getByTestId('scroller').scrollTop).toBe(0);
        });

        test('a press on Edit or This search only does not move its row, so the click lands; Tab and a cancelled press still do', () => {
            const h = all();
            render(inBlock(<ReviewChanges changes={one} compact {...h}/>));
            const scroller = screen.getByTestId('scroller');
            for (const name of ['Edit', 'This search only']) {
                const button = screen.getByRole('button', {name});
                // The pointer goes down, the button takes the focus, the pointer comes up and the click follows.
                scroller.scrollTop = 0;
                fireEvent.pointerDown(button);
                act(() => button.focus());
                expect(scroller.scrollTop).toBe(0);
                fireEvent.pointerUp(button);
                fireEvent.click(button);
                act(() => button.blur());
                // The press is over: a later focus by Tab shows the row.
                act(() => button.focus());
                expect(scroller.scrollTop).toBe(300);
                act(() => button.blur());
                // A press that leaves the button, or is cancelled, ends as well.
                for (const end of [fireEvent.pointerLeave, fireEvent.pointerCancel]) {
                    scroller.scrollTop = 0;
                    fireEvent.pointerDown(button);
                    end(button);
                    act(() => button.focus());
                    expect(scroller.scrollTop).toBe(300);
                    act(() => button.blur());
                }
            }
            expect(h.onEdit).toHaveBeenCalledTimes(1);
            expect(h.onApplySearchOnly).toHaveBeenCalledTimes(1);
        });

        test('a touch tap, whose mouse events follow its pointerup and come before the focus, lands too', () => {
            const h = all();
            render(inBlock(<ReviewChanges changes={one} compact {...h}/>));
            const scroller = screen.getByTestId('scroller');
            for (const name of ['Edit', 'This search only']) {
                const button = screen.getByRole('button', {name});
                scroller.scrollTop = 0;
                fireEvent.pointerDown(button);
                fireEvent.pointerUp(button);
                fireEvent.mouseDown(button);
                act(() => button.focus());
                expect(scroller.scrollTop).toBe(0);
                fireEvent.mouseUp(button);
                fireEvent.click(button);
                act(() => button.blur());
                // The press is over: Tab shows the row again; so does a press that leaves the button.
                act(() => button.focus());
                expect(scroller.scrollTop).toBe(300);
                act(() => button.blur());
                scroller.scrollTop = 0;
                fireEvent.mouseDown(button);
                fireEvent.mouseLeave(button);
                act(() => button.focus());
                expect(scroller.scrollTop).toBe(300);
                act(() => button.blur());
            }
            expect(h.onEdit).toHaveBeenCalledTimes(1);
            expect(h.onApplySearchOnly).toHaveBeenCalledTimes(1);
        });

        test('This search only keeps the focus while its request runs and ignores a second press; after the failure the focus is in the pinned row', () => {
            const h = all();
            const {rerender} = render(inBlock(<ReviewChanges changes={one} compact {...h}/>));
            const search = screen.getByRole('button', {name: 'This search only'});
            act(() => search.focus());
            rerender(inBlock(<ReviewChanges changes={one} compact pending {...h}/>));
            const running = screen.getByRole('button', {name: 'This search only'});
            expect(running).toBe(search);
            expect(running).toHaveAttribute('aria-disabled', 'true');
            expect(running).not.toBeDisabled();
            expect(running).toHaveFocus();
            fireEvent.click(running);
            expect(h.onApplySearchOnly).not.toHaveBeenCalled();
            // The message pushes the second row out of sight: the focus is not left on a button nobody can see.
            rerender(inBlock(<ReviewChanges changes={one} compact error="Could not apply." {...h}/>));
            expect(screen.getByRole('button', {name: 'Apply to account'})).toHaveFocus();
            expect(screen.getByRole('button', {name: 'This search only'})).not.toHaveAttribute('aria-disabled');
            fireEvent.click(screen.getByRole('button', {name: 'This search only'}));
            expect(h.onApplySearchOnly).toHaveBeenCalledTimes(1);
        });

        test('an error moves only a focus held in the second row: one held elsewhere stays, and so does a review without the bounded block', () => {
            const h = all();
            const {rerender, unmount} = render(inBlock(<ReviewChanges changes={one} compact {...h}/>));
            act(() => screen.getByRole('button', {name: 'Cancel'}).focus());
            rerender(inBlock(<ReviewChanges changes={one} compact error="Could not apply." {...h}/>));
            expect(screen.getByRole('button', {name: 'Cancel'})).toHaveFocus();
            // A message that clears, or a second-row button of another review, is not moved either.
            const stray = document.createElement('button');
            stray.className = 'priorities-review-edit';
            document.body.appendChild(stray);
            act(() => stray.focus());
            rerender(inBlock(<ReviewChanges changes={one} compact error="Again." {...h}/>));
            expect(stray).toHaveFocus();
            stray.remove();
            rerender(inBlock(<ReviewChanges changes={one} compact error="Again." {...h}/>));
            act(() => screen.getByRole('button', {name: 'Edit'}).focus());
            rerender(inBlock(<ReviewChanges changes={one} compact {...h}/>));
            expect(screen.getByRole('button', {name: 'Edit'})).toHaveFocus();
            unmount();
            const outside = render(<ReviewChanges changes={one} {...h}/>);
            act(() => outside.getByRole('button', {name: 'This search only'}).focus());
            outside.rerender(<ReviewChanges changes={one} error="Could not apply." {...h}/>);
            expect(outside.getByRole('button', {name: 'This search only'})).toHaveFocus();
        });

        test('More while Apply is running keeps the focus on Apply, which still has it after the failure', () => {
            const h = all();
            const {rerender} = render(inBlock(<ReviewChanges changes={one} compact {...h}/>));
            const scroller = screen.getByTestId('scroller');
            const more = screen.getByRole('button', {name: 'More options'});
            rerender(inBlock(<ReviewChanges changes={one} compact pending {...h}/>));
            expect(screen.getByRole('button', {name: 'Edit'})).toBeDisabled();
            const apply = screen.getByRole('button', {name: 'Applying…'});
            fireEvent.click(more);
            expect(apply).toHaveFocus();
            expect(document.activeElement).not.toBe(document.body);
            expect(scroller.scrollTop).toBe(300);
            rerender(inBlock(<ReviewChanges changes={one} compact error="Could not apply." {...h}/>));
            expect(screen.getByRole('button', {name: 'Apply to account'})).toHaveFocus();
        });

        test('stays beside a failed Apply and beside Review latest, and leads to This search only when there is no Edit', () => {
            const {rerender} = render(inBlock(
                <ReviewChanges changes={one} compact error="Could not apply." onApply={jest.fn()} onCancel={jest.fn()} onApplySearchOnly={jest.fn()}/>,
            ));
            expect(screen.getByRole('alert')).toHaveTextContent('Could not apply.');
            fireEvent.click(screen.getByRole('button', {name: 'More options'}));
            expect(screen.getByRole('button', {name: 'This search only'})).toHaveFocus();
            rerender(inBlock(
                <ReviewChanges changes={one} compact stale error="Changed elsewhere." onApply={jest.fn()} onCancel={jest.fn()}
                               onApplySearchOnly={jest.fn()} onReviewLatest={jest.fn()}/>,
            ));
            expect(names()).toEqual(['Review latest', 'Cancel', 'More options', 'This search only']);
        });

        test('is not offered where everything fits, without a second row, or outside the bounded block', () => {
            sizes.scrollHeight = 160;
            const h = all();
            const {unmount} = render(inBlock(<ReviewChanges changes={one} compact {...h}/>));
            expect(names()).toEqual(['Apply to account', 'Cancel', 'Edit', 'This search only']);
            unmount();
            sizes.scrollHeight = 300;
            // A search-scoped review has no "This search only", and here no Edit: one row.
            const view = render(inBlock(<ReviewChanges changes={one} compact scope="search" onApply={jest.fn()} onCancel={jest.fn()} onApplySearchOnly={jest.fn()}/>));
            expect(names()).toEqual(['Apply to this search', 'Cancel']);
            // A second row arrives, then goes again: More follows it.
            view.rerender(inBlock(<ReviewChanges changes={one} compact scope="search" {...h}/>));
            expect(names()).toEqual(['Apply to this search', 'Cancel', 'More options', 'Edit']);
            view.rerender(inBlock(<ReviewChanges changes={one} compact scope="search" onApply={jest.fn()} onCancel={jest.fn()}/>));
            expect(names()).toEqual(['Apply to this search', 'Cancel']);
            view.unmount();
            // The chat's own card and the main editor's review: never.
            const outside = render(inBlock(<ReviewChanges changes={one} {...h}/>));
            expect(screen.queryByRole('button', {name: 'More options'})).not.toBeInTheDocument();
            outside.unmount();
            render(<ReviewChanges changes={one} compact {...h}/>);
            expect(screen.queryByRole('button', {name: 'More options'})).not.toBeInTheDocument();
        });

        test('follows the block as it is resized, and stops listening when the review closes', () => {
            (global as any).ResizeObserver = class {
                private entry: {run: () => void; observed: Element[]; disconnected: boolean};
                constructor(run: () => void) { this.entry = {run, observed: [], disconnected: false}; observers.push(this.entry); }
                observe(el: Element) { this.entry.observed.push(el); }
                disconnect() { this.entry.disconnected = true; }
            };
            const {unmount} = render(inBlock(<ReviewChanges changes={one} compact {...all()}/>));
            const scroller = screen.getByTestId('scroller');
            const removed = jest.spyOn(scroller, 'removeEventListener');
            expect(observers).toHaveLength(1);
            expect(observers[0].observed).toEqual([scroller, screen.getByTestId('priorities-review')]);
            expect(screen.getByRole('button', {name: 'More options'})).toBeInTheDocument();
            // The panel grew: both rows fit.
            sizes.clientHeight = 300;
            act(() => observers[0].run());
            expect(screen.queryByRole('button', {name: 'More options'})).not.toBeInTheDocument();
            unmount();
            expect(observers[0].disconnected).toBe(true);
            expect(removed).toHaveBeenCalledWith('scroll', expect.any(Function));
        });
    });
});
