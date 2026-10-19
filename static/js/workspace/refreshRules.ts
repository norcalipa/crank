// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// The idle rule for "Updated results available" (issue #485, owner decision),
// shared by the job-match panel and the company rankings so the two cannot
// drift. Pure and import-free: both bundles can take it.

export interface AutoApplyState {
    foundOnFirstCheckAfterReturn: boolean;
    scrollY: number;
    /** Something inside the surface (the panel, the results) has focus. */
    focusInsidePanel: boolean;
    /** The surface cannot take new results right now (a company is open over
     * the rankings). Absent means not blocked. */
    blocked?: boolean;
}

/** Newer results are applied without asking only when they were found on the
 * first check after the tab became visible again, the page is at the top,
 * nothing inside the surface has focus, and the surface is not blocked. Any
 * other case shows the notice and changes nothing. */
export function shouldAutoApplyRefresh(state: AutoApplyState): boolean {
    return state.foundOnFirstCheckAfterReturn && state.scrollY === 0 && !state.focusInsidePanel
        && state.blocked !== true;
}
