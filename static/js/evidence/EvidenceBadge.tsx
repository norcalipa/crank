// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {EVIDENCE_STATUS_META, EvidenceStatusKey, formatEvidenceDate} from '../labels';

interface EvidenceBadgeProps {
    status: EvidenceStatusKey;
    fieldLabel?: string;
    lastVerifiedAt?: string | null;
    testId?: string;
}

// The only renderer of evidence status words. Status is a word plus a marker;
// the optional field label and date extend the accessible name only.
const EvidenceBadge: React.FC<EvidenceBadgeProps> = ({status, fieldLabel, lastVerifiedAt, testId}) => {
    const meta = EVIDENCE_STATUS_META[status];
    return (
        <span className={`evidence-badge evidence-badge-${status}`} data-testid={testId} data-status={status}
              title={meta.meaning}>
            {fieldLabel && <span className="visually-hidden">{fieldLabel}: </span>}
            <span className="evidence-badge-marker" aria-hidden="true">{meta.marker}</span>
            {' '}{meta.label}
            {lastVerifiedAt && (
                <span className="visually-hidden">, last verified {formatEvidenceDate(lastVerifiedAt, true)}</span>
            )}
        </span>
    );
};

export default EvidenceBadge;
