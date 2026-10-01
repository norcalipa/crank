// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.

// Provenance responses already fetched by the details dialog, so the
// correction form can show the current value without a loading flash.
export interface CachedProvenance {
    fields?: Array<{
        field_key: string;
        value: string;
        stale: boolean;
        last_verified_at: string | null;
        source_domain?: string | null;
    }>;
    unverified_fields?: string[];
    displayed_values?: Record<string, string>;
}

const cache = new Map<number, CachedProvenance>();

export const getCachedProvenance = (organizationId: number): CachedProvenance | undefined =>
    cache.get(organizationId);

export const setCachedProvenance = (organizationId: number, data: CachedProvenance): void => {
    cache.set(organizationId, data);
};

export const clearProvenanceCache = (): void => {
    cache.clear();
};
