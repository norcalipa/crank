// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import {shouldAutoApplyRefresh} from './refreshRules';

describe('shouldAutoApplyRefresh (issue #485 idle rule)', () => {
    const idle = {foundOnFirstCheckAfterReturn: true, scrollY: 0, focusInsidePanel: false};

    it('applies only on the first check after a return, at the top, with nothing focused', () => {
        expect(shouldAutoApplyRefresh(idle)).toBe(true);
        expect(shouldAutoApplyRefresh({...idle, foundOnFirstCheckAfterReturn: false})).toBe(false);
        expect(shouldAutoApplyRefresh({...idle, scrollY: 1})).toBe(false);
        expect(shouldAutoApplyRefresh({...idle, focusInsidePanel: true})).toBe(false);
    });

    it('never applies while the surface is blocked, and an absent flag is not blocked', () => {
        expect(shouldAutoApplyRefresh({...idle, blocked: true})).toBe(false);
        expect(shouldAutoApplyRefresh({...idle, blocked: false})).toBe(true);
    });
});
