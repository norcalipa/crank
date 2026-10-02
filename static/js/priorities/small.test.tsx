// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import '@testing-library/jest-dom';
import * as React from 'react';
import {fireEvent, render, screen} from '@testing-library/react';
import PriorityChips from './PriorityChips';
import ReviewChanges from './ReviewChanges';
import AppliedChanges from './AppliedChanges';
import {chipValueLabel, preferencePathLabel, preferenceValueLabel} from './format';

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
        expect(screen.getByTestId('priority-chip-legend')).toBeInTheDocument();
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
