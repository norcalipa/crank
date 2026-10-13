// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {EvidenceData} from '../evidence/EvidenceDetails';
import {setCachedProvenance} from '../provenanceCache';
import {getWorkspaceSnapshot, subscribeWorkspace} from '../workspace/store';
import {isCompanyId} from './companyId';

// Fired on `window` by the correction form once a suggestion is stored, so a
// view that stays open shows it under "Your pending suggestions" (issue #486).
export const COMPANY_CORRECTION_SAVED_EVENT = 'crank:company-correction-saved';

export interface ScoreDetail {
    type__name: string;
    avg_score: number;
}

interface ProvenanceObservation {
    source_url: string;
    observed_domain: string;
    observed_at: string;
    extraction_version: string;
    status: string;
    is_verified?: boolean;
}

interface ProvenanceData {
    organization_id: number;
    organization_modified: string | null;
    organization_created: string | null;
    latest_observation: ProvenanceObservation | null;
    displayed_values?: Record<string, string>;
}

export type Provenance = ProvenanceData & EvidenceData;

export interface PendingCorrection {
    id: number;
    field_key: string;
    field_label: string;
    proposed_value: string;
    status: string;
    status_label: string;
}

export interface CompanyDataOptions {
    // The caller already has the score breakdown (the list row carried
    // `avg_scores`), so the scores request is not made.
    skipScores?: boolean;
}

export interface CompanyData {
    scores: ScoreDetail[];
    scoresLoading: boolean;
    provenance: Provenance | null;
    provenanceLoading: boolean;
    // Starts the provenance request again on the user's behalf.
    retryProvenance: () => void;
    // Counts the failures of attempts the user started with retryProvenance
    // (never the first, automatic one), so the caller can hand focus back to
    // its Retry control.
    retryFailures: number;
    pendingCorrections: PendingCorrection[];
}

// Every piece of loaded state carries the company it was loaded for. A value
// whose id is not the current one is never returned, so nothing of one company
// can be shown for another, not even for the render between an id change and
// the effect that starts the new requests (issue #486).
interface ScoresState {
    id: number | null;
    items: ScoreDetail[];
    loading: boolean;
}

interface ProvenanceState {
    id: number | null;
    data: Provenance | null;
    loading: boolean;
}

interface CorrectionsState {
    id: number | null;
    items: PendingCorrection[];
}

const NO_SCORES: ScoreDetail[] = [];
const NO_CORRECTIONS: PendingCorrection[] = [];

/**
 * The three reads behind the company details: score breakdown, provenance
 * (field evidence) and the requester's own pending suggestions.
 *
 * Keyed on the company id. `null` means no company is shown: nothing is
 * requested and everything is empty. Each request is ignored once the id has
 * changed or the caller has unmounted.
 */
export function useCompanyData(
    requestedId: number | null,
    isAuthenticated: boolean,
    options: CompanyDataOptions = {},
): CompanyData {
    // Nothing is requested for an id that is not a positive integer: it would
    // otherwise be interpolated into three URL paths as given (issue #486).
    const organizationId = isCompanyId(requestedId) ? requestedId : null;
    const skipScores = options.skipScores === true;
    const [scores, setScores] = React.useState<ScoresState>({id: null, items: NO_SCORES, loading: false});
    const [provenance, setProvenance] = React.useState<ProvenanceState>(
        {id: null, data: null, loading: false});
    const [corrections, setCorrections] = React.useState<CorrectionsState>({id: null, items: NO_CORRECTIONS});
    const [provenanceAttempt, setProvenanceAttempt] = React.useState(0);
    const [retryFailures, setRetryFailures] = React.useState(0);
    // Set by retryProvenance, consumed by the next failure: the caller is told
    // about a failure only when the user started the attempt that failed.
    const userRetryRef = React.useRef(false);

    React.useEffect(() => {
        if (organizationId === null || skipScores) {
            setScores({id: organizationId, items: NO_SCORES, loading: false});
            return undefined;
        }
        let cancelled = false;
        setScores({id: organizationId, items: NO_SCORES, loading: true});
        fetch(`/api/organizations/${organizationId}/scores/`)
            .then(response => {
                if (response.ok === false) throw new Error(`Scores request failed (${response.status})`);
                return response.json();
            })
            .then(data => {
                if (cancelled) return;
                setScores({id: organizationId, items: Array.isArray(data) ? data : NO_SCORES, loading: false});
            })
            .catch(error => {
                if (cancelled) return;
                console.error('Error fetching organization scores:', error);
                setScores({id: organizationId, items: NO_SCORES, loading: false});
            });
        return () => {
            cancelled = true;
        };
    }, [organizationId, skipScores]);

    React.useEffect(() => {
        userRetryRef.current = false;
    }, [organizationId]);

    React.useEffect(() => {
        if (organizationId === null) {
            setProvenance({id: null, data: null, loading: false});
            return undefined;
        }
        let cancelled = false;
        setProvenance(previous => ({
            id: organizationId,
            data: previous.id === organizationId ? previous.data : null,
            loading: true,
        }));
        fetch(`/api/organizations/${organizationId}/provenance/`)
            .then(response => {
                if (response.ok === false) throw new Error(`Provenance request failed (${response.status})`);
                return response.json();
            })
            .then(data => {
                if (cancelled) return;
                userRetryRef.current = false;
                setProvenance({id: organizationId, data, loading: false});
                setCachedProvenance(organizationId, data);
            })
            .catch(error => {
                if (cancelled) return;
                console.error('Error fetching organization provenance:', error);
                setProvenance({id: organizationId, data: null, loading: false});
                if (userRetryRef.current) setRetryFailures(count => count + 1);
                userRetryRef.current = false;
            });
        return () => {
            cancelled = true;
        };
    }, [organizationId, provenanceAttempt]);

    // Pending suggestions belong to one account. A purge (sign-out, account
    // switch) or an account change in the workspace store empties the list and
    // makes any response still in flight for the previous account ignorable;
    // the shell closes the view itself, this keeps the rows out of the DOM in
    // the meantime. An `unobserved` hydration never reaches here (issue #527).
    const accountEpoch = React.useRef(0);
    const [correctionsAttempt, setCorrectionsAttempt] = React.useState(0);
    React.useEffect(() => {
        const clear = () => {
            accountEpoch.current += 1;
            setCorrections({id: null, items: NO_CORRECTIONS});
        };
        let seen = getWorkspaceSnapshot().account;
        const unsubscribe = subscribeWorkspace(() => {
            const account = getWorkspaceSnapshot().account;
            const previous = seen;
            seen = account;
            if (previous.status !== 'unknown'
                && (previous.status !== account.status || previous.key !== account.key)) {
                clear();
            }
        });
        document.addEventListener('crank:private-state-purged', clear);
        return () => {
            unsubscribe();
            document.removeEventListener('crank:private-state-purged', clear);
        };
    }, []);

    React.useEffect(() => {
        const saved = (event: Event) => {
            if ((event as CustomEvent).detail?.organizationId === organizationId) {
                setCorrectionsAttempt(attempt => attempt + 1);
            }
        };
        window.addEventListener(COMPANY_CORRECTION_SAVED_EVENT, saved);
        return () => window.removeEventListener(COMPANY_CORRECTION_SAVED_EVENT, saved);
    }, [organizationId]);

    // The requester's own pending suggestions (issue #477). Only fetched when
    // signed in; a failure simply hides the list.
    React.useEffect(() => {
        if (organizationId === null || !isAuthenticated) {
            setCorrections({id: organizationId, items: NO_CORRECTIONS});
            return undefined;
        }
        let cancelled = false;
        const epoch = accountEpoch.current;
        const stale = () => cancelled || epoch !== accountEpoch.current;
        setCorrections(previous => (previous.id === organizationId ? previous : {id: organizationId, items: NO_CORRECTIONS}));
        fetch(`/api/company-corrections/?organization=${organizationId}`)
            .then(response => (response.ok ? response.json() : {corrections: []}))
            .then(data => {
                if (stale()) return;
                const items: PendingCorrection[] = Array.isArray(data.corrections) ? data.corrections : [];
                setCorrections({id: organizationId, items: items.filter(item => item.status === 'pending')});
            })
            .catch(() => {
                if (!stale()) setCorrections({id: organizationId, items: NO_CORRECTIONS});
            });
        return () => {
            cancelled = true;
        };
    }, [organizationId, isAuthenticated, correctionsAttempt]);

    const retryProvenance = React.useCallback(() => {
        userRetryRef.current = true;
        setProvenanceAttempt(attempt => attempt + 1);
    }, []);

    const shown = organizationId !== null;
    const scoresCurrent = scores.id === organizationId;
    const provenanceCurrent = provenance.id === organizationId;
    return {
        scores: scoresCurrent ? scores.items : NO_SCORES,
        // Before the effect for a new id has run, its request is as good as
        // started: report loading instead of an empty breakdown.
        scoresLoading: shown && !skipScores && (scoresCurrent ? scores.loading : true),
        provenance: provenanceCurrent ? provenance.data : null,
        provenanceLoading: shown && (provenanceCurrent ? provenance.loading : true),
        retryProvenance,
        retryFailures,
        pendingCorrections: isAuthenticated && corrections.id === organizationId ? corrections.items : NO_CORRECTIONS,
    };
}
