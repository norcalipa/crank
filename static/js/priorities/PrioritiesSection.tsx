// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Priorities section (issue #480): chips, inline editor, review, applied
// summary with Undo, and Reset priorities. One component serves the main
// workspace and the assistant sidebar (`variant`); the workspace store
// ensures at most one editor is open and carries the post-write revision so
// other roots (job matches) refetch.

import * as React from 'react';
import {
    ApiFailure, AppliedResult, EditorField, PrioritiesSnapshot, Proposal,
    applyProposal, proposePriorities, readPriorities, resetPriorities, undoApplied,
} from './api';
import AppliedChanges from './AppliedChanges';
import PriorityChips, {PriorityChipsSkeleton} from './PriorityChips';
import PriorityEditor from './PriorityEditor';
import ReviewChanges from './ReviewChanges';
import {preferencePathLabel} from './format';
import {Draft, DraftValue, buildPatch, emptyDraft, isDirty} from './patch';
import {prioritiesSurface, subscribeDesktop} from './surface';
import {useWorkspace} from './useWorkspace';
import {createLatestGuard} from '../workspace/requests';
import {setPrioritiesEditorOpen, setPrioritiesRevision} from '../workspace/store';

export type PrioritiesVariant = 'main' | 'sidebar';

interface Props {
    variant: PrioritiesVariant;
    // false/undefined-unknown: signed out. The sidebar renders nothing unless true.
    authenticated: boolean;
}

type Phase = 'loading' | 'ready' | 'error';
type Step = 'view' | 'edit' | 'review' | 'applied' | 'confirm-reset';

// Scroll region for the editor / review. The border appears once scrolled so
// content can never be sliced under the title without a visible edge.
const ScrollRegion: React.FC<{children: React.ReactNode}> = ({children}) => {
    const [scrolled, setScrolled] = React.useState(false);
    return (
        <div className={`priorities-scroll${scrolled ? ' is-scrolled' : ''}`}
             onScroll={(e) => setScrolled(e.currentTarget.scrollTop > 0)}>
            {children}
        </div>
    );
};

const STALE_COPY = 'Your priorities changed elsewhere. Review the latest before applying.';

const PrioritiesSection: React.FC<Props> = ({variant, authenticated}) => {
    const workspace = useWorkspace();
    const accountKey = workspace.account.key;
    const [phase, setPhase] = React.useState<Phase>('loading');
    const [snapshot, setSnapshot] = React.useState<PrioritiesSnapshot | null>(null);
    const [loadError, setLoadError] = React.useState<string | null>(null);
    const [step, setStep] = React.useState<Step>('view');
    const [draft, setDraft] = React.useState<Draft>(emptyDraft);
    const [fieldErrors, setFieldErrors] = React.useState<Record<string, string[]>>({});
    const [formError, setFormError] = React.useState<string | null>(null);
    const [busy, setBusy] = React.useState(false);
    const [proposal, setProposal] = React.useState<Proposal | null>(null);
    const [reviewError, setReviewError] = React.useState<string | null>(null);
    const [stale, setStale] = React.useState(false);
    const [applied, setApplied] = React.useState<{result: AppliedResult; summary: string} | null>(null);
    const [undoPending, setUndoPending] = React.useState(false);
    const [undoError, setUndoError] = React.useState<string | null>(null);
    const [undone, setUndone] = React.useState(false);
    const guard = React.useMemo(createLatestGuard, []);
    const mounted = React.useRef(true);
    const revisionRef = React.useRef<number | null>(null);

    const load = React.useCallback(async () => {
        const request = guard.begin();
        setPhase((p) => (p === 'ready' ? p : 'loading'));
        try {
            const data = await readPriorities(request.signal);
            if (!request.isLatest() || !mounted.current) return;
            revisionRef.current = data.revision;
            setSnapshot(data);
            setLoadError(null);
            setPhase('ready');
        } catch (err) {
            if (!request.isLatest() || !mounted.current) return;
            setLoadError(err instanceof ApiFailure ? err.message : 'Could not load your priorities.');
            setPhase('error');
        }
    }, [guard]);

    const clearAll = React.useCallback(() => {
        guard.cancel();
        revisionRef.current = null;
        setSnapshot(null);
        setStep('view');
        setDraft(emptyDraft());
        setFieldErrors({});
        setFormError(null);
        setProposal(null);
        setApplied(null);
        setPhase('loading');
    }, [guard]);

    React.useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
            guard.cancel();
        };
    }, [guard]);

    // Load for the current account; drop everything when the account changes
    // or private state is purged, so no priorities survive a switch.
    React.useEffect(() => {
        clearAll();
        if (authenticated) {
            void load();
        }
        const purge = () => clearAll();
        document.addEventListener('crank:private-state-purged', purge);
        return () => document.removeEventListener('crank:private-state-purged', purge);
    }, [authenticated, accountKey, clearAll, load]);

    // A write elsewhere on the page (chat apply/undo, other section) bumps the
    // store revision; refetch once when it differs from what is shown.
    React.useEffect(() => {
        const rev = workspace.prioritiesRevision;
        if (authenticated && rev !== null && rev !== revisionRef.current) {
            void load();
        }
    }, [workspace.prioritiesRevision, authenticated, load]);

    // The store names which surface hosts the editor: open it here, close it
    // here when another surface takes over or the store is purged.
    const hostsEditor = workspace.prioritiesEditorOpenIn === variant;
    React.useEffect(() => {
        if (hostsEditor && step === 'view' && snapshot) {
            setDraft(emptyDraft());
            setFieldErrors({});
            setFormError(null);
            setStep('edit');
        } else if (!hostsEditor) {
            // Functional: a write may have just moved the step to 'applied'.
            setStep((current) => (current === 'edit' || current === 'review' ? 'view' : current));
        }
    }, [hostsEditor, step, snapshot]);

    const close = () => {
        setStep('view');
        setPrioritiesEditorOpen(null);
    };

    const fields: EditorField[] = snapshot?.fields || [];
    const dirty = isDirty(fields, draft);
    const labels = React.useMemo(
        () => Object.fromEntries(fields.map((f) => [
            f.path,
            f.path.includes('.') ? `${preferencePathLabel(f.path).split(' \u203a ')[0]} \u203a ${f.label}` : f.label,
        ])) as Record<string, string>,
        [fields],
    );

    const startReview = async (rebaseFrom?: Draft) => {
        if (!snapshot || busy) return;
        const patch = buildPatch(snapshot.fields, snapshot.preferences, rebaseFrom || draft);
        if (!patch) return;
        setBusy(true);
        setFormError(null);
        setFieldErrors({});
        try {
            const next = await proposePriorities(patch, 'account');
            setProposal(next);
            setReviewError(null);
            setStale(false);
            setStep('review');
        } catch (err) {
            if (err instanceof ApiFailure && Object.keys(err.fieldErrors).length > 0) {
                setFieldErrors(err.fieldErrors);
                setFormError('Some values are not valid. Fix them and review again.');
            } else {
                setFormError(err instanceof ApiFailure ? err.message : 'Could not check your changes.');
            }
            setStep('edit');
        } finally {
            setBusy(false);
        }
    };

    const finishWrite = (result: AppliedResult, summary: string) => {
        setApplied({result, summary});
        setUndone(false);
        setUndoError(null);
        setStep('applied');
        setPrioritiesEditorOpen(null);
        if (result.revision !== null) {
            revisionRef.current = result.revision;
            setPrioritiesRevision(result.revision);
        }
        void load();
    };

    const apply = async (scope: 'account' | 'search') => {
        if (!proposal || busy) return;
        setBusy(true);
        setReviewError(null);
        try {
            const result = await applyProposal({...proposal.token, scope});
            finishWrite(
                result,
                scope === 'search'
                    ? `This search only \u2014 not saved. ${result.matchCount ?? 0} matches.`
                    : 'Priorities saved. Job matches will update.',
            );
        } catch (err) {
            const failure = err instanceof ApiFailure ? err : null;
            if (failure?.stale) {
                setStale(true);
                setReviewError(STALE_COPY);
            } else {
                setReviewError(failure?.message || 'Could not apply your changes. Try again.');
            }
        } finally {
            setBusy(false);
        }
    };

    const reviewLatest = async () => {
        await load();
        await startReview();
    };

    const undo = async () => {
        const token = applied?.result.undo;
        if (!token || undoPending) return;
        setUndoPending(true);
        setUndoError(null);
        try {
            const revision = await undoApplied(token);
            setUndone(true);
            if (revision !== null) {
                revisionRef.current = revision;
                setPrioritiesRevision(revision);
            }
            void load();
        } catch (err) {
            const failure = err instanceof ApiFailure ? err : null;
            setUndoError(failure?.stale
                ? 'Your priorities changed since this update, so it can no longer be undone.'
                : (failure?.message || 'Could not undo. Try again.'));
        } finally {
            setUndoPending(false);
        }
    };

    const reset = async () => {
        if (!snapshot || busy) return;
        setBusy(true);
        setFormError(null);
        try {
            const result = await resetPriorities(snapshot.revision);
            finishWrite(result, 'Priorities reset to defaults. Job matches will update.');
        } catch (err) {
            const failure = err instanceof ApiFailure ? err : null;
            setFormError(failure?.stale
                ? 'Your priorities changed elsewhere. Reload to see the latest, then try again.'
                : (failure?.message || 'Could not reset priorities. Try again.'));
            setStep('view');
            if (failure?.stale) void load();
        } finally {
            setBusy(false);
        }
    };

    const className = `priorities-section priorities-${variant}`;

    // Signed out: the page's own sign-in prompt is the call to action.
    if (!authenticated) return null;

    const titleId = `priorities-title-${variant}`;
    let body: React.ReactNode;
    if (phase === 'loading' && !snapshot) {
        body = <div aria-busy="true"><PriorityChipsSkeleton/><span className="visually-hidden" role="status">Loading your priorities</span></div>;
    } else if (phase === 'error' && !snapshot) {
        body = (
            <div className="priorities-load-error" role="alert" data-testid="priorities-load-error">
                <i className="fa-solid fa-triangle-exclamation priorities-load-error-icon" aria-hidden="true"></i>
                <div className="priorities-load-error-text">
                    <strong>Couldn\u2019t load your priorities.</strong>
                    {loadError && <span className="d-block small">{loadError}</span>}
                </div>
                <button type="button" className="btn btn-sm btn-outline-light priorities-load-error-retry" onClick={() => void load()}>Try again</button>
            </div>
        );
    } else if (step === 'edit' && snapshot) {
        body = (
            <ScrollRegion>
                <PriorityEditor fields={fields} draft={draft} fieldErrors={fieldErrors} dirty={dirty}
                                pending={busy} formError={formError} idPrefix={`priority-${variant}`}
                                onChange={(path: string, value: DraftValue) =>
                                    setDraft((d) => ({...d, values: {...d.values, [path]: value}}))}
                                onToggleHard={(path, hard) =>
                                    setDraft((d) => ({...d, hard: {...d.hard, [path]: hard}}))}
                                onReview={() => void startReview()} onCancel={close}/>
            </ScrollRegion>
        );
    } else if (step === 'review' && proposal) {
        body = (
            <ScrollRegion>
                <ReviewChanges changes={proposal.changes} labels={labels} pending={busy} error={reviewError} stale={stale}
                               onApply={() => void apply('account')}
                               onApplySearchOnly={() => void apply('search')}
                               onEdit={() => setStep('edit')} onCancel={close}
                               onReviewLatest={() => void reviewLatest()}/>
            </ScrollRegion>
        );
    } else if (step === 'applied' && applied) {
        body = (
            <AppliedChanges changes={applied.result.changes} labels={labels} summary={applied.summary}
                            canUndo={applied.result.undo !== null} undoPending={undoPending}
                            undoError={undoError} undone={undone} onUndo={() => void undo()}
                            onDismiss={() => setStep('view')}/>
        );
    } else if (step === 'confirm-reset') {
        body = (
            <div className="priorities-card priorities-confirm" role="group" aria-label="Confirm reset">
                <p className="priorities-heading mb-1">Reset priorities?</p>
                <p>Reset all saved priorities to their defaults? Your conversations are not changed. Job matches will update.</p>
                <div className="chat-actions" role="group" aria-label="Reset actions">
                    <button type="button" className="btn btn-sm btn-danger"
                            onClick={() => void reset()} disabled={busy} aria-busy={busy}>
                        {busy ? 'Resetting…' : 'Reset priorities'}
                    </button>
                    <button type="button" className="btn btn-sm btn-outline-light"
                            onClick={() => setStep('view')} disabled={busy}>Keep priorities</button>
                </div>
            </div>
        );
    } else {
        const chips = snapshot?.chips || [];
        body = (
            <>
                {formError && (
                    <div className="priorities-load-error" role="alert" data-testid="priorities-view-error">
                        <i className="fa-solid fa-triangle-exclamation priorities-load-error-icon" aria-hidden="true"></i>
                        <div className="priorities-load-error-text">{formError}</div>
                    </div>
                )}
                {chips.length > 0 ? (
                    <PriorityChips chips={chips} onEdit={() => setPrioritiesEditorOpen(variant)}/>
                ) : (
                    <p className="priorities-empty" data-testid="priorities-empty">
                        No priorities saved yet. Add a few so job matches fit what you want.
                    </p>
                )}
                <div className="priorities-actions" role="group" aria-label="Priorities actions">
                    <button type="button" className="btn btn-sm btn-outline-primary priorities-edit"
                            onClick={() => setPrioritiesEditorOpen(variant)}>
                        {chips.length > 0 ? 'Edit priorities' : 'Add priorities'}
                    </button>
                    {chips.length > 0 && (
                        <button type="button" className="btn btn-sm btn-link link-danger priorities-reset"
                                onClick={() => setStep('confirm-reset')}>Reset priorities</button>
                    )}
                </div>
            </>
        );
    }

    return (
        <section className={className} aria-labelledby={titleId} data-testid={`priorities-${variant}`}>
            <h2 id={titleId} className="h6 priorities-title">Your priorities</h2>
            {body}
        </section>
    );
};

export const SidebarPriorities: React.FC = () => {
    const workspace = useWorkspace();
    const surface = React.useSyncExternalStore(subscribeDesktop, prioritiesSurface, () => 'sidebar' as const);
    if (surface === 'main') return null;
    return <PrioritiesSection variant="sidebar" authenticated={workspace.account.status === 'authenticated'}/>;
};

export default PrioritiesSection;
