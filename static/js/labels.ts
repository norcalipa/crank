// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.

// Human labels for the company fields that carry evidence and can be
// corrected (mirrors CompanyFieldEvidence.FieldKey on the server).
export const CORRECTABLE_FIELD_LABELS: Record<string, string> = {
    rto_policy: 'RTO Policy',
    funding_round: 'Funding Round',
    public_status: 'Public Status',
    accelerated_vesting: 'Accelerated Vesting',
    locations: 'Locations',
    company_name: 'Company Name',
    company_domain: 'Company Domain',
};

export const isCorrectableFieldKey = (key: unknown): key is string =>
    typeof key === 'string' && Object.prototype.hasOwnProperty.call(CORRECTABLE_FIELD_LABELS, key);

export const fieldKeyLabel = (key: string): string => CORRECTABLE_FIELD_LABELS[key] || key;
