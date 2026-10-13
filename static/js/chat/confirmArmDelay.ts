// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.

// A confirmation opens under the pointer that chose its item, so a second press
// of a double press would land on the confirm button; a press that starts
// earlier than this after the confirmation opened is ignored.
// No imports: the Playwright runner loads this file (e2e/django/support.ts,
// e2e/django/chat-layout.spec.ts and e2e/job-search-chat-viewport.spec.ts), and
// tsconfig.json covers static/js only, so tsc does not see those importers.
export const CONFIRM_ARM_DELAY_MS = 500;
