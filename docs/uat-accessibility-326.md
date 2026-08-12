<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Phase 4 UAT and accessibility record (#326)

This record covers the authenticated job-search chat and the currently shipped
organization ranking/details result surface. It uses synthetic data only. It is
an evidence record, not a claim of legal accessibility certification.

## Test context

| Field | Value |
| --- | --- |
| Environment | Local test build; network/provider calls mocked |
| Test date | 2026-08-12 |
| Browser/device | Jest + jsdom (Node 22.23.2); staging browser run still required before production enablement |
| Assistive technology | Stable DOM assertions cover screen-reader names/live regions; NVDA/VoiceOver/TalkBack execution is a rollout follow-up |
| Data | Synthetic account and messages (`remote work`, `exportable`, `Organization 1`) |
| Commands | `npx jest --runInBand --coverage`; `npx tsc --noEmit`; `git diff --check` |

## Personas and critical scripts

Each script starts as an authenticated synthetic account. The expected result is
written down so a staging tester can replay it without real personal data.

| ID / persona | Script and expected result | Evidence/status | Owner |
| --- | --- | --- | --- |
| UAT-01 first-time interview | Open `/chat/`; start with an empty conversation; submit `I need remote work`; verify the user turn and assistant reply appear once, focus returns to Message, and the empty-state prompt is replaced. | Jest: submit, empty state, focus, and live-log assertions pass. | Engineering |
| UAT-02 returning conversation | Reload with synthetic existing messages; verify message order, distinct `Your message` / `Assistant message` semantics, and that the Message field is enabled. | Jest: existing history and semantic-role assertions pass. | Engineering |
| UAT-03 correction/removal | Submit a preference-changing turn; verify the saved-preferences announcement says the user can correct or remove a preference by telling the assistant what to change. | Jest: preference disclosure and `aria-describedby` assertions pass. | Engineering |
| UAT-04 rationale/limitations | Read the visible notice before submitting; confirm it says the assistant is automated, may be wrong, and important details should be checked. Confirm recommendation rationale is not represented as certainty. | Chat limitation/data notice is rendered and tested. Match rationale UI is not shipped in this worktree; track with the match-results UI owner. | Product + #321 owner |
| UAT-05 browse/dismiss match | Browse ranked synthetic organizations; open details with Enter/Space or pointer; close with the labelled button or Escape. | Jest: keyboard row activation, dialog name/modal semantics, close focus, and Escape assertions pass. Match API dismiss endpoint remains owner-scoped; no in-app match-card dismiss control exists in this worktree. | Engineering + #321 owner |
| UAT-06 provider/source failure | Mock chat HTTP 500/non-JSON response and source-choice/detail failures; verify an understandable alert, retry where available, no duplicate optimistic message, and no raw provider payload. | Jest: chat failure/rollback/retry and existing result-source failure tests pass. | Engineering |
| UAT-07 export/reset/delete | With synthetic history, activate Export; confirm a JSON download. Confirm Reset and Delete before the action; verify reset clears the active view and delete removes the conversation. | Jest: export, reset, delete success/failure paths pass. | Engineering |
| UAT-08 empty states | Open a new chat and filter the organization list to zero results; verify the empty messages are plain language and announced/visible without relying on color. | Jest: empty chat and organization alert assertions pass. | Engineering |

## Accessibility checklist and evidence

The following checks were performed through source review and deterministic DOM
assertions in `static/js/*.test.tsx`; the browser/device items are required in
the staging run recorded below.

- **Keyboard/focus:** native buttons and inputs are in the tab order; ranking
  rows are focusable button-like controls and activate on Enter/Space; the
  details dialog focuses Close and closes on Escape; chat refocuses Message
  after submit/reset/delete.
- **Names/semantics:** chat is a named `section`; the conversation title is a
  heading; history is a `role=log` with a polite live region; each message is a
  labelled `article`; controls have visible/accessible names; ranking has a
  caption and labelled search; details are a modal dialog with a labelled
  heading.
- **Announcements:** pending and error states use status/alert regions; the
  preference-change notice is a status with an explicit description; the log
  exposes `aria-busy` while a turn is pending.
- **Contrast:** Bootstrap dark-theme component classes provide the existing
  contrast treatment. Staging must verify computed contrast for custom/theme
  overrides with browser devtools or axe; no color-only meaning is used for
  empty/error/preference states.
- **Zoom/reflow:** the chat history uses bounded vertical scrolling and message
  wrapping; the organization table remains native table markup. Staging must
  verify 200% zoom and 320 CSS-pixel reflow without clipped controls.
- **Assistive technology:** staging must replay UAT-01, UAT-02, UAT-03, UAT-05,
  UAT-07, and UAT-08 with NVDA/Firefox (Windows), VoiceOver/Safari (macOS/iOS),
  and TalkBack/Chrome (Android), recording browser/device/OS versions.

## Rollout triage

### Blockers

- None found in the tested chat or organization ranking/details surfaces by
  source review and Jest assertions.
- Production rollout remains gated on the staging browser/assistive-technology
  run above. This is evidence still required, not a claim that jsdom replaces
  manual testing.

### Non-blocking follow-ups

1. **P1 / Product + #321 owner:** ship the in-app match-results UI (including
   rationale, source/limitations disclosure, keyboard browsing, and dismissal)
   against the existing owner-scoped `/api/job-matches/` endpoints, then extend
   this record with match-card evidence. The endpoint alone is not a user-facing
   match UI.
2. **P1 / Release engineering:** execute the staging browser matrix and attach
   sanitized results (no session IDs or personal/job-search data).
3. **P2 / Design system owner:** run automated contrast checks against the
   production Bootstrap/theme bundle and record any custom override findings.
4. **P2 / Engineering:** address existing React test `act(...)` warnings in a
   separate cleanup; they do not fail the current assertions.

## Gate evidence

The final PR description records the exact commands and results after the last
formatting change. Changed text files must end with exactly one newline, and
`git diff --check` must be clean before push.
