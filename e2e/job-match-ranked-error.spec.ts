// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// A failed ranked read is an error with Retry, never "No matches" (issue #609).
import {test, expect} from '@playwright/test';

const FIXTURE = '/e2e/fixtures/job-match-panel.html?state=ranked_unavailable';

for (const [name, width, height] of [['desktop', 1280, 800], ['mobile', 375, 800]] as const) {
    test(`a failed ranked read shows the error card and Try again at ${name} width`, async ({page}) => {
        await page.setViewportSize({width, height});
        await page.goto(FIXTURE);
        const alert = page.getByTestId('job-match-error');
        await expect(alert).toBeVisible();
        await expect(alert).toHaveAttribute('role', 'alert');
        await expect(alert).toContainText('We couldn’t load your job matches. Please try again.');
        await expect(page.getByRole('button', {name: 'Retry loading matches'})).toBeVisible();
        await expect(page.getByText('No matches for your current requirements')).toHaveCount(0);
    });
}
