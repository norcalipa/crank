// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Priorities section (issue #480): chips, inline editor, review, applied
// summary with Undo, and Reset priorities. One component serves the main
// workspace and the assistant sidebar (`variant`); the workspace store
// ensures at most one editor is open and carries the post-write revision so
// other roots (job matches) refetch. The sidebar shows one collapsed summary
// row that expands to the chips, so the conversation below keeps its height.

import * as React from 'react';
import {
    ApiFailure, AppliedResult, EditorField, GENERIC_ERROR_MESSAGE, PrioritiesSnapshot, Proposal,
    SESSION_EXPIRED_MESSAGE, applyProposal, proposePriorities, readPriorities, resetPriorities, undoApplied,
} from './api';
import AppliedChanges from './AppliedChanges';
import PriorityChips, {PriorityChipsSkeleton} from './PriorityChips';
import PriorityEditor from './PriorityEditor';
import ReviewChanges from './ReviewChanges';
import {preferencePathLabel, prioritiesSummary} from './format';
import {Draft, DraftValue, buildPatch, conflictingPaths, emptyDraft, isDirty, patchToDraft} from './patch';
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

// The page's own sign-in link when it renders one; otherwise the login page, returning to the current page.
function signInHref(): string {
    const {pathname, search} = window.location;
    return document.getElementById('priorities-main')?.dataset.signInUrl
        || `/accounts/login/?next=${encodeURIComponent(pathname + search)}`;
}

const INVALID_COPY = 'These changes could not be checked. Review your edits and try again.';

function searchOnlyCopy(result: AppliedResult): string {
    const count = result.matchCount ?? 0;
    const jobs = `${count}${result.matchCapped ? '+' : ''} ${count === 1 && !result.matchCapped ? 'job matches' : 'jobs match'}`;
    return `This search only \u2014 not saved. ${jobs} these priorities. The list below still reflects your saved priorities.`;
}

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
    const [conflicts, setConflicts] = React.useState<string[]>([]);
    const [sessionExpired, setSessionExpired] = React.useState(false);
    const [rebasing, setRebasing] = React.useState(false);
    const [refreshed, setRefreshed] = React.useState(false);
    const [expanded, setExpanded] = React.useState(false);
    const guard = React.useMemo(createLatestGuard, []);
    const mounted = React.useRef(true);
    const revisionRef = React.useRef<number | null>(null);
    // Writes (propose / apply / undo / reset) belong to the account that started them: a purge or
    // account switch bumps the epoch and aborts them, so a late answer is never shown to, or
    // undone under, a different login.
    const epoch = React.useRef(0);
    const writes = React.useRef(new Set<AbortController>());
    const seeded = React.useRef<unknown>(null);
    const appliedRef = React.useRef<unknown>(null);
    const focusAfter = React.useRef<'edit' | 'reset' | null>(null);
    const editButton = React.useRef<HTMLButtonElement>(null);
    const resetButton = React.useRef<HTMLButtonElement>(null);
    const confirmHeading = React.useRef<HTMLParagraphElement>(null);

    const beginWrite = () => {
        const controller = new AbortController();
        writes.current.add(controller);
        const owner = epoch.current;
        return {
            signal: controller.signal,
            live: () => mounted.current && epoch.current === owner && !controller.signal.aborted,
            done: () => { writes.current.delete(controller); },
        };
    };
    const dropWrites = () => {
        epoch.current += 1;
        writes.current.forEach((controller) => controller.abort());
        writes.current.clear();
    };

    const load = React.useCallback(async (): Promise<PrioritiesSnapshot | null> => {
        const request = guard.begin();
        setPhase((p) => (p === 'ready' ? p : 'loading'));
        try {
            const data = await readPriorities(request.signal);
            if (!request.isLatest() || !mounted.current) return null;
            revisionRef.current = data.revision;
            setSnapshot(data);
            setLoadError(null);
            setPhase('ready');
            return data;
        } catch (err) {
            if (!request.isLatest() || !mounted.current) return null;
            setSessionExpired(err instanceof ApiFailure && err.authRequired);
            setLoadError(err instanceof ApiFailure ? err.message : 'Could not load your priorities.');
            setPhase('error');
            return null;
        }
    }, [guard]);

    const clearAll = React.useCallback(() => {
        guard.cancel();
        epoch.current += 1;
        writes.current.forEach((controller) => controller.abort());
        writes.current.clear();
        revisionRef.current = null;
        seeded.current = null;
        setSnapshot(null);
        setStep('view');
        setDraft(emptyDraft());
        setFieldErrors({});
        setFormError(null);
        setProposal(null);
        setReviewError(null);
        setStale(false);
        setConflicts([]);
        setApplied(null);
        setUndone(false);
        setUndoError(null);
        setUndoPending(false);
        setBusy(false);
        setSessionExpired(false);
        setRebasing(false);
        setRefreshed(false);
        setExpanded(false);
        setPhase('loading');
    }, [guard]);

    React.useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
            guard.cancel();
            dropWrites();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
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
    const editorSeed = workspace.prioritiesEditorSeed;
    React.useEffect(() => {
        const newSeed = !!editorSeed && editorSeed !== seeded.current;
        const working = step === 'edit' || step === 'review';
        if (hostsEditor && snapshot && (!working || newSeed)) {
            // A seed (the assistant's proposal) fills the editor instead of the saved values. Asking to edit from the
            // applied summary or the reset prompt leaves that step; a seed arriving while editing or reviewing merges
            // into the draft, so typed values the proposal does not touch are kept.
            seeded.current = editorSeed;
            setDraft((current) => {
                const fromSeed = newSeed ? patchToDraft(snapshot.fields, editorSeed, working ? current : undefined) : emptyDraft();
                return working
                    ? {values: {...current.values, ...fromSeed.values}, hard: {...current.hard, ...fromSeed.hard}}
                    : fromSeed;
            });
            setFieldErrors({});
            setFormError(null);
            setStep('edit');
        } else if (!hostsEditor) {
            // The next open request is a new one, even when it carries the same seed object.
            seeded.current = null;
            // Functional: a write may have just moved the step to 'applied'. A summary not yet dismissed returns, with its Undo.
            setStep((current) => (current === 'edit' || current === 'review' ? (appliedRef.current ? 'applied' : 'view') : current));
        }
    }, [hostsEditor, editorSeed, step, snapshot]);

    // Focus follows the user's action: back to the control that opened the step they left.
    React.useEffect(() => {
        if (step === 'confirm-reset') {
            confirmHeading.current?.focus();
        } else if (step === 'view' && focusAfter.current) {
            const target = focusAfter.current === 'reset' ? resetButton.current || editButton.current : editButton.current;
            focusAfter.current = null;
            target?.focus();
        }
    }, [step]);

    appliedRef.current = applied;

    const close = () => {
        if (applied) {
            setStep('applied');
        } else {
            focusAfter.current = 'edit';
            setStep('view');
        }
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

    // `base` is the document the patch is built against: the reload after a conflict, not the
    // snapshot the draft was started from, so another tab's changes are kept, not overwritten.
    const startReview = async (base: PrioritiesSnapshot | null = snapshot, found: string[] = []) => {
        if (!base || busy) return;
        const patch = buildPatch(base.fields, base.preferences, draft);
        if (!patch) {
            setFormError('Nothing left to change: the latest saved priorities already match your edit.');
            setStep('edit');
            return;
        }
        const write = beginWrite();
        setBusy(true);
        setFormError(null);
        setFieldErrors({});
        try {
            const next = await proposePriorities(patch, 'account', write.signal);
            if (!write.live()) return;
            setRefreshed(base !== snapshot);
            setProposal(next);
            setReviewError(null);
            setStale(false);
            setConflicts(found);
            setStep('review');
        } catch (err) {
            if (!write.live()) return;
            if (err instanceof ApiFailure && Object.keys(err.fieldErrors).length > 0) {
                setFieldErrors(err.fieldErrors);
                setFormError('Some values are not valid. Fix them and review again.');
            } else {
                setSessionExpired(err instanceof ApiFailure && err.authRequired);
                // A 400 carries the server's validation wording; users get friendly copy, never that text.
                setFormError(err instanceof ApiFailure
                    ? (err.status === 400 ? INVALID_COPY : err.message)
                    : 'Could not check your changes.');
            }
            setStep('edit');
        } finally {
            if (write.live()) setBusy(false);
            write.done();
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
        const write = beginWrite();
        setBusy(true);
        setReviewError(null);
        try {
            const result = await applyProposal({...proposal.token, scope}, write.signal);
            if (!write.live()) return;
            finishWrite(
                result,
                scope === 'search' ? searchOnlyCopy(result) : 'Priorities saved. Job matches will update.',
            );
        } catch (err) {
            if (!write.live()) return;
            const failure = err instanceof ApiFailure ? err : null;
            if (failure?.stale) {
                setStale(true);
                setReviewError(STALE_COPY);
            } else {
                setSessionExpired(!!failure?.authRequired);
                setReviewError(failure?.message || 'Could not apply your changes. Try again.');
            }
        } finally {
            if (write.live()) setBusy(false);
            write.done();
        }
    };

    const reviewLatest = async () => {
        if (rebasing) return;
        const before = snapshot;
        setRefreshed(false);
        setRebasing(true);
        try {
            const fresh = await load();
            if (!fresh) {
                if (mounted.current) setReviewError('Could not load your latest priorities. Try again.');
                return;
            }
            const found = before ? conflictingPaths(before.fields, fresh.fields, draft).map((path) => labels[path] || path) : [];
            await startReview(fresh, found);
        } finally {
            if (mounted.current) setRebasing(false);
        }
    };

    const undo = async () => {
        const token = applied?.result.undo;
        if (!token || undoPending) return;
        const write = beginWrite();
        setUndoPending(true);
        setUndoError(null);
        try {
            const revision = await undoApplied(token, write.signal);
            if (!write.live()) return;
            setUndone(true);
            if (revision !== null) {
                revisionRef.current = revision;
                setPrioritiesRevision(revision);
            }
            void load();
        } catch (err) {
            if (!write.live()) return;
            const failure = err instanceof ApiFailure ? err : null;
            setSessionExpired(!!failure?.authRequired);
            setUndoError(failure?.stale
                ? 'Your priorities changed since this update, so it can no longer be undone.'
                : (failure?.message || 'Could not undo. Try again.'));
        } finally {
            if (write.live()) setUndoPending(false);
            write.done();
        }
    };

    const reset = async () => {
        if (!snapshot || busy) return;
        const write = beginWrite();
        setBusy(true);
        setFormError(null);
        try {
            const result = await resetPriorities(snapshot.revision, write.signal);
            if (!write.live()) return;
            finishWrite(result, 'Priorities reset to defaults. Job matches will update.');
        } catch (err) {
            if (!write.live()) return;
            const failure = err instanceof ApiFailure ? err : null;
            setSessionExpired(!!failure?.authRequired);
            setFormError(failure?.stale
                ? 'Your priorities changed elsewhere. Reload to see the latest, then try again.'
                : (failure?.message || 'Could not reset priorities. Try again.'));
            focusAfter.current = 'reset';
            setStep('view');
            if (failure?.stale) void load();
        } finally {
            if (write.live()) setBusy(false);
            write.done();
        }
    };

    const className = `priorities-section priorities-${variant}`;
    const compensation = snapshot?.preferences.compensation as {currency?: unknown} | undefined;
    const currency = compensation?.currency;
    const choicePaths = new Set(fields.filter((f) => f.choices).map((f) => f.path));
    const readOnlyPaths = new Set(fields.filter((f) => f.type === 'float_map').map((f) => f.path));
    const titleId = `priorities-title-${variant}`;
    const detailsId = `priorities-details-${variant}`;
    const sidebar = variant === 'sidebar';
    // The sidebar is bounded to a share of the panel, so every step scrolls inside it.
    const bounded = (node: React.ReactNode) => (sidebar ? <ScrollRegion>{node}</ScrollRegion> : node);
    let summaryRow = false;

    // Signed out: the page's own sign-in prompt is the call to action; the main block says what signing in is for.
    if (!authenticated) {
        if (variant !== 'main') return null;
        return (
            <section className={className} aria-labelledby={titleId} data-testid="priorities-main">
                <h2 id={titleId} className="h6 priorities-title">Your priorities</h2>
                <p className="priorities-empty" data-testid="priorities-signed-out">Sign in to save your priorities.</p>
            </section>
        );
    }

    let body: React.ReactNode;
    if (phase === 'loading' && !snapshot) {
        body = <div aria-busy="true"><PriorityChipsSkeleton/><span className="visually-hidden" role="status">Loading your priorities</span></div>;
    } else if (phase === 'error' && !snapshot) {
        body = (
            <div className="priorities-load-error" role="alert" data-testid="priorities-load-error">
                <i className="fa-solid fa-triangle-exclamation priorities-load-error-icon" aria-hidden="true"></i>
                <div className="priorities-load-error-text">
                    <strong>Couldn’t load your priorities.</strong>
                    {loadError && loadError !== GENERIC_ERROR_MESSAGE && <span className="d-block small">{loadError}</span>}
                </div>
                {!sessionExpired && (
                    <button type="button" className="btn btn-sm btn-outline-light priorities-load-error-retry" onClick={() => void load()}>Try again</button>
                )}
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
                                onUndoClear={(path) => setDraft((d) => {
                                    const values = {...d.values};
                                    delete values[path];
                                    return {...d, values};
                                })}
                                onReview={() => void startReview()} onCancel={close}/>
            </ScrollRegion>
        );
    } else if (step === 'review' && proposal) {
        body = (
            <ScrollRegion>
                <ReviewChanges key={proposal.id} changes={proposal.changes} labels={labels} currency={currency}
                               choicePaths={choicePaths} conflicts={conflicts} pending={busy || rebasing}
                               error={reviewError} stale={stale}
                               onApply={() => void apply('account')}
                               onApplySearchOnly={() => void apply('search')}
                               onEdit={() => setStep('edit')} onCancel={close}
                               onReviewLatest={() => void reviewLatest()}/>
            </ScrollRegion>
        );
    } else if (step === 'applied' && applied) {
        body = bounded(
            <AppliedChanges changes={applied.result.changes} labels={labels} currency={currency}
                            choicePaths={choicePaths} summary={applied.summary}
                            canUndo={applied.result.undo !== null} undoPending={undoPending}
                            undoError={undoError} undone={undone} onUndo={() => void undo()}
                            onDismiss={() => { focusAfter.current = 'edit'; setApplied(null); setStep('view'); }}/>,
        );
    } else if (step === 'confirm-reset') {
        body = bounded(
            <div className="priorities-card priorities-confirm" role="group" aria-label="Confirm reset">
                <p className="priorities-heading mb-1" tabIndex={-1} ref={confirmHeading}>Reset priorities?</p>
                <p>Reset all saved priorities to their defaults? Your conversations are not changed. Job matches will update.</p>
                <div className="chat-actions" role="group" aria-label="Reset actions">
                    <button type="button" className="btn btn-sm btn-danger"
                            onClick={() => void reset()} disabled={busy} aria-busy={busy}>
                        {busy ? 'Resetting…' : 'Reset priorities'}
                    </button>
                    <button type="button" className="btn btn-sm btn-outline-light"
                            onClick={() => { focusAfter.current = 'reset'; setStep('view'); }} disabled={busy}>Keep priorities</button>
                </div>
            </div>,
        );
    } else {
        const chips = snapshot?.chips || [];
        const viewError = formError && (
            <div className="priorities-load-error" role="alert" data-testid="priorities-view-error">
                <i className="fa-solid fa-triangle-exclamation priorities-load-error-icon" aria-hidden="true"></i>
                <div className="priorities-load-error-text">{formError}</div>
            </div>
        );
        const resetAction = (
            <button type="button" className="btn btn-sm btn-link link-danger priorities-reset" ref={resetButton}
                    onClick={() => setStep('confirm-reset')}>Reset priorities</button>
        );
        if (sidebar) {
            // One row: a disclosure with the summary, and Edit beside it (never nested in it).
            summaryRow = true;
            const open = expanded && chips.length > 0;
            body = (
                <>
                    <div className="priorities-summary-row">
                        <h2 className="priorities-summary-heading">
                            {chips.length > 0 ? (
                                <button type="button" className="priorities-summary-toggle"
                                        data-testid="priorities-summary-toggle"
                                        aria-expanded={open} aria-controls={detailsId}
                                        onClick={() => setExpanded((was) => !was)}>
                                    <span className="priorities-summary-text">
                                        <span id={titleId} className="priorities-summary-title">
                                            Your priorities
                                            <i className={`fa-solid fa-chevron-${open ? 'up' : 'down'} priorities-summary-chevron`} aria-hidden="true"></i>
                                        </span>
                                        <span className="priorities-summary" data-testid="priorities-summary">
                                            {prioritiesSummary(chips, currency, choicePaths)}
                                        </span>
                                    </span>
                                </button>
                            ) : (
                                <span className="priorities-summary-text">
                                    <span id={titleId} className="priorities-summary-title">Your priorities</span>
                                    <span className="priorities-summary" data-testid="priorities-empty">None saved yet</span>
                                </span>
                            )}
                        </h2>
                        <button type="button" className="btn btn-sm btn-outline-primary priorities-edit" ref={editButton}
                                onClick={() => setPrioritiesEditorOpen(variant)}>
                            {chips.length > 0 ? 'Edit' : 'Add'}<span className="visually-hidden"> priorities</span>
                        </button>
                    </div>
                    {viewError}
                    <div id={detailsId} className="priorities-details" hidden={!open}>
                        {open && (
                            <>
                                <ScrollRegion>
                                    <PriorityChips chips={chips} collapsedCount={chips.length}
                                                   currency={currency} readOnlyPaths={readOnlyPaths} choicePaths={choicePaths}
                                                   onEdit={() => setPrioritiesEditorOpen(variant)}/>
                                </ScrollRegion>
                                <div className="priorities-actions" role="group" aria-label="Priorities actions">
                                    {resetAction}
                                </div>
                            </>
                        )}
                    </div>
                </>
            );
        } else {
            body = (
                <>
                    {viewError}
                    {chips.length > 0 ? (
                        <PriorityChips chips={chips} currency={currency} readOnlyPaths={readOnlyPaths}
                                       choicePaths={choicePaths} onEdit={() => setPrioritiesEditorOpen(variant)}/>
                    ) : (
                        <p className="priorities-empty" data-testid="priorities-empty">
                            No priorities saved yet. Add a few so job matches fit what you want.
                        </p>
                    )}
                    <div className="priorities-actions" role="group" aria-label="Priorities actions">
                        <button type="button" className="btn btn-sm btn-outline-primary priorities-edit" ref={editButton}
                                onClick={() => setPrioritiesEditorOpen(variant)}>
                            {chips.length > 0 ? 'Edit priorities' : 'Add priorities'}
                        </button>
                        {chips.length > 0 && resetAction}
                    </div>
                </>
            );
        }
    }

    return (
        <section className={className} aria-labelledby={titleId} data-testid={`priorities-${variant}`}>
            {!summaryRow && <h2 id={titleId} className="h6 priorities-title">Your priorities</h2>}
            {sessionExpired && (
                <p className="priorities-session-expired" role="alert" data-testid="priorities-session-expired">
                    {SESSION_EXPIRED_MESSAGE} <a href={signInHref()}>Sign in</a>
                </p>
            )}
            <span className="visually-hidden" role="status" data-testid="priorities-announcement">
                {step === 'review' && refreshed ? 'Updated against your latest priorities.' : ''}
            </span>
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
