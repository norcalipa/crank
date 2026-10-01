// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Which surface hosts the priorities UI (issue #480).

import type {PrioritiesVariant} from './PrioritiesSection';

const DESKTOP_QUERY = '(min-width: 768px)';

/** Which surface hosts the priorities: the page's main block on desktop, else the assistant panel. */
export function prioritiesSurface(): PrioritiesVariant {
    const hasMain = typeof document !== 'undefined' && document.getElementById('priorities-main') !== null;
    return hasMain && window.matchMedia?.(DESKTOP_QUERY).matches ? 'main' : 'sidebar';
}

export function subscribeDesktop(onChange: () => void): () => void {
    const query = window.matchMedia?.(DESKTOP_QUERY);
    query?.addEventListener?.('change', onChange);
    return () => query?.removeEventListener?.('change', onChange);
}
